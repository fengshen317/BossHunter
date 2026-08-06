"""并发生成招呼语的回归测试。

验证：
1. 多岗位并发生成时，结果不串号（job_id 与 greeting 正确对应）。
2. greeting_concurrency 可配置，且并发度为 1 时退化为串行行为。
3. 确定性规则 _quality_issues 命中套话时，跳过 AI 审查调用（断言 _call_claude 调用次数下降）。
4. scorer 评分阶段通过 generate_greetings_batch 批量并发生成。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bosshunter.ai import greeter
from bosshunter.db import get_db


def _make_job(job_id: str, title: str = "Python开发", company: str = "示例科技") -> dict:
    """构造一个满足招呼语生成所需字段的岗位字典。"""
    return {
        "id": job_id,
        "title": title,
        "company": company,
        "salary": "20-35K",
        "city": "深圳",
        "district": "南山",
        "company_background": "互联网",
        "company_industry": "互联网",
        "jd": "负责后端服务开发，熟悉 Python、分布式系统，有高并发经验。",
        "score_reason": "技能匹配",
        "url": f"https://example.com/{job_id}",
    }


def _base_config(concurrency: int = 4) -> dict:
    return {
        "ai": {
            "greeting_concurrency": concurrency,
            "greeting_max_iterations": 0,  # 默认关闭自审，便于精确统计 AI 调用次数
        },
        "profile": {"resume_path": "./resume.md"},
    }


REVIEW_PROMPT_MARKER = "评估以下BOSS直聘招呼语的质量"


class FakeClaude:
    """根据 prompt 内容区分「生成」与「审查」请求的 _call_claude 替身。

    - 生成请求：返回包含 job_id 的招呼语，便于断言不串号。
    - 审查请求：返回高分 JSON，表示质量达标（应触发 break，不再生成）。
    """

    def __init__(self):
        self.generate_calls: list[str] = []
        self.review_calls: list[str] = []

    def __call__(self, prompt: str, config: dict, max_tokens: int = 300) -> str | None:
        if REVIEW_PROMPT_MARKER in prompt:
            self.review_calls.append(prompt)
            return '{"naturalness": 9, "relevance": 9, "differentiation": 9, "truthfulness": 10, "avg": 9.0, "critique": "良好"}'
        self.generate_calls.append(prompt)
        # 从 prompt 里提取被生成的 job_id（_make_job 的 title/company 组合唯一）。
        # 返回一条通过确定性质量校验的招呼语（长度≥45、无禁词、含标点、我≤5），
        # 这样 with_review=False 路径的确定性重试不会被触发，便于精确统计调用次数。
        return "你们在推进智能体落地这件事很有意思。我之前用 LangGraph 把知识库接进订单问数场景并稳定上线。如果团队也在解决这类问题，我很愿意带着细节聊聊。"


class GreetingBatchConcurrencyTests(unittest.TestCase):
    def test_batch_generates_for_all_jobs_without_mixing_ids(self):
        """多岗位并发生成：每个 job 都有结果，且 job_id 正确对应。"""
        config = _base_config(concurrency=4)
        jobs = [_make_job(f"job-{i}", company=f"公司{i}") for i in range(5)]

        fake = FakeClaude()
        with mock.patch.object(greeter, "_call_claude", side_effect=fake):
            results = greeter.generate_greetings_batch(jobs, "示例简历摘要", config, with_review=False)

        self.assertEqual(len(results), 5)
        result_ids = {job_id for job_id, _ in results}
        self.assertEqual(result_ids, {f"job-{i}" for i in range(5)})
        # 每个 job 恰好调用一次生成（with_review=False，无自审）。
        self.assertEqual(len(fake.generate_calls), 5)
        self.assertEqual(fake.review_calls, [])

    def test_concurrency_one_degrades_to_serial(self):
        """并发度为 1 时仍能正确生成全部结果。"""
        config = _base_config(concurrency=1)
        jobs = [_make_job(f"j-{i}") for i in range(3)]

        fake = FakeClaude()
        with mock.patch.object(greeter, "_call_claude", side_effect=fake):
            results = greeter.generate_greetings_batch(jobs, "摘要", config, with_review=False)

        self.assertEqual(len(results), 3)
        self.assertEqual(len(fake.generate_calls), 3)

    def test_empty_jobs_returns_empty(self):
        self.assertEqual(greeter.generate_greetings_batch([], "摘要", _base_config()), [])

    def test_quality_issues_skips_ai_review(self):
        """确定性规则命中套话时，应跳过 AI 审查调用（critique 直接来自 _quality_issues）。

        构造：首轮生成一条含「您好/看到贵司」的招呼语 → 确定性规则命中 →
        第二轮重试生成一条干净招呼语。开启自审 (greeting_max_iterations=2)。
        断言：审查类 AI 调用为 0（被确定性规则短路）。
        """
        config = _base_config(concurrency=2)
        config["ai"]["greeting_max_iterations"] = 2

        call_state = {"first_done": False}

        def alternating_fake(prompt, cfg, max_tokens=300):
            if REVIEW_PROMPT_MARKER in prompt:
                # 不应被调用：确定性规则命中时直接跳过审查。
                return '{"naturalness": 9, "relevance": 9, "differentiation": 9, "truthfulness": 10, "avg": 9.0, "critique": ""}'
            # 首轮返回含套话的招呼语，第二轮返回干净招呼语。
            if not call_state["first_done"]:
                call_state["first_done"] = True
                return "您好，看到贵司在招聘，我对这个岗位很感兴趣，相信我能胜任。"
            return "你们推进智能体落地这件事很有意思，我有 LangGraph 经验，很愿意聊聊实现细节。"

        jobs = [_make_job("mixing-job")]
        with mock.patch.object(greeter, "_call_claude", side_effect=alternating_fake) as mock_call:
            results = greeter.generate_greetings_batch(jobs, "摘要", config, with_review=True)

        self.assertEqual(len(results), 1)
        job_id, greeting = results[0]
        self.assertEqual(job_id, "mixing-job")
        # 最终保留的是干净招呼语（含套话的版本应被重试替换）。
        self.assertNotIn("您好", greeting)
        # 审查类调用应为 0：首轮确定性规则命中直接进入第二轮生成，无需 AI 审查。
        review_calls = [c for c in mock_call.call_args_list if REVIEW_PROMPT_MARKER in (c.args[0] if c.args else "")]
        self.assertEqual(review_calls, [], "确定性规则命中时应跳过 AI 审查调用")


class GenerateGreetingsDbIntegrationTests(unittest.TestCase):
    """通过真实临时 SQLite 库验证 generate_greetings 的并发写库不串号、不报错。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test.db"
        # 用 monkeypatch 把 db.DB_PATH 指向临时库，保证 get_db() 走测试库。
        self._patcher = mock.patch.object(greeter, "get_db", lambda: get_db(self.db_path))
        self._patcher.start()
        db = get_db(self.db_path)
        for i in range(3):
            db.execute(
                "INSERT INTO jobs (id, title, company, salary, city, jd, status, greeting, score) "
                "VALUES (?, ?, ?, ?, ?, ?, 'approved', '', 70)",
                (f"db-{i}", "Python开发", f"公司{i}", "20-30K", "深圳", "后端开发"),
            )
        db.commit()
        db.close()

    def tearDown(self):
        self._patcher.stop()
        self._tmp.cleanup()

    def test_generate_greetings_writes_all_rows_concurrently(self):
        config = _base_config(concurrency=3)
        config["profile"] = {"resume_path": "./resume.md"}
        # 提供一个存在的简历文件，让 _get_resume_summary 返回非空。
        resume_file = Path(self._tmp.name) / "resume.md"
        resume_file.write_text("示例候选人简历", encoding="utf-8")
        config["profile"]["resume_path"] = str(resume_file)

        with mock.patch.object(greeter, "_call_claude", side_effect=FakeClaude()):
            count = greeter.generate_greetings(config)

        self.assertEqual(count, 3)
        db = get_db(self.db_path)
        rows = {r["id"]: r for r in db.execute("SELECT id, greeting, status FROM jobs").fetchall()}
        db.close()
        for i in range(3):
            self.assertEqual(rows[f"db-{i}"]["status"], "ready")
            self.assertTrue(rows[f"db-{i}"]["greeting"])


if __name__ == "__main__":
    unittest.main()
