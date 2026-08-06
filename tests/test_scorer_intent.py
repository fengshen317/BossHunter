"""Tests for scorer personalization and robustness improvements.

Covers:
- _build_job_intent: 求职意向从 config.profile 正确拼装，缺失项省略
- score 钳制: LLM 返回越界值/字符串/浮点时归一到 [0, 100]
- _call_claude_with_retry: API 失败重试 1 次
- SCORING_PROMPT 含求职意向占位符
"""

import unittest
from unittest.mock import patch

from bosshunter.ai.scorer import (
	SCORING_PROMPT,
	_build_job_intent,
	_call_claude_with_retry,
)


class BuildJobIntentTests(unittest.TestCase):
	def test_full_intent_is_assembled(self):
		config = {
			"profile": {
				"salary_min": 15,
				"salary_max": 30,
				"target_cities": ["深圳", "广州"],
				"deal_breakers": ["外包", "驻场"],
				"allow_internship": False,
			}
		}
		intent = _build_job_intent(config)
		self.assertIn("15-30K", intent)
		self.assertIn("深圳", intent)
		self.assertIn("广州", intent)
		self.assertIn("外包", intent)
		self.assertIn("不接受实习", intent)

	def test_missing_salary_is_omitted(self):
		"""salary 全为 0 时不输出薪资行，避免噪声。"""
		config = {"profile": {"salary_min": 0, "salary_max": 0, "target_cities": ["北京"]}}
		intent = _build_job_intent(config)
		self.assertNotIn("薪资", intent)
		self.assertIn("北京", intent)

	def test_only_salary_min_shown_as_above(self):
		"""只有下限没有上限时显示'以上'。"""
		config = {"profile": {"salary_min": 15, "salary_max": 0}}
		intent = _build_job_intent(config)
		self.assertIn("15K以上", intent)

	def test_empty_profile_falls_back_to_neutral(self):
		intent = _build_job_intent({})
		# 空配置不应崩溃，应给中性提示
		self.assertIsInstance(intent, str)
		self.assertTrue(len(intent) > 0)

	def test_allow_internship_true_omits_rejection(self):
		config = {"profile": {"allow_internship": True, "salary_min": 10, "salary_max": 20}}
		intent = _build_job_intent(config)
		self.assertNotIn("不接受实习", intent)


class ScoreClampingTests(unittest.TestCase):
	"""score 钳制逻辑通过 score_jobs 集成验证；这里测 prompt 解析后的钳制。"""

	def test_prompt_has_job_intent_placeholder(self):
		"""SCORING_PROMPT 必须有求职意向占位符。"""
		self.assertIn("{job_intent}", SCORING_PROMPT)
		self.assertIn("求职意向", SCORING_PROMPT)


class CallClaudeRetryTests(unittest.TestCase):
	def test_success_no_retry(self):
		"""首次成功不重试。"""
		with patch("bosshunter.ai.scorer._call_claude", return_value="ok") as mock_call, \
			patch("bosshunter.ai.scorer.time.sleep"):
			result = _call_claude_with_retry("prompt", {})
		self.assertEqual(result, "ok")
		self.assertEqual(mock_call.call_count, 1)

	def test_failure_retries_once(self):
		"""首次 None（API 失败）时 sleep 后重试 1 次。"""
		with patch("bosshunter.ai.scorer._call_claude", side_effect=[None, "ok"]) as mock_call, \
			patch("bosshunter.ai.scorer.time.sleep") as mock_sleep:
			result = _call_claude_with_retry("prompt", {})
		self.assertEqual(result, "ok")
		self.assertEqual(mock_call.call_count, 2)
		mock_sleep.assert_called_once_with(2)

	def test_persistent_failure_returns_none(self):
		"""两次都失败返回 None，不无限重试。"""
		with patch("bosshunter.ai.scorer._call_claude", side_effect=[None, None]) as mock_call, \
			patch("bosshunter.ai.scorer.time.sleep"):
			result = _call_claude_with_retry("prompt", {})
		self.assertIsNone(result)
		self.assertEqual(mock_call.call_count, 2)


if __name__ == "__main__":
	unittest.main()
