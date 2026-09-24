"""工作台任务流程阶段的回归测试。

阶段（stage）由后端执行器显式写入、前端流程条据此渲染进度。
这组测试锁定"哪个执行器写哪个阶段"这一契约，防止有人把它退回成前端匹配日志文案——
那样后端改一个字，进度条就会静默失效并显示"等待后端返回阶段"。

runner 的持久化走独立的临时 SQLite 库（见 _IsolatedDbTests.setUp），
不会往共享业务库里写任务行、污染面板的「运行记录」。
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bosshunter.db import get_db as real_get_db
from bosshunter.web import server
from bosshunter.web.tasks import PIPELINE_STAGE_KEYS, WorkbenchTask, WorkbenchTaskRunner


def _no_persist_runner() -> SimpleNamespace:
    """替换模块级 task_runner，让 _log 只改内存状态、不落库。"""
    return SimpleNamespace(_persist=lambda task: None)


def _by_id(runner: WorkbenchTaskRunner) -> dict[str, dict]:
    return {task["id"]: task for task in runner.status()["tasks"]}


class _IsolatedDbTests(unittest.TestCase):
    """把 bosshunter.web.tasks.get_db 指向临时 SQLite，隔离任务持久化。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        db_path = Path(self._tmp.name) / "bosshunter.db"
        patcher = patch("bosshunter.web.tasks.get_db", lambda: real_get_db(db_path))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)


class WorkbenchTaskStageTests(_IsolatedDbTests):
    def test_stage_keys_match_frontend_pipeline_order(self):
        """阶段枚举是前后端契约，顺序即流程条顺序。"""
        self.assertEqual(PIPELINE_STAGE_KEYS, ("collect", "score", "confirm", "greet", "send"))

    def test_task_starts_without_stage(self):
        """未进入任何已知阶段时 stage 为 None，前端据此不渲染任何进度勾选。"""
        task = WorkbenchTask(id="stage-none", mode="collect", label="单独采集")

        self.assertIsNone(task.snapshot()["stage"])

    def test_log_with_stage_advances_task_and_survives_restart(self):
        """带 stage 的日志推进阶段，且落库后能被新 runner 恢复（重启不丢进度）。"""
        with patch.object(server, "task_runner", _no_persist_runner()):
            runner = WorkbenchTaskRunner(
                {"collect": lambda task, config: server._log(task, "开始 AI 评分", stage="score")}
            )
            started = runner.start("collect", {})
            runner.wait(timeout=2)
            self.assertEqual(_by_id(runner)[started["id"]]["stage"], "score")

            restored = WorkbenchTaskRunner()
            self.assertEqual(_by_id(restored)[started["id"]]["stage"], "score")

    def test_log_without_stage_keeps_current_stage(self):
        """同一阶段内的后续日志不应把阶段清空或改写。"""
        task = WorkbenchTask(id="stage-keep", mode="collect", label="单独采集")
        with patch.object(server, "task_runner", _no_persist_runner()):
            server._log(task, "开始采集岗位", stage="collect")
            server._log(task, "采集完成，岗位已进入待评分队列")

        self.assertEqual(task.stage, "collect")

    def test_failed_task_keeps_last_stage_for_diagnosis(self):
        """失败任务保留最后阶段，前端据此提示"停在某一步"，而不是只给一句失败。"""

        def failing_executor(task, config):
            server._log(task, "开始 AI 评分", stage="score")
            raise RuntimeError("AI 未配置")

        with patch.object(server, "task_runner", _no_persist_runner()):
            runner = WorkbenchTaskRunner({"score": failing_executor})
            started = runner.start("score", {})
            runner.wait(timeout=2)
            snapshot = _by_id(runner)[started["id"]]

        self.assertEqual(snapshot["status"], "failed")
        self.assertEqual(snapshot["stage"], "score")


