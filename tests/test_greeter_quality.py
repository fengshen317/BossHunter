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
		greeting = "您好，看到贵司在招聘这个岗位，我对这个岗位很感兴趣，相信我能胜任，希望有机会进一步沟通？"

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
		greeting = "您好，这个岗位同时提到多轮交互和评测体系，我之前用 LangGraph 做过订单问数工作流，处理过状态流转和结果可追溯。想了解一下，团队现阶段更侧重效果评测，还是生产稳定性？"

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
		self.assertIn("效果评测", GREETING_PROMPT)
		self.assertIn("必须以问号结束", GREETING_PROMPT)

	def test_prompt_picks_project_by_jd_not_strongest(self):
		"""prompt 要求按 JD 方向选项目，解决同质化。"""
		self.assertIn("最相关的 1 条可核验经历", GREETING_PROMPT)
		self.assertIn("不要总用同一个项目", GREETING_PROMPT)

	def test_long_greeting_is_not_hard_truncated(self):
		"""超长招呼语应由质量门禁要求重写，不能硬切成看似完整的残句。"""
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
		self.assertEqual(result, long_greeting)
		self.assertIn("长度不在 45-120 字范围内", _quality_issues(result))

	def test_length_upper_bound_is_120(self):
		greeting = "正" * 121
		issues = _quality_issues(greeting)
		self.assertTrue(any("120" in i for i in issues))

	def test_rejects_message_without_question_or_greeting(self):
		greeting = "这个岗位同时提到多轮交互和评测体系，我做过订单问数工作流，处理过状态流转和结果可追溯。"
		issues = _quality_issues(greeting)
		self.assertIn("没有以“您好，”自然开场", issues)
		self.assertIn("没有以完整、具体的问题收尾", issues)


class DeterministicRetryTests(unittest.TestCase):
	"""评分阶段 with_review=False 路径的确定性质量校验。"""

	def test_clean_greeting_returned_without_retry(self):
		"""合规招呼语不触发重试。"""
		clean = "您好，这个岗位同时提到多轮交互和评测体系，我做过 LangGraph 订单问数工作流，处理过状态流转。想了解一下，团队目前更侧重效果评测，还是生产稳定性？"
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
		clean = "您好，这个岗位同时提到多轮交互和评测体系，我做过 LangGraph 订单问数工作流，处理过状态流转。想了解一下，团队目前更侧重效果评测，还是生产稳定性？"
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

	def test_persistent_failure_returns_none(self):
		"""重试后仍不合规时拒绝落库，避免发送残句或模板文案。"""
		banned = "您好，看到贵司在招人，我对这个岗位很感兴趣。"
		job = {"jd": "AI", "title": "AI", "company": "C", "salary": "20K", "city": "深圳"}
		with patch(
			"bosshunter.ai.greeter._generate_greeting_once",
			side_effect=[banned, banned],
		) as gen_once:
			result = _generate_with_deterministic_retry(job, "resume", {})
		self.assertIsNone(result)
		self.assertEqual(gen_once.call_count, 2)


if __name__ == "__main__":
	unittest.main()
