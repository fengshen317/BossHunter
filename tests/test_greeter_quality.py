import unittest
from unittest.mock import patch

from bosshunter.ai.greeter import (
	GREETING_PROMPT,
	_generate_greeting_once,
	_generate_with_deterministic_retry,
	_quality_issues,
)


class GreeterQualityTests(unittest.TestCase):
	def test_rejects_template_opening(self):
		greeting = "您好，看到贵司在招聘这个岗位，我对这个岗位很感兴趣，相信我能胜任，希望有机会进一步沟通。"

		self.assertIn("包含模板化开场或承诺", _quality_issues(greeting))

	def test_rejects_banned_phrase_variants(self):
		"""扩充后的禁词能拦住实测出现的套话变体。"""
		variants = [
			"你好，看到团队在推进这个项目，我很感兴趣。",
			"看到你们在做 AI Agent，我对该岗位很感兴趣。",
			"看到Anker在招人，希望能有机会进一步沟通。",
			"我对这个岗位很感兴趣，期待您的回复。",
		]
		for greeting in variants:
			with self.subTest(greeting=greeting):
				self.assertIn(
					"包含模板化开场或承诺",
					_quality_issues(greeting),
					msg=f"未拦住套话变体: {greeting}",
				)

	def test_accepts_concrete_natural_message(self):
		greeting = "你们在推进智能体落地这件事很有意思。我最近负责过 LangGraph 工作流开发，把知识库问答接进订单场景并持续迭代。如果团队也在解决这类问题，我很愿意带着实现细节聊聊。"

		self.assertEqual(_quality_issues(greeting), [])

	def test_prompt_requires_company_and_location_context(self):
		self.assertIn("公司背景", GREETING_PROMPT)
		self.assertIn("工作地点", GREETING_PROMPT)
		self.assertIn("可核验", GREETING_PROMPT)

	def test_prompt_includes_few_shot_examples(self):
		"""prompt 必须包含正反示例，降低 zero-shot 套话率。"""
		self.assertIn("参考示例", GREETING_PROMPT)
		self.assertIn("✅", GREETING_PROMPT)  # 正例标记
		self.assertIn("❌", GREETING_PROMPT)  # 反例标记
		# 反例必须是套话，正例必须含可核验数据
		self.assertIn("很感兴趣", GREETING_PROMPT)  # 反例内容
		self.assertIn("日均", GREETING_PROMPT)  # 正例的量化数据

	def test_prompt_picks_project_by_jd_not_strongest(self):
		"""prompt 要求按 JD 方向选项目，解决同质化。"""
		self.assertIn("JD 强调的方向", GREETING_PROMPT)
		self.assertIn("不必是最强项目", GREETING_PROMPT)

	def test_truncation_preserves_terminal_punctuation(self):
		"""超长招呼语截断后必须以标点结尾，不得悬空。"""
		# 构造一段超 160 字、结尾无句末标点的纯中文输入（模拟 LLM 输出）
		long_greeting = (
			"你们在把大模型落到制造业数据分析这件事很有意思，"
			"我之前负责过一个类似的系统，把通义千问和知识库接进生产报告生成，"
			"日均稳定产出二十多份报告服务一线员工，过程中踩了不少工作流稳定性的坑，"
			"比如并发请求时的限流重试和长文本分块策略，还有就是提示词漂移导致输出格式不一致，"
			"后来通过示例约束和结构化输出解决了，如果团队也在解决这类问题我也很愿意聊聊"
		)
		self.assertGreater(len(long_greeting), 160)  # 确认确实超长
		with patch("bosshunter.ai.greeter._call_claude", return_value=long_greeting):
			result = _generate_greeting_once(
				{"jd": "AI 工程师", "title": "AI", "company": "C", "salary": "20K", "city": "深圳"},
				"resume",
				{},
			)
		self.assertIsNotNone(result)
		self.assertLessEqual(len(result), 160)
		# 截断后必须以某个标点结尾（不能是悬空的逗号分句中间或无标点）
		self.assertIn(result[-1], "。！；，、", msg=f"截断后结尾无标点: ...{result[-10:]}")

	def test_length_upper_bound_is_160(self):
		"""长度上限从 150 提到 160，与截断阈值一致。"""
		greeting = "正" * 161
		issues = _quality_issues(greeting)
		self.assertTrue(any("160" in i for i in issues))


class DeterministicRetryTests(unittest.TestCase):
	"""评分阶段 with_review=False 路径的确定性质量校验。"""

	def test_clean_greeting_returned_without_retry(self):
		"""合规招呼语不触发重试。"""
		clean = "你们在推进智能体落地这件事很有意思。我负责过 LangGraph 工作流，把知识库接进订单问数场景。如果团队也解决这类问题，我很愿意带着细节聊聊。"
		job = {"jd": "AI", "title": "AI", "company": "C", "salary": "20K", "city": "深圳"}
		with patch(
			"bosshunter.ai.greeter._generate_greeting_once", return_value=clean
		) as gen_once:
			result = _generate_with_deterministic_retry(job, "resume", {})
		self.assertEqual(result, clean)
		self.assertEqual(gen_once.call_count, 1)

	def test_banned_greeting_triggers_one_retry(self):
		"""命中禁词时带 critique 重试一次。"""
		banned = "您好，看到贵司在招人，我对这个岗位很感兴趣，希望有机会沟通。"
		clean = "你们在推进智能体落地这件事很有意思。我负责过 LangGraph 工作流，把知识库接进订单问数场景。如果团队也解决这类问题，我很愿意带着细节聊聊。"
		job = {"jd": "AI", "title": "AI", "company": "C", "salary": "20K", "city": "深圳"}
		with patch(
			"bosshunter.ai.greeter._generate_greeting_once",
			side_effect=[banned, clean],
		) as gen_once:
			result = _generate_with_deterministic_retry(job, "resume", {})
		self.assertEqual(result, clean)
		self.assertEqual(gen_once.call_count, 2)
		# 第二次调用必须带 critique
		self.assertTrue(gen_once.call_args_list[1].kwargs.get("critique"))

	def test_persistent_failure_returns_best_effort(self):
		"""重试后仍不合规也返回结果，不丢数据。"""
		banned = "您好，看到贵司在招人，我对这个岗位很感兴趣。"
		job = {"jd": "AI", "title": "AI", "company": "C", "salary": "20K", "city": "深圳"}
		with patch(
			"bosshunter.ai.greeter._generate_greeting_once",
			side_effect=[banned, banned],
		) as gen_once:
			result = _generate_with_deterministic_retry(job, "resume", {})
		self.assertEqual(result, banned)  # 仍返回，不返回 None
		self.assertEqual(gen_once.call_count, 2)


if __name__ == "__main__":
	unittest.main()