class WorkbenchExecutorStageTests(_IsolatedDbTests):
    """逐个执行器确认它写入的阶段 key。"""

    def test_execute_collect_reports_collect_stage(self):
        task = WorkbenchTask(id="stage-collect", mode="collect", label="单独采集")
        config = {"search": {"keywords": ["python"]}, "_workbench_limit": 1}
        with (
            patch.object(server, "task_runner", _no_persist_runner()),
            patch("bosshunter.scraper.jobs.scrape_jobs", return_value=[]),
        ):
            server._execute_collect(task, config)

        self.assertEqual(task.stage, "collect")

    def test_execute_score_reports_score_stage(self):
        task = WorkbenchTask(id="stage-score", mode="score", label="仅评分待处理岗位")
        with (
            patch.object(server, "task_runner", _no_persist_runner()),
            patch("bosshunter.ai.scorer.score_jobs", return_value=(1, 0)),
            patch.object(server, "_get_web_db", side_effect=RuntimeError("跳过待重试统计")),
        ):
            with self.assertRaises(RuntimeError):
                server._execute_score(task, {})

        # 即使后续统计失败，阶段也已推进到 score，前端能指出"停在哪一步"。
        self.assertEqual(task.stage, "score")

    def test_execute_full_scores_immediately_after_collect(self):
        """全流程必须包含 AI 评分。

        漏掉评分时，新采集的岗位停在 pending，待确认查询必然为空，
        「运行全流程」会静默退化成"只采集"，与界面文案和 CLI run_pipeline 都不符。
        """
        task = WorkbenchTask(id="stage-full-order", mode="full", label="运行全流程")
        order: list[str] = []
        with (
            patch.object(server, "task_runner", _no_persist_runner()),
            patch.object(server, "_execute_collect", side_effect=lambda t, c: order.append("collect")),
            patch.object(server, "_execute_score", side_effect=lambda t, c: order.append("score")),
            patch.object(server, "_get_web_db", side_effect=RuntimeError("到此为止")),
        ):
            with self.assertRaises(RuntimeError):
                server._execute_full(task, {})

        self.assertEqual(order, ["collect", "score"])

    def test_execute_full_waits_at_confirm_stage(self):
        """全流程在等人工确认时停在 confirm，而不是靠日志里的"确认"二字去猜。"""
        task = WorkbenchTask(id="stage-full", mode="full", label="运行全流程")
        db = SimpleNamespace(close=lambda: None)
        with (
            patch.object(server, "task_runner", _no_persist_runner()),
            patch.object(server, "_execute_collect", side_effect=lambda t, c: None),
            patch.object(server, "_execute_score", side_effect=lambda t, c: None),
            patch.object(server, "_get_web_db", return_value=db),
            patch.object(server, "get_jobs_pending_confirmation", return_value=[]),
        ):
            server._execute_full(task, {})

        self.assertEqual(task.stage, "confirm")

    def test_execute_greeting_reports_greet_stage(self):
        task = WorkbenchTask(id="stage-greeting", mode="greeting", label="重新生成招呼语")
        with (
            patch.object(server, "task_runner", _no_persist_runner()),
            patch("bosshunter.ai.greeter.generate_greetings", return_value=1),
        ):
            server._execute_greeting(task, {})

        self.assertEqual(task.stage, "greet")

    def test_execute_deliver_ends_on_send_stage(self):
        """投递先经过「生成招呼语」，真正发送时切到「发送与监测」。"""
        task = WorkbenchTask(id="stage-deliver", mode="deliver", label="确认投递")
        config = {"_workbench_job_ids": ["job-1"]}
        with (
            patch.object(server, "task_runner", _no_persist_runner()),
            patch("bosshunter.ai.greeter.generate_greetings", return_value=1),
            patch("bosshunter.executor.sender.send_greetings", return_value=1),
        ):
            server._execute_deliver(task, config)

        self.assertEqual(task.stage, "send")

    def test_execute_monitor_reports_send_stage(self):
        """监测归到流程条最后一步「发送与监测」，首轮即把阶段标好。"""
        task = WorkbenchTask(id="stage-monitor", mode="monitor", label="单独监测")

        def stop_after_first_round(config):
            # 监测是无限轮询，首轮结束后请求停止，避免测试挂住。
            task.stop_requested.set()
            return {"unmatched_convs": []}

        with (
            patch.object(server, "task_runner", _no_persist_runner()),
            patch("bosshunter.executor.monitor.monitor_and_send_resumes", side_effect=stop_after_first_round),
        ):
            server._execute_monitor(task, {"monitor": {"interval": 30}})

        self.assertEqual(task.stage, "send")


if __name__ == "__main__":
    unittest.main()
