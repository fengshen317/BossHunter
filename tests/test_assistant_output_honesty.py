"""回归测试：Agent 输出界面不得伪造执行状态或连通性。

这些断言锁定的是「界面说的话必须能被后端事实支撑」这一条不变量。它们检查前端
源码而不是渲染结果，因此故意写得保守：只断言那些一旦被改回去就会重新产生
「说谎的状态」的语句。
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_SRC = ROOT / "src" / "bosshunter" / "web" / "frontend" / "src"
ASSISTANT_PAGE = FRONTEND_SRC / "pages" / "AIAssistantPage.tsx"
HEADER = FRONTEND_SRC / "components" / "layout" / "Header.tsx"
HEALTH_HOOK = FRONTEND_SRC / "hooks" / "useBackendHealth.ts"


class ExecutionStatusHonestyTests(unittest.TestCase):
	def setUp(self) -> None:
		# Arrange
		self.source = ASSISTANT_PAGE.read_text(encoding="utf-8")

	def test_missing_terminal_event_is_not_rewritten_as_completed(self) -> None:
		# 缺少终态事件的步骤必须标为「未收到终态」，不能谎报完成。
		# Act / Assert
		self.assertIn("status: 'incomplete' as ExecutionStatus", self.source)
		self.assertNotIn("status: 'completed' as ExecutionStatus } : event", self.source)

	def test_skipped_is_not_collapsed_into_completed(self) -> None:
		# 「跳过」意味着这一步没有执行，不能与「已完成」共用同一个对勾。
		# Act / Assert
		self.assertIn("if (values.some(status => status === 'skipped')) return 'skipped'", self.source)
		self.assertNotIn("every(status => status === 'completed' || status === 'skipped')", self.source)

	def test_terminal_states_carry_text_not_only_colour(self) -> None:
		# 状态不能只靠颜色表达：跳过 / 未收到终态 / 已取消都必须有文字。
		# Act / Assert
		self.assertIn("function statusNote(", self.source)
		self.assertIn("return '已跳过'", self.source)
		self.assertIn("return '未收到终态'", self.source)

	def test_cancelled_and_budget_exhausted_are_reported_in_the_summary(self) -> None:
		# Act / Assert
		self.assertIn("'已取消，部分步骤未执行'", self.source)
		self.assertIn("'预算耗尽，结果可能不完整'", self.source)
		self.assertIn("'本次未记录执行过程'", self.source)

	def test_evidence_domain_is_joined_on_declared_fields(self) -> None:
		# 证据域归属必须使用后端声明的字段，而不是字符串包含猜测。
		# Act / Assert
		self.assertIn("const executorDomain: Record<string, EvidenceDomain>", self.source)
		self.assertIn("step.arguments?.evidence_type", self.source)
		self.assertNotIn("result.executor.includes(", self.source)
		self.assertNotIn("step.id.includes(", self.source)


class ConnectivityHonestyTests(unittest.TestCase):
	def test_assistant_badge_is_backed_by_a_health_probe(self) -> None:
		# Arrange
		source = ASSISTANT_PAGE.read_text(encoding="utf-8")

		# Act / Assert
		self.assertIn("useBackendHealth", source)
		self.assertIn("后端未连接", source)
		self.assertNotIn("LangGraph 已连接", source)

	def test_header_indicator_is_backed_by_a_health_probe(self) -> None:
		# Arrange
		source = HEADER.read_text(encoding="utf-8")

		# Act / Assert
		self.assertIn("useBackendHealth", source)
		self.assertIn("后端离线", source)

	def test_health_hook_calls_the_health_endpoint(self) -> None:
		# Arrange
		source = HEALTH_HOOK.read_text(encoding="utf-8")

		# Act / Assert
		self.assertIn("fetch('/api/health')", source)
		self.assertIn("setInterval", source)

	def test_capability_list_is_not_hardcoded(self) -> None:
		# 右栏「本轮能力」必须反映本轮真实命中，而不是四行写死的绿点。
		# Arrange
		source = ASSISTANT_PAGE.read_text(encoding="utf-8")

		# Act / Assert
		self.assertIn("latestCapabilities", source)
		self.assertNotIn('label="PostgreSQL 岗位数据"', source)


class ApprovalCopyTests(unittest.TestCase):
	def setUp(self) -> None:
		# Arrange
		self.source = ASSISTANT_PAGE.read_text(encoding="utf-8")

	def test_pending_approval_copy_names_the_actual_object(self) -> None:
		# 待确认时必须说清在等什么，不能把只读数据查询也叫「回复草稿」。
		# Act / Assert
		self.assertIn("function approvalNoun(", self.source)
		self.assertIn("execute_readonly_sql: '只读数据查询'", self.source)
		self.assertNotIn("待确认的回复草稿", self.source)
		self.assertNotIn("请先完成回复确认", self.source)

	def test_resolved_approval_distinguishes_approve_from_reject(self) -> None:
		# 终态必须区分「已确认执行」和「已取消，未执行」，不能一律显示「已处理」。
		# Act / Assert
		self.assertIn("function approvalOutcomeChip(", self.source)
		self.assertIn("'已确认执行'", self.source)
		self.assertIn("'已取消，未执行'", self.source)
		self.assertNotIn("approvalActive ? '等待审批' : '已处理'", self.source)


if __name__ == "__main__":
	unittest.main()
