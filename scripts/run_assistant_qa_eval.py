"""通过 BossHunter Web API 自动评测问答意图、路由和安全边界。"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


HIGH_CONFIDENCE_ACTIONS = {
	"job_search",
	"job_score",
	"greeting_generate",
	"greeting_send",
	"reply_send",
	"reply_monitor",
	"pipeline_run",
	"complex_task",
}


@dataclass(frozen=True)
class EvalCase:
	"""单个或多轮问答评测用例。"""

	case_id: str
	turns: tuple[str, ...]
	expected_intents: tuple[str, ...]
	expected_routes: tuple[str, ...]
	expected_modes: tuple[str, ...] = ()
	expected_missing_fields: tuple[str, ...] = ()
	expected_approval: bool | None = None
	expected_tools: tuple[str, ...] = ()
	requires_local_data: bool = False


CASES: tuple[EvalCase, ...] = (
	EvalCase("Q01", ("你好",), ("general_question",), ("direct",), ("answer",)),
	EvalCase("Q02", ("什么是产品经理？",), ("general_question",), ("direct",), ("answer",)),
	EvalCase("Q03", ("这个岗位怎么样？",), ("general_question",), ("direct",)),
	EvalCase("Q04", ("我现在有哪些岗位？",), ("job_query",), ("capability",), expected_tools=("job_database",)),
	EvalCase("Q05", ("上海有哪些 Python 开发岗位？",), ("job_query", "job_search"), ("capability",)),
	EvalCase("Q06", ("查一下我收藏的高分岗位",), ("job_query",), ("capability",), expected_tools=("job_database",)),
	EvalCase("Q07", ("腾讯旗下有哪些业务？",), ("general_question",), ("direct",)),
	EvalCase("Q08", ("帮我查一下字节跳动最近的业务方向",), ("company_research",), ("capability",)),
	EvalCase("Q09", ("帮我分析一下这家公司值不值得去",), ("company_research",), ("clarification", "capability")),
	EvalCase(
		"Q10",
		("搜索上海的 AI 产品经理岗位",),
		("job_search",),
		("capability",),
		expected_approval=True,
		expected_tools=("collect_jobs",),
	),
	EvalCase("Q11", ("帮我搜索岗位",), ("job_search",), ("clarification",), expected_missing_fields=("keywords",)),
	EvalCase("Q12", ("帮我采集后端岗位",), ("job_search",), ("clarification", "capability")),
	EvalCase("Q13", ("给这几个岗位打分",), ("job_score",), ("clarification",), expected_missing_fields=("job_ids",)),
	EvalCase("Q14", ("帮我生成一段给 HR 的回复",), ("reply_draft",), ("clarification", "capability")),
	EvalCase("Q15", ("HR 问我有没有推荐经验，怎么回复？",), ("reply_draft",), ("capability",), expected_approval=True),
	EvalCase("Q16", ("帮我回复 HR：“您好，我有三年经验”",), ("reply_send",), ("clarification", "capability")),
	EvalCase("Q17", ("回复刚才那个 HR",), ("reply_send",), ("clarification",)),
	EvalCase(
		"Q18",
		("发送这几个岗位的招呼语",),
		("greeting_send",),
		("clarification", "capability"),
		requires_local_data=True,
	),
	EvalCase("Q19", ("帮我自动投递高分岗位",), ("pipeline_run",), ("planner",), expected_approval=True),
	EvalCase("Q20", ("搜索岗位并给我评分",), ("complex_task",), ("planner", "clarification")),
	EvalCase(
		"Q21",
		("搜索上海的 AI 产品经理岗位，筛选 80 分以上的，并生成招呼语",),
		("complex_task",),
		("planner",),
	),
	EvalCase(
		"Q22",
		("发送所有高分岗位的招呼语",),
		("greeting_send",),
		("clarification", "capability"),
		requires_local_data=True,
	),
	EvalCase("Q23", ("监听 HR 回复",), ("reply_monitor",), ("capability",), expected_approval=True),
	EvalCase("Q24", ("自动回复所有 HR 的问题",), ("reply_send", "reply_monitor"), ("clarification", "capability")),
	EvalCase("Q25", ("不用了，取消刚才的操作",), ("general_question",), ("direct",), ("cancel",)),
	EvalCase(
		"Q26",
		("帮我搜索岗位", "Python", "上海"),
		("job_search",),
		("capability", "clarification"),
	),
	EvalCase(
		"Q27",
		("帮我给岗位打分", "刚才列表里的前 5 个"),
		("job_score",),
		("capability", "clarification"),
		requires_local_data=True,
	),
	EvalCase(
		"Q28",
		("帮我回复 HR", "他说“方便发一份简历吗”"),
		("reply_draft", "reply_send"),
		("capability", "clarification"),
		expected_approval=True,
	),
	EvalCase("Q29", ("帮我发送招呼语",), ("greeting_send",), ("clarification", "capability"), requires_local_data=True),
	EvalCase("Q30", ("帮我回复 HR：您好，我考虑一下",), ("reply_send",), ("clarification", "capability")),
)


SMOKE_CASE_IDS = {"Q03", "Q07", "Q11", "Q15", "Q19", "Q20", "Q25", "Q29", "Q30"}


def _request_json(base_url: str, method: str, path: str, payload: dict[str, Any] | None, timeout: int) -> Any:
	"""发送 JSON 请求并返回解析结果。"""
	body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
	request = Request(
		f"{base_url.rstrip('/')}{path}",
		data=body,
		method=method,
		headers={"Content-Type": "application/json"},
	)
	try:
		with urlopen(request, timeout=timeout) as response:
			return json.loads(response.read().decode("utf-8"))
	except HTTPError as exc:
		detail = exc.read().decode("utf-8", errors="replace")
		raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
	except URLError as exc:
		raise RuntimeError(f"无法连接 {base_url}：{exc.reason}") from exc


def _is_complex_goal(intent: dict[str, Any]) -> bool:
	"""复现当前后端的复杂目标判断，便于校验实际路由。"""
	intent_type = str(intent.get("intent_type", ""))
	if intent_type in {"pipeline_run", "complex_task"}:
		return True
	companies = intent.get("entities", {}).get("company") if isinstance(intent.get("entities"), dict) else None
	if intent_type == "company_research" and isinstance(companies, list) and len(companies) > 1:
		return True
	steps = intent.get("steps")
	if isinstance(steps, list) and steps:
		return len([step for step in steps if isinstance(step, dict) and step.get("enabled", True)]) >= 2
	goal = str(intent.get("goal", "")).replace(" ", "")
	return sum(term in goal for term in ("采集", "搜索", "评分", "生成", "发送", "监测")) >= 2


def _infer_route(intent: dict[str, Any]) -> str:
	"""根据后端公开的结构化意图推导实际决策路由。"""
	missing = intent.get("missing_fields") if isinstance(intent.get("missing_fields"), list) else []
	confidence = float(intent.get("confidence", 0.0) or 0.0)
	mode = str(intent.get("mode", "execute"))
	intent_type = str(intent.get("intent_type", ""))
	if missing or confidence < 0.6:
		return "clarification"
	if mode in {"answer", "cancel"}:
		return "direct"
	if confidence < 0.85 and intent_type in HIGH_CONFIDENCE_ACTIONS:
		return "clarification"
	if _is_complex_goal(intent):
		return "planner"
	if intent_type == "general_question":
		return "direct"
	return "capability"


def _check_case(case: EvalCase, result: dict[str, Any]) -> tuple[bool, list[str], dict[str, Any]]:
	"""比较单个用例的最终响应和预期。"""
	intent = result.get("intent") if isinstance(result.get("intent"), dict) else {}
	actual_intent = str(intent.get("intent_type", "unknown"))
	actual_mode = str(intent.get("mode", "execute"))
	actual_route = _infer_route(intent)
	missing = [str(field) for field in intent.get("missing_fields", [])]
	tools = [str(item.get("name")) for item in result.get("tool_trace", []) if isinstance(item, dict)]
	approval = bool(result.get("approval")) or result.get("status") == "interrupted"
	errors: list[str] = []
	if actual_intent not in case.expected_intents:
		errors.append(f"意图期望 {case.expected_intents}，实际 {actual_intent}")
	if actual_route not in case.expected_routes:
		errors.append(f"路由期望 {case.expected_routes}，实际 {actual_route}")
	if case.expected_modes and actual_mode not in case.expected_modes:
		errors.append(f"模式期望 {case.expected_modes}，实际 {actual_mode}")
	if case.expected_missing_fields and not set(case.expected_missing_fields).issubset(missing):
		errors.append(f"缺失字段应包含 {case.expected_missing_fields}，实际 {tuple(missing)}")
	if case.expected_approval is not None and approval != case.expected_approval:
		errors.append(f"审批期望 {case.expected_approval}，实际 {approval}")
	if case.expected_tools and not set(case.expected_tools).issubset(tools):
		errors.append(f"工具应包含 {case.expected_tools}，实际 {tuple(tools)}")
	actual = {
		"intent": actual_intent,
		"mode": actual_mode,
		"confidence": intent.get("confidence"),
		"missing_fields": missing,
		"route": actual_route,
		"approval": approval,
		"tools": tools,
		"message_type": result.get("message", {}).get("message_type"),
		"status": result.get("status"),
	}
	return not errors, errors, actual


def _run_case(base_url: str, case: EvalCase, timeout: int, web_enabled: bool) -> dict[str, Any]:
	"""在独立临时会话中运行一条用例，并保证不恢复任何审批。"""
	conversation = _request_json(
		base_url,
		"POST",
		"/api/assistant/conversations",
		{"title": f"自动评测 {case.case_id}"},
		timeout,
	)
	conversation_id = str(conversation["id"])
	turn_results: list[dict[str, Any]] = []
	started = time.perf_counter()
	try:
		for turn in case.turns:
			turn_started = time.perf_counter()
			response = _request_json(
				base_url,
				"POST",
				f"/api/assistant/conversations/{conversation_id}/messages",
				{"message": turn, "web_enabled": web_enabled},
				timeout,
			)
			turn_results.append(
				{
					"message": turn,
					"elapsed_ms": round((time.perf_counter() - turn_started) * 1000),
					"response": response,
				}
			)
			# 审批中断后绝不自动确认；多轮用例到此停止，避免触发副作用。
			if response.get("approval") or response.get("status") == "interrupted":
				break
		final_result = turn_results[-1]["response"]
		passed, errors, actual = _check_case(case, final_result)
		return {
			"case": asdict(case),
			"passed": passed,
			"errors": errors,
			"actual": actual,
			"elapsed_ms": round((time.perf_counter() - started) * 1000),
			"turns": turn_results,
		}
	finally:
		_request_json(base_url, "DELETE", f"/api/assistant/conversations/{conversation_id}", None, timeout)


def _write_reports(results: list[dict[str, Any]], output_dir: Path) -> tuple[Path, Path]:
	"""写入完整 JSON 和便于阅读的 Markdown 报告。"""
	output_dir.mkdir(parents=True, exist_ok=True)
	timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
	json_path = output_dir / f"qa-eval-{timestamp}.json"
	markdown_path = output_dir / f"qa-eval-{timestamp}.md"
	passed = sum(bool(result.get("passed")) for result in results)
	payload = {
		"generated_at": datetime.now().isoformat(timespec="seconds"),
		"summary": {"total": len(results), "passed": passed, "failed": len(results) - passed},
		"results": results,
	}
	json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
	lines = [
		"# AI 助手问答自动评测报告",
		"",
		f"- 总数：{len(results)}",
		f"- 通过：{passed}",
		f"- 失败：{len(results) - passed}",
		"",
		"| 用例 | 结果 | 实际意图 | 实际路由 | 耗时 | 说明 |",
		"| --- | --- | --- | --- | ---: | --- |",
	]
	for result in results:
		actual = result.get("actual", {})
		errors = "；".join(result.get("errors", [])) or "符合预期"
		lines.append(
			f"| {result['case']['case_id']} | {'通过' if result.get('passed') else '失败'} | "
			f"`{actual.get('intent', '-')}` | `{actual.get('route', '-')}` | "
			f"{result.get('elapsed_ms', 0)} ms | {errors.replace('|', '\\|')} |"
		)
	markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
	return json_path, markdown_path


def _parse_args() -> argparse.Namespace:
	"""解析命令行参数。"""
	parser = argparse.ArgumentParser(description="自动评测 BossHunter AI 助手问答路由")
	parser.add_argument("--base-url", default="http://127.0.0.1:8686", help="BossHunter Web 服务地址")
	parser.add_argument("--suite", choices=("smoke", "all"), default="smoke", help="运行冒烟集或全部用例")
	parser.add_argument("--case", action="append", dest="case_ids", help="只运行指定用例，可重复传入")
	parser.add_argument("--timeout", type=int, default=180, help="单次 API 请求超时秒数")
	parser.add_argument("--disable-web", action="store_true", help="关闭问答中的联网检索")
	parser.add_argument("--output-dir", type=Path, default=Path("data/assistant-evals"), help="报告输出目录")
	return parser.parse_args()


def main() -> int:
	"""运行所选用例并返回适合 CI 使用的退出码。"""
	args = _parse_args()
	selected = list(CASES)
	if args.case_ids:
		requested = set(args.case_ids)
		selected = [case for case in selected if case.case_id in requested]
	elif args.suite == "smoke":
		selected = [case for case in selected if case.case_id in SMOKE_CASE_IDS]
	if not selected:
		raise SystemExit("没有匹配的测试用例")

	try:
		_request_json(args.base_url, "GET", "/api/health", None, min(args.timeout, 5))
	except RuntimeError as exc:
		print(f"评测未启动：{exc}")
		print("请先运行 `bosshunter web`，再重新执行本脚本。")
		return 2

	results: list[dict[str, Any]] = []
	for index, case in enumerate(selected, start=1):
		print(f"[{index}/{len(selected)}] {case.case_id}：{case.turns[0]}", flush=True)
		try:
			result = _run_case(args.base_url, case, args.timeout, not args.disable_web)
		except Exception as exc:
			result = {
				"case": asdict(case),
				"passed": False,
				"errors": [f"执行异常：{type(exc).__name__}: {exc}"],
				"actual": {},
				"elapsed_ms": 0,
				"turns": [],
			}
		results.append(result)
		print("  通过" if result["passed"] else f"  失败：{'；'.join(result['errors'])}", flush=True)

	json_path, markdown_path = _write_reports(results, args.output_dir)
	passed = sum(bool(result["passed"]) for result in results)
	print(f"完成：{passed}/{len(results)} 通过")
	print(f"JSON：{json_path}")
	print(f"报告：{markdown_path}")
	return 0 if passed == len(results) else 1


if __name__ == "__main__":
	raise SystemExit(main())
