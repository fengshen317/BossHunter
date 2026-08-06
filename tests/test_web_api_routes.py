import io
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import yaml

from bosshunter.db import (
	add_history,
	get_db,
	insert_job,
	update_job_enrichment,
	update_job_score,
	update_job_status,
)
from bosshunter.throttle import SendWindowChecker
from bosshunter.web import server
from threading import Event

from bosshunter.web.tasks import TaskAlreadyRunningError, WorkbenchTask, WorkbenchTaskRunner


def _poll_task_status(runner: WorkbenchTaskRunner, task_id: str, want: str, timeout: float = 1.5) -> dict | None:
    """轮询指定任务直到达到期望状态（长任务阻塞时不能用 runner.wait）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for task in runner.status()["tasks"]:
            if task["id"] == task_id and task["status"] == want:
                return task
        time.sleep(0.02)
    return None


def _job(job_id: str) -> dict:
    return {
        "id": job_id,
        "title": "Product Manager",
        "company": "Example",
        "salary": "20-30K",
        "city": "Shanghai",
        "experience": "1-3 years",
        "jd": "Build AI product features",
        "hr_name": "HR",
        "hr_title": "Recruiter",
        "hr_active": "",
        "company_size": "",
        "company_industry": "",
        "url": "https://example.com/job",
    }


class WebApiRouteTests(unittest.TestCase):
    def setUp(self):
        # Arrange
        self.original_base_dir = server.BASE_DIR

    def tearDown(self):
        # Cleanup
        server.set_base_dir(self.original_base_dir)

    def _request(self, path: str, method: str = "GET"):
        if "?" in path:
            path_info, query_string = path.split("?", 1)
        else:
            path_info, query_string = path, ""

        status_headers = {}

        def start_response(status, headers, exc_info=None):
            status_headers["status"] = status
            status_headers["headers"] = dict(headers)

        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path_info,
            "QUERY_STRING": query_string,
            "SERVER_NAME": "127.0.0.1",
            "SERVER_PORT": "8686",
            "wsgi.version": (1, 0),
            "wsgi.url_scheme": "http",
            "wsgi.input": io.BytesIO(b""),
            "wsgi.errors": io.StringIO(),
            "wsgi.multithread": False,
            "wsgi.multiprocess": False,
            "wsgi.run_once": False,
        }

        body = b"".join(
            chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
            for chunk in server.app(environ, start_response)
        ).decode("utf-8")
        return status_headers["status"], status_headers["headers"], body

    def test_web_api_missing_api_route_returns_json_404_not_spa_html(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            server.set_base_dir(Path(tmp))

            # Act
            status, headers, body = self._request("/api/does-not-exist")

        # Assert
        self.assertTrue(status.startswith("404"))
        self.assertIn("application/json", headers["Content-Type"])
        self.assertEqual(json.loads(body), {"error": "Not found"})
        self.assertNotIn("<!doctype html", body.lower())

    def test_database_timestamps_are_serialized_as_utc(self):
        record = {
            "id": "job-1",
            "created_at": "2026-07-30 14:57:21",
            "updated_at": "2026-07-30T14:58:00",
        }

        serialized = server._serialize_db_item(record)

        self.assertEqual(serialized["created_at"], "2026-07-30T14:57:21Z")
        self.assertEqual(serialized["updated_at"], "2026-07-30T14:58:00Z")
        self.assertEqual(record["created_at"], "2026-07-30 14:57:21")

    def test_web_api_workbench_preflight_full_returns_json_payload(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            resume_path = base_dir / "resume.md"
            resume_path.write_text("# Resume", encoding="utf-8")
            (base_dir / "config.yaml").write_text(
                yaml.dump(
                    {
                        "profile": {"resume_path": str(resume_path)},
                        "search": {"keywords": ["AI产品经理"]},
                        "ai": {"api_key": "test-api-key"},
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            server.set_base_dir(base_dir)

            # Act
            status, headers, body = self._request("/api/workbench/preflight?mode=full")

        # Assert
        self.assertTrue(status.startswith("200"))
        self.assertIn("application/json", headers["Content-Type"])
        self.assertEqual(json.loads(body), {"ok": True, "messages": []})

    def test_web_api_workbench_preflight_full_requires_ai_key(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            resume_path = base_dir / "resume.md"
            resume_path.write_text("# Resume", encoding="utf-8")
            (base_dir / "config.yaml").write_text(
                yaml.dump(
                    {
                        "profile": {"resume_path": str(resume_path)},
                        "search": {"keywords": ["AI产品经理"]},
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            server.set_base_dir(base_dir)

            # Act
            with patch.dict("os.environ", {}, clear=True):
                status, headers, body = self._request("/api/workbench/preflight?mode=full")

        # Assert
        self.assertTrue(status.startswith("200"))
        self.assertIn("application/json", headers["Content-Type"])
        payload = json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertIn("请先在配置页填写 AI API Key，或设置 ANTHROPIC_API_KEY 环境变量。", payload["messages"])

    def test_web_api_activity_returns_json_without_runtime_name_error(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            server.set_base_dir(Path(tmp))

            # Act
            status, headers, body = self._request("/api/activity?days=7")

        # Assert
        self.assertTrue(status.startswith("200"))
        self.assertIn("application/json", headers["Content-Type"])
        self.assertEqual(json.loads(body), [])

    def test_web_api_workbench_pending_confirmation_returns_ready_jobs(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                insert_job(db, _job("ready-job"))
                update_job_score(db, "ready-job", 82, "good match")
                update_job_status(db, "ready-job", "ready")
            finally:
                db.close()
            server.set_base_dir(base_dir)

            # Act
            status, headers, body = self._request("/api/workbench")

        # Assert
        payload = json.loads(body)
        self.assertTrue(status.startswith("200"))
        self.assertIn("application/json", headers["Content-Type"])
        self.assertEqual([job["id"] for job in payload["pending_confirmation"]], ["ready-job"])

    def test_web_api_full_task_stays_running_while_waiting_for_frontend_confirmation(self):
        # Arrange
        confirmation_reached = False

        def fake_collect(task, config):
            nonlocal confirmation_reached
            confirmation_reached = True

        runner = WorkbenchTaskRunner()
        runner._executors["full"] = lambda task, config: server._execute_full(task, config)

        # Act
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                insert_job(db, _job("ready-job"))
                update_job_score(db, "ready-job", 82, "good match")
                update_job_status(db, "ready-job", "ready")
            finally:
                db.close()
            server.set_base_dir(base_dir)

            with patch.object(server, "_execute_collect", side_effect=fake_collect):
                task = runner.start("full", {})
                for _ in range(20):
                    status = runner.status()
                    active = status["active"]
                    if active and "等待前端确认投递" in active["logs"]:
                        break
                    time.sleep(0.01)
                time.sleep(0.05)
                status = runner.status()
                active = status["active"]
                if active:
                    runner._tasks[task["id"]].stop_requested.set()
                    runner.wait(timeout=1)

        # Assert
        self.assertTrue(confirmation_reached)
        self.assertIsNotNone(active)
        self.assertEqual(active["id"], task["id"])
        self.assertEqual(active["status"], "running")
        self.assertIn("等待前端确认投递", active["logs"])

    def test_task_stop_keeps_active_slot_until_executor_really_returns(self):
        # Arrange
        started = Event()
        release = Event()

        def blocking_executor(task, config):
            started.set()
            release.wait(timeout=1)

        runner = WorkbenchTaskRunner({
            "collect": blocking_executor,
            "monitor": lambda task, config: None,
        })
        task = runner.start("collect", {})
        self.assertTrue(started.wait(timeout=1))

        try:
            # Act
            stopped = runner.stop(task["id"])
            status_after_stop = runner.status()
            with self.assertRaises(TaskAlreadyRunningError):
                runner.start("monitor", {})
            release.set()
            runner.wait(timeout=1)
            second_task = runner.start("monitor", {})
            runner.wait(timeout=1)
        finally:
            release.set()
            runner.wait(timeout=1)

        # Assert
        self.assertEqual(stopped["status"], "stopping")
        self.assertEqual(status_after_stop["active"]["id"], task["id"])
        self.assertEqual(status_after_stop["active"]["status"], "stopping")
        self.assertEqual(second_task["mode"], "monitor")

    def test_task_runner_automatically_stops_at_send_window_deadline(self):
        # Arrange
        def wait_for_stop(task, config):
            task.stop_requested.wait(timeout=1)

        runner = WorkbenchTaskRunner({"monitor": wait_for_stop})
        deadline = datetime.now() + timedelta(milliseconds=50)

        # Act
        with patch("bosshunter.web.tasks._deadline_from_config", return_value=deadline):
            task = runner.start("monitor", {"throttle": {"send_windows": ["09:00-16:00"]}})
            runner.wait(timeout=1)
        result = runner.status()["last_task"]

        # Assert
        self.assertEqual(task["deadline_at"], deadline.isoformat(timespec="seconds"))
        self.assertEqual(result["status"], "stopped")
        self.assertTrue(result["stop_requested"])
        self.assertEqual(result["stop_reason"], "已到发送时间窗口截止时间，后台自动停止")
        self.assertIn(result["stop_reason"], result["logs"])

    def test_reply_can_run_while_monitor_is_active(self):
        # Arrange: 长任务 monitor 阻塞运行，reply 短任务应可并行启动。
        started = Event()
        release = Event()
        reply_done = Event()

        def blocking_executor(task, config):
            started.set()
            release.wait(timeout=2)

        runner = WorkbenchTaskRunner({
            "monitor": blocking_executor,
            "reply": lambda task, config: reply_done.set(),
        })
        monitor_task = runner.start("monitor", {})
        self.assertTrue(started.wait(timeout=1))

        try:
            # Act: monitor 运行中启动 reply → 应成功（并行）
            reply_task = runner.start("reply", {})
            self.assertTrue(reply_done.wait(timeout=1))
            final = _poll_task_status(runner, reply_task["id"], "completed")
        finally:
            release.set()
            runner.wait(timeout=1)

        # Assert
        self.assertEqual(monitor_task["status"], "running")
        self.assertEqual(reply_task["mode"], "reply")
        self.assertIsNotNone(final)
        self.assertEqual(final["status"], "completed")

    def test_reply_is_mutually_exclusive_with_another_reply(self):
        # Arrange
        started = Event()
        release = Event()

        def blocking_executor(task, config):
            started.set()
            release.wait(timeout=2)

        runner = WorkbenchTaskRunner({"reply": blocking_executor})
        runner.start("reply", {})
        self.assertTrue(started.wait(timeout=1))

        try:
            # Act: 已有 reply 在跑，再启动 reply 应被拒绝
            with self.assertRaises(TaskAlreadyRunningError):
                runner.start("reply", {})
        finally:
            release.set()
            runner.wait(timeout=1)

    def test_long_task_is_blocked_while_reply_is_active(self):
        # Arrange
        started = Event()
        release = Event()

        def blocking_executor(task, config):
            started.set()
            release.wait(timeout=2)

        runner = WorkbenchTaskRunner({
            "reply": blocking_executor,
            "collect": lambda task, config: None,
        })
        runner.start("reply", {})
        self.assertTrue(started.wait(timeout=1))

        try:
            # Act: reply 运行中启动 collect（长任务）→ 应被拒绝
            with self.assertRaises(TaskAlreadyRunningError):
                runner.start("collect", {})
        finally:
            release.set()
            runner.wait(timeout=1)

    def test_task_runner_does_not_start_after_today_deadline(self):
        # Arrange
        executed = Event()
        runner = WorkbenchTaskRunner({"monitor": lambda task, config: executed.set()})
        deadline = datetime.now() - timedelta(minutes=1)

        # Act
        with patch("bosshunter.web.tasks._deadline_from_config", return_value=deadline):
            task = runner.start("monitor", {"throttle": {"send_windows": ["09:00-16:00"]}})

        # Assert
        self.assertEqual(task["status"], "stopped")
        self.assertEqual(task["stop_reason"], "今日发送时间窗口已截止，后台未启动")
        self.assertFalse(executed.is_set())
        self.assertIsNone(runner.status()["active"])

    def test_send_window_checker_uses_last_window_end_as_daily_deadline(self):
        checker = SendWindowChecker(["09:00-12:00", "14:00-17:30", "99:00-100:00"])

        deadline = checker.latest_end_datetime(datetime(2026, 7, 28, 10, 15, 45))

        self.assertEqual(deadline, datetime(2026, 7, 28, 17, 30))

    def test_web_api_full_task_completes_when_no_jobs_need_confirmation(self):
        # Arrange
        calls = []

        def fake_collect(task, config):
            calls.append("collect")

        runner = WorkbenchTaskRunner()
        runner._executors["full"] = lambda task, config: server._execute_full(task, config)

        # Act
        with tempfile.TemporaryDirectory() as tmp:
            server.set_base_dir(Path(tmp))
            with patch.object(server, "_execute_collect", side_effect=fake_collect):
                task = runner.start("full", {})
                runner.wait(timeout=1)
                status = runner.status()
                last_task = status["last_task"]

        # Assert
        self.assertEqual(calls, ["collect"])
        self.assertIsNone(status["active"])
        self.assertEqual(last_task["id"], task["id"])
        self.assertEqual(last_task["status"], "completed")
        self.assertIn("没有待确认岗位，流程结束", last_task["logs"])

    def test_web_api_deliver_hands_selected_jobs_to_waiting_full_task(self):
        # Arrange
        confirmation_event = Event()
        full_task = WorkbenchTask(id="full-task", mode="full", label="运行全流程")
        full_task.context["waiting_confirmation"] = True
        full_task.context["confirmation_event"] = confirmation_event
        runner = WorkbenchTaskRunner()
        runner._tasks[full_task.id] = full_task

        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                insert_job(db, _job("ready-job"))
                update_job_status(db, "ready-job", "ready")
            finally:
                db.close()
            server.set_base_dir(base_dir)

            body = json.dumps({"job_ids": ["ready-job"]}).encode("utf-8")
            status_headers = {}

            def start_response(status, headers, exc_info=None):
                status_headers["status"] = status
                status_headers["headers"] = dict(headers)

            environ = {
                "REQUEST_METHOD": "POST",
                "PATH_INFO": "/api/workbench/deliver",
                "QUERY_STRING": "",
                "CONTENT_LENGTH": str(len(body)),
                "CONTENT_TYPE": "application/json",
                "SERVER_NAME": "127.0.0.1",
                "SERVER_PORT": "8686",
                "wsgi.version": (1, 0),
                "wsgi.url_scheme": "http",
                "wsgi.input": io.BytesIO(body),
                "wsgi.errors": io.StringIO(),
                "wsgi.multithread": False,
                "wsgi.multiprocess": False,
                "wsgi.run_once": False,
            }

            # Act
            with patch.object(server, "task_runner", runner):
                response_body = b"".join(
                    chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
                    for chunk in server.app(environ, start_response)
                ).decode("utf-8")

        # Assert
        self.assertTrue(status_headers["status"].startswith("200"), response_body)
        self.assertTrue(confirmation_event.is_set())
        self.assertEqual(full_task.context["confirmed_job_ids"], ["ready-job"])
        self.assertEqual(json.loads(response_body)["id"], "full-task")

    def test_web_api_deliver_ignores_stale_stopped_full_task_waiting_context(self):
        # Arrange
        stale_event = Event()
        stale_task = WorkbenchTask(id="stale-full-task", mode="full", label="运行全流程", status="stopped")
        stale_task.context["waiting_confirmation"] = True
        stale_task.context["confirmation_event"] = stale_event

        active_event = Event()
        active_task = WorkbenchTask(id="active-full-task", mode="full", label="运行全流程")
        active_task.context["waiting_confirmation"] = True
        active_task.context["confirmation_event"] = active_event

        runner = WorkbenchTaskRunner()
        runner._tasks[stale_task.id] = stale_task
        runner._tasks[active_task.id] = active_task

        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                insert_job(db, _job("ready-job"))
                update_job_status(db, "ready-job", "ready")
            finally:
                db.close()
            server.set_base_dir(base_dir)

            body = json.dumps({"job_ids": ["ready-job"]}).encode("utf-8")
            status_headers = {}

            def start_response(status, headers, exc_info=None):
                status_headers["status"] = status
                status_headers["headers"] = dict(headers)

            environ = {
                "REQUEST_METHOD": "POST",
                "PATH_INFO": "/api/workbench/deliver",
                "QUERY_STRING": "",
                "CONTENT_LENGTH": str(len(body)),
                "CONTENT_TYPE": "application/json",
                "SERVER_NAME": "127.0.0.1",
                "SERVER_PORT": "8686",
                "wsgi.version": (1, 0),
                "wsgi.url_scheme": "http",
                "wsgi.input": io.BytesIO(body),
                "wsgi.errors": io.StringIO(),
                "wsgi.multithread": False,
                "wsgi.multiprocess": False,
                "wsgi.run_once": False,
            }

            # Act
            with patch.object(server, "task_runner", runner):
                response_body = b"".join(
                    chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
                    for chunk in server.app(environ, start_response)
                ).decode("utf-8")

        # Assert
        self.assertTrue(status_headers["status"].startswith("200"), response_body)
        self.assertFalse(stale_event.is_set())
        self.assertTrue(active_event.is_set())
        self.assertNotIn("confirmed_job_ids", stale_task.context)
        self.assertEqual(active_task.context["confirmed_job_ids"], ["ready-job"])
        self.assertEqual(json.loads(response_body)["id"], "active-full-task")

    def test_web_api_full_task_continues_delivery_and_monitoring_after_confirmation(self):
        # Arrange
        calls = []

        def fake_collect(task, config):
            calls.append("collect")

        def fake_deliver(task, config):
            calls.append(("deliver", config.get("_workbench_job_ids")))

        def fake_monitor(task, config):
            calls.append("monitor")

        runner = WorkbenchTaskRunner()
        runner._executors["full"] = lambda task, config: server._execute_full(task, config)

        # Act
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                insert_job(db, _job("ready-a"))
                update_job_score(db, "ready-a", 88, "good match")
                update_job_status(db, "ready-a", "ready")
            finally:
                db.close()
            server.set_base_dir(base_dir)

            with patch.object(server, "_execute_collect", side_effect=fake_collect), \
                 patch.object(server, "_execute_deliver", side_effect=fake_deliver), \
                 patch.object(server, "_execute_monitor", side_effect=fake_monitor):
                task = runner.start("full", {})
                for _ in range(50):
                    running_task = runner._tasks[task["id"]]
                    confirmation_event = running_task.context.get("confirmation_event")
                    if isinstance(confirmation_event, Event):
                        running_task.context["confirmed_job_ids"] = ["ready-a", "ready-b"]
                        confirmation_event.set()
                        break
                    time.sleep(0.01)
                runner.wait(timeout=1)

        # Assert
        self.assertEqual(calls, ["collect", ("deliver", ["ready-a", "ready-b"]), "monitor"])

    def test_web_api_workbench_reject_marks_selected_ready_jobs_rejected(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                insert_job(db, _job("reject-a"))
                update_job_score(db, "reject-a", 82, "good match")
                update_job_status(db, "reject-a", "ready")

                insert_job(db, _job("reject-b"))
                update_job_score(db, "reject-b", 72, "ok match")
                update_job_status(db, "reject-b", "ready")
            finally:
                db.close()
            server.set_base_dir(base_dir)

            body = json.dumps({"job_ids": ["reject-a", "reject-b"]}).encode("utf-8")
            status_headers = {}

            def start_response(status, headers, exc_info=None):
                status_headers["status"] = status
                status_headers["headers"] = dict(headers)

            environ = {
                "REQUEST_METHOD": "POST",
                "PATH_INFO": "/api/workbench/reject",
                "QUERY_STRING": "",
                "CONTENT_LENGTH": str(len(body)),
                "CONTENT_TYPE": "application/json",
                "SERVER_NAME": "127.0.0.1",
                "SERVER_PORT": "8686",
                "wsgi.version": (1, 0),
                "wsgi.url_scheme": "http",
                "wsgi.input": io.BytesIO(body),
                "wsgi.errors": io.StringIO(),
                "wsgi.multithread": False,
                "wsgi.multiprocess": False,
                "wsgi.run_once": False,
            }

            # Act
            response_body = b"".join(
                chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
                for chunk in server.app(environ, start_response)
            ).decode("utf-8")

            verify_db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                statuses = {
                    row["id"]: row["status"]
                    for row in verify_db.execute(
                        "SELECT id, status FROM jobs WHERE id IN ('reject-a', 'reject-b')"
                    ).fetchall()
                }
                history_actions = [
                    dict(row)
                    for row in verify_db.execute(
                        "SELECT job_id, action, detail FROM history ORDER BY id"
                    ).fetchall()
                ]
            finally:
                verify_db.close()

        # Assert
        self.assertTrue(status_headers["status"].startswith("200"), response_body)
        self.assertEqual(json.loads(response_body), {"success": True, "count": 2})
        self.assertEqual(statuses, {"reject-a": "rejected", "reject-b": "rejected"})
        self.assertEqual(
            history_actions,
            [
                {"job_id": "reject-a", "action": "rejected", "detail": "Web Dashboard 放弃投递"},
                {"job_id": "reject-b", "action": "rejected", "detail": "Web Dashboard 放弃投递"},
            ],
        )

    def test_web_api_workbench_reject_removes_jobs_from_pending_confirmation(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                insert_job(db, _job("reject-visible"))
                update_job_score(db, "reject-visible", 82, "good match")
                update_job_status(db, "reject-visible", "ready")
            finally:
                db.close()
            server.set_base_dir(base_dir)

            body = json.dumps({"job_ids": ["reject-visible"]}).encode("utf-8")
            status_headers = {}

            def start_response(status, headers, exc_info=None):
                status_headers["status"] = status
                status_headers["headers"] = dict(headers)

            environ = {
                "REQUEST_METHOD": "POST",
                "PATH_INFO": "/api/workbench/reject",
                "QUERY_STRING": "",
                "CONTENT_LENGTH": str(len(body)),
                "CONTENT_TYPE": "application/json",
                "SERVER_NAME": "127.0.0.1",
                "SERVER_PORT": "8686",
                "wsgi.version": (1, 0),
                "wsgi.url_scheme": "http",
                "wsgi.input": io.BytesIO(body),
                "wsgi.errors": io.StringIO(),
                "wsgi.multithread": False,
                "wsgi.multiprocess": False,
                "wsgi.run_once": False,
            }

            # Act
            response_body = b"".join(
                chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
                for chunk in server.app(environ, start_response)
            ).decode("utf-8")
            workbench_status, _, workbench_body = self._request("/api/workbench")

        # Assert
        self.assertTrue(status_headers["status"].startswith("200"), response_body)
        self.assertTrue(workbench_status.startswith("200"), workbench_body)
        self.assertEqual(json.loads(workbench_body)["pending_confirmation"], [])

    def test_web_api_resume_delete_only_detaches_config_and_keeps_master_resume_file(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            resume_path = base_dir / "data" / "resumes" / "_AI_Homepage.md"
            resume_path.parent.mkdir(parents=True, exist_ok=True)
            resume_path.write_text("# 主简历\n\n完整事实库，不能删减。\n", encoding="utf-8")
            (base_dir / "config.yaml").write_text(
                yaml.dump({"profile": {"resume_path": str(resume_path)}}, allow_unicode=True),
                encoding="utf-8",
            )
            server.set_base_dir(base_dir)

            # Act
            status, _, body = self._request("/api/resume", method="DELETE")
            config = yaml.safe_load((base_dir / "config.yaml").read_text(encoding="utf-8"))

            # Assert
            self.assertTrue(status.startswith("200"), body)
            self.assertEqual(json.loads(body), {"success": True})
            self.assertTrue(resume_path.exists())
            self.assertEqual(config["profile"]["resume_path"], "")

    def test_web_api_avatar_delete_only_detaches_config_and_keeps_image(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            avatar_path = base_dir / "data" / "profile" / "avatar.png"
            avatar_path.parent.mkdir(parents=True, exist_ok=True)
            avatar_path.write_bytes(b"\x89PNG\r\n\x1a\nportrait")
            (base_dir / "config.yaml").write_text(
                yaml.dump({"profile": {"avatar_path": str(avatar_path)}}, allow_unicode=True),
                encoding="utf-8",
            )
            server.set_base_dir(base_dir)

            # Act
            status, _, body = self._request("/api/avatar", method="DELETE")
            config = yaml.safe_load((base_dir / "config.yaml").read_text(encoding="utf-8"))

            # Assert
            self.assertTrue(status.startswith("200"), body)
            self.assertEqual(json.loads(body), {"success": True})
            self.assertTrue(avatar_path.exists())
            self.assertEqual(config["profile"]["avatar_path"], "")

    def test_web_api_history_dismiss_reply_adds_dismissed_history_without_rejecting_job(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                insert_job(db, _job("reply-dismiss"))
                update_job_status(db, "reply-dismiss", "sent")
                add_history(db, "reply-dismiss", "reply_pending", "AI建议回复")
                history_id = db.execute(
                    "SELECT id FROM history WHERE job_id = ? AND action = ?",
                    ("reply-dismiss", "reply_pending"),
                ).fetchone()["id"]
            finally:
                db.close()
            server.set_base_dir(base_dir)

            body = b"{}"
            status_headers = {}

            def start_response(status, headers, exc_info=None):
                status_headers["status"] = status
                status_headers["headers"] = dict(headers)

            environ = {
                "REQUEST_METHOD": "POST",
                "PATH_INFO": f"/api/history/{history_id}/dismiss",
                "QUERY_STRING": "",
                "CONTENT_LENGTH": str(len(body)),
                "CONTENT_TYPE": "application/json",
                "SERVER_NAME": "127.0.0.1",
                "SERVER_PORT": "8686",
                "wsgi.version": (1, 0),
                "wsgi.url_scheme": "http",
                "wsgi.input": io.BytesIO(body),
                "wsgi.errors": io.StringIO(),
                "wsgi.multithread": False,
                "wsgi.multiprocess": False,
                "wsgi.run_once": False,
            }

            # Act
            response_body = b"".join(
                chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
                for chunk in server.app(environ, start_response)
            ).decode("utf-8")

            verify_db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                job_status = verify_db.execute(
                    "SELECT status FROM jobs WHERE id = ?", ("reply-dismiss",)
                ).fetchone()["status"]
                history_actions = [
                    dict(row)
                    for row in verify_db.execute(
                        "SELECT job_id, action, detail FROM history ORDER BY id"
                    ).fetchall()
                ]
            finally:
                verify_db.close()

        # Assert
        self.assertTrue(status_headers["status"].startswith("200"), response_body)
        self.assertEqual(json.loads(response_body), {"success": True})
        self.assertEqual(job_status, "sent")
        self.assertEqual(history_actions[0], {"job_id": "reply-dismiss", "action": "reply_pending", "detail": "AI建议回复"})
        self.assertEqual(history_actions[1]["job_id"], "reply-dismiss")
        self.assertEqual(history_actions[1]["action"], "reply_dismissed")
        self.assertIn("Web Dashboard 放弃回复建议", history_actions[1]["detail"])

    def test_web_api_history_reply_starts_a_confirmed_reply_task(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                insert_job(db, _job("reply-confirm"))
                update_job_status(db, "reply-confirm", "sent")
                add_history(db, "reply-confirm", "reply_pending", "AI建议回复")
                history_id = db.execute(
                    "SELECT id FROM history WHERE job_id = ? AND action = ?",
                    ("reply-confirm", "reply_pending"),
                ).fetchone()["id"]
            finally:
                db.close()
            server.set_base_dir(base_dir)
            runner = WorkbenchTaskRunner({"reply": lambda task, config: None})

            body = json.dumps({"message": "已手动回复 HR"}).encode("utf-8")
            status_headers = {}

            def start_response(status, headers, exc_info=None):
                status_headers["status"] = status
                status_headers["headers"] = dict(headers)

            environ = {
                "REQUEST_METHOD": "POST",
                "PATH_INFO": f"/api/history/{history_id}/reply",
                "QUERY_STRING": "",
                "CONTENT_LENGTH": str(len(body)),
                "CONTENT_TYPE": "application/json",
                "SERVER_NAME": "127.0.0.1",
                "SERVER_PORT": "8686",
                "wsgi.version": (1, 0),
                "wsgi.url_scheme": "http",
                "wsgi.input": io.BytesIO(body),
                "wsgi.errors": io.StringIO(),
                "wsgi.multithread": False,
                "wsgi.multiprocess": False,
                "wsgi.run_once": False,
            }

            # Act
            with patch.object(server, "task_runner", runner):
                response_body = b"".join(
                    chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
                    for chunk in server.app(environ, start_response)
                ).decode("utf-8")

            verify_db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                history_actions = [
                    dict(row)
                    for row in verify_db.execute(
                        "SELECT job_id, action, detail FROM history ORDER BY id"
                    ).fetchall()
                ]
                job_status = verify_db.execute(
                    "SELECT status FROM jobs WHERE id = ?", ("reply-confirm",)
                ).fetchone()["status"]
            finally:
                verify_db.close()

        # Assert
        self.assertTrue(status_headers["status"].startswith("200"), response_body)
        reply_task = json.loads(response_body)
        self.assertEqual(reply_task["mode"], "reply")
        self.assertIn(reply_task["status"], {"running", "completed"})
        self.assertEqual(job_status, "sent")
        self.assertEqual(history_actions[0], {"job_id": "reply-confirm", "action": "reply_pending", "detail": "AI建议回复"})
        self.assertEqual(len(history_actions), 1)
        return
        self.assertIn("已手动回复 HR", history_actions[1]["detail"])

    def test_web_api_unresolved_count_includes_resume_failures_and_excludes_resolved_rows(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                insert_job(db, _job("reply-open"))
                insert_job(db, _job("reply-closed"))
                insert_job(db, _job("resume-failed-open"))
                insert_job(db, _job("resume-failed-resolved"))
                add_history(db, "reply-open", "reply_pending", "AI建议回复")
                add_history(db, "reply-closed", "reply_pending", "AI建议回复")
                add_history(db, "reply-closed", "reply_dismissed", "Web Dashboard 放弃回复建议")
                add_history(db, "resume-failed-open", "resume_failed", "定制简历生成失败")
                add_history(db, "resume-failed-resolved", "resume_failed", "定制简历生成失败")
                add_history(db, "resume-failed-resolved", "needs_resume", "后来已成功生成")
            finally:
                db.close()
            server.set_base_dir(base_dir)

            # Act
            status, _, body = self._request("/api/history/unresolved-replies/count")

        # Assert
        self.assertTrue(status.startswith("200"), body)
        self.assertEqual(json.loads(body), {"count": 2})

    def test_web_api_history_can_include_unresolved_resume_failures_outside_recent_limit(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                insert_job(db, _job("resume-failed-open"))
                insert_job(db, _job("resume-failed-resolved"))
                insert_job(db, _job("recent-job"))
                add_history(db, "resume-failed-open", "resume_failed", "仍需处理")
                add_history(db, "resume-failed-resolved", "resume_failed", "旧失败")
                add_history(db, "resume-failed-resolved", "resume_sent", "后来已成功")
                add_history(db, "recent-job", "sent", "最近记录")
            finally:
                db.close()
            server.set_base_dir(base_dir)

            # Act
            status, _, body = self._request("/api/history?limit=1&include_unresolved=1")

        # Assert
        self.assertTrue(status.startswith("200"), body)
        payload = json.loads(body)
        self.assertEqual(
            {(item["job_id"], item["action"]) for item in payload},
            {("recent-job", "sent"), ("resume-failed-open", "resume_failed")},
        )

    def test_web_api_history_exposes_structured_failure_reason_and_resolution_state(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                insert_job(db, _job("resume-failed-detail"))
                add_history(
                    db,
                    "resume-failed-detail",
                    "resume_failed",
                    json.dumps(
                        {
                            "schema": "resume_failed.v2",
                            "hr_question": "请发一份简历。",
                            "ai_reply": "",
                            "system_reason": "事实完整性校验失败：新增了 50%",
                            "conversation_tail": [],
                        },
                        ensure_ascii=False,
                    ),
                )
            finally:
                db.close()
            server.set_base_dir(base_dir)

            # Act
            status, _, body = self._request("/api/history?limit=10&include_unresolved=1")
            unresolved_item = json.loads(body)[0]

            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                db.execute(
                    "UPDATE jobs SET resume_path = ? WHERE id = ?",
                    ("/tmp/generated.md", "resume-failed-detail"),
                )
                db.commit()
            finally:
                db.close()
            _, _, resolved_body = self._request("/api/history?limit=10&include_unresolved=1")
            resolved_item = json.loads(resolved_body)[0]

        # Assert
        self.assertTrue(status.startswith("200"), body)
        self.assertEqual(unresolved_item["detail_payload"]["hr_question"], "请发一份简历。")
        self.assertEqual(
            unresolved_item["detail_payload"]["system_reason"],
            "事实完整性校验失败：新增了 50%",
        )
        self.assertFalse(unresolved_item["resolved"])
        self.assertTrue(resolved_item["resolved"])
        self.assertEqual(resolved_item["resume_path"], "/tmp/generated.md")

    def _post_json(self, path: str, payload: dict):
        """POST JSON 到指定路由，返回 (status, headers, body_text)。"""
        body = json.dumps(payload).encode("utf-8")
        status_headers = {}

        def start_response(status, headers, exc_info=None):
            status_headers["status"] = status
            status_headers["headers"] = dict(headers)

        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": path,
            "QUERY_STRING": "",
            "CONTENT_LENGTH": str(len(body)),
            "CONTENT_TYPE": "application/json",
            "SERVER_NAME": "127.0.0.1",
            "SERVER_PORT": "8686",
            "wsgi.version": (1, 0),
            "wsgi.url_scheme": "http",
            "wsgi.input": io.BytesIO(body),
            "wsgi.errors": io.StringIO(),
            "wsgi.multithread": False,
            "wsgi.multiprocess": False,
            "wsgi.run_once": False,
        }
        text = b"".join(
            chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
            for chunk in server.app(environ, start_response)
        ).decode("utf-8")
        return status_headers["status"], status_headers["headers"], text

    def test_replies_generate_returns_ai_reply_for_pasted_hr_message(self):
        """粘贴 HR 消息 → 调用 AI 生成回复语，返回文本，不写库。"""
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            (base_dir / "resume.md").write_text("# Resume", encoding="utf-8")
            (base_dir / "config.yaml").write_text(
                yaml.dump(
                    {
                        "profile": {"resume_path": str(base_dir / "resume.md")},
                        "search": {"keywords": ["AI"]},
                        "ai": {"api_key": "test-key"},
                    },
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )
            server.set_base_dir(base_dir)

            with patch(
                "bosshunter.executor.monitor._generate_reply_from_text",
                return_value="可以的，我整理后发您。",
            ) as gen:
                status, _headers, body = self._post_json(
                    "/api/replies/generate",
                    {"hr_message": "方便发一份你的简历吗？"},
                )

        self.assertTrue(status.startswith("200"), body)
        self.assertEqual(json.loads(body), {"message": "可以的，我整理后发您。"})
        # 确认传入了粘贴的 HR 消息
        args, _kwargs = gen.call_args
        self.assertEqual(args[0], "方便发一份你的简历吗？")

    def test_replies_generate_passes_job_context_when_job_id_given(self):
        """提供 job_id 时读取该岗位 title/company 作为上下文。"""
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                insert_job(db, _job("ctx-job"))
            finally:
                db.close()
            (base_dir / "resume.md").write_text("# Resume", encoding="utf-8")
            (base_dir / "config.yaml").write_text(
                yaml.dump(
                    {
                        "profile": {"resume_path": str(base_dir / "resume.md")},
                        "search": {"keywords": ["AI"]},
                        "ai": {"api_key": "test-key"},
                    },
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )
            server.set_base_dir(base_dir)

            with patch(
                "bosshunter.executor.monitor._generate_reply_from_text",
                return_value="好的",
            ) as gen:
                status, _headers, body = self._post_json(
                    "/api/replies/generate",
                    {"hr_message": "聊聊？", "job_id": "ctx-job"},
                )

        self.assertTrue(status.startswith("200"), body)
        args, _kwargs = gen.call_args
        # args = (hr_message, job, config, conversation_history)
        job_arg = args[1]
        self.assertEqual(job_arg["title"], "Product Manager")
        self.assertEqual(job_arg["company"], "Example")

    def test_replies_generate_rejects_empty_hr_message(self):
        """hr_message 为空 → 400。"""
        with tempfile.TemporaryDirectory() as tmp:
            server.set_base_dir(Path(tmp))
            status, _headers, body = self._post_json(
                "/api/replies/generate", {"hr_message": "   "}
            )

        self.assertTrue(status.startswith("400"), body)
        self.assertIn("error", json.loads(body))

    def test_greetings_generate_returns_greeting_for_pasted_jd(self):
        """粘贴 JD → 调用 AI 生成招呼语，返回文本，不写库。"""
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            (base_dir / "resume.md").write_text("# Resume", encoding="utf-8")
            (base_dir / "config.yaml").write_text(
                yaml.dump(
                    {
                        "profile": {"resume_path": str(base_dir / "resume.md")},
                        "search": {"keywords": ["AI"]},
                        "ai": {"api_key": "test-key"},
                    },
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )
            server.set_base_dir(base_dir)

            with patch(
                "bosshunter.ai.greeter._generate_with_deterministic_retry",
                return_value="你们在把 LangGraph 落到订单问数这件事很有意思。",
            ) as gen:
                status, _headers, body = self._post_json(
                    "/api/greetings/generate",
                    {
                        "title": "AI 后端工程师",
                        "company": "示例公司",
                        "salary": "25-40K",
                        "city": "广州",
                        "jd": "负责 LLM 应用架构，熟悉 LangGraph 与知识库。",
                    },
                )

        self.assertTrue(status.startswith("200"), body)
        self.assertEqual(
            json.loads(body),
            {"message": "你们在把 LangGraph 落到订单问数这件事很有意思。"},
        )
        # 确认构造的 job dict 把粘贴的字段带进了生成器
        args, _kwargs = gen.call_args
        job_arg = args[0]
        self.assertEqual(job_arg["title"], "AI 后端工程师")
        self.assertEqual(job_arg["company"], "示例公司")
        self.assertEqual(job_arg["salary"], "25-40K")
        self.assertEqual(job_arg["city"], "广州")
        self.assertEqual(job_arg["jd"], "负责 LLM 应用架构，熟悉 LangGraph 与知识库。")

    def test_greetings_generate_with_review_uses_review_path(self):
        """review=true → 走带 AI 自检循环的生成路径。"""
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            (base_dir / "resume.md").write_text("# Resume", encoding="utf-8")
            (base_dir / "config.yaml").write_text(
                yaml.dump(
                    {
                        "profile": {"resume_path": str(base_dir / "resume.md")},
                        "search": {"keywords": ["AI"]},
                        "ai": {"api_key": "test-key"},
                    },
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )
            server.set_base_dir(base_dir)

            with patch(
                "bosshunter.ai.greeter._generate_for_job_with_review",
                return_value=("", "好的招呼语"),
            ) as gen:
                status, _headers, body = self._post_json(
                    "/api/greetings/generate",
                    {"jd": "负责 LLM 应用架构。", "review": True},
                )

        self.assertTrue(status.startswith("200"), body)
        self.assertEqual(json.loads(body), {"message": "好的招呼语"})
        gen.assert_called_once()

    def test_greetings_generate_rejects_empty_jd(self):
        """jd 为空 → 400。"""
        with tempfile.TemporaryDirectory() as tmp:
            server.set_base_dir(Path(tmp))
            status, _headers, body = self._post_json(
                "/api/greetings/generate", {"jd": "   "}
            )

        self.assertTrue(status.startswith("400"), body)
        self.assertIn("error", json.loads(body))

    def test_greetings_generate_returns_502_when_ai_fails(self):
        """AI 生成失败返回 None → 502。"""
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            (base_dir / "resume.md").write_text("# Resume", encoding="utf-8")
            (base_dir / "config.yaml").write_text(
                yaml.dump(
                    {
                        "profile": {"resume_path": str(base_dir / "resume.md")},
                        "search": {"keywords": ["AI"]},
                        "ai": {"api_key": "test-key"},
                    },
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )
            server.set_base_dir(base_dir)

            with patch(
                "bosshunter.ai.greeter._generate_with_deterministic_retry",
                return_value=None,
            ):
                status, _headers, body = self._post_json(
                    "/api/greetings/generate", {"jd": "负责 LLM 应用架构。"}
                )

        self.assertTrue(status.startswith("502"), body)
        self.assertIn("error", json.loads(body))

    def test_api_jobs_returns_paginated_envelope_with_global_counts(self):
        """岗位池接口返回分页信封 {items,total,status_counts,...}，超 100 条也能分页取到。

        回归：原先裸数组 + 硬编码 limit=100，导致新采集的低分岗位落在 100 名外
        永远看不到；现在改为分页响应，total 反映全量、items 反映当前页。
        """
        # Arrange：插 12 条 job（11 pending + 1 scored），其中一条补全了企业信息。
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                for i in range(11):
                    insert_job(db, _job(f"pending-{i}"))
                insert_job(db, _job("scored-0"))
                update_job_score(db, "scored-0", 90, "great")
                update_job_status(db, "scored-0", "scored")
                # 只给 scored-0 补全企业信息，其余 11 条都缺。
                update_job_enrichment(db, {
                    "id": "scored-0",
                    "district": "海淀",
                    "company_size": "100-499人",
                    "company_industry": "互联网",
                    "company_background": "知名互联网公司",
                    "registered_capital": "5000万人民币",
                })
            finally:
                db.close()
            server.set_base_dir(base_dir)

            # Act：limit=10 应只返回 10 条，但 total 反映全部 12 条。
            status, headers, body = self._request("/api/jobs?limit=10")

        # Assert
        self.assertTrue(status.startswith("200"), body)
        self.assertIn("application/json", headers["Content-Type"])
        payload = json.loads(body)
        # 分页信封结构
        self.assertIn("items", payload)
        self.assertIn("total", payload)
        self.assertIn("status_counts", payload)
        self.assertIn("pending_count", payload)
        self.assertIn("missing_company_info_count", payload)
        # 分页：items 受 limit 约束，total 反映全量
        self.assertEqual(len(payload["items"]), 10)
        self.assertEqual(payload["total"], 12)
        # 全量按状态计数（不受 limit 影响）
        self.assertEqual(payload["status_counts"], {"pending": 11, "scored": 1})
        self.assertEqual(payload["pending_count"], 11)
        # 11 条 pending 缺企业信息，scored-0 已补全 → 缺失 11 条
        self.assertEqual(payload["missing_company_info_count"], 11)

    def test_api_jobs_status_filter_returns_filtered_total(self):
        """带 ?status=scored 时 total 只计 scored 岗位，status_counts 仍是全量。"""
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                insert_job(db, _job("a"))
                insert_job(db, _job("b"))
                update_job_score(db, "b", 88, "ok")
                update_job_status(db, "b", "scored")
            finally:
                db.close()
            server.set_base_dir(base_dir)

            # Act
            status, _headers, body = self._request("/api/jobs?status=scored")

        # Assert
        self.assertTrue(status.startswith("200"), body)
        payload = json.loads(body)
        # 筛选后 items 和 total 都只针对 scored
        self.assertEqual(payload["total"], 1)
        self.assertEqual([job["id"] for job in payload["items"]], ["b"])
        # status_counts 不受筛选影响，仍是全量
        self.assertEqual(payload["status_counts"], {"pending": 1, "scored": 1})


if __name__ == "__main__":
    unittest.main()
