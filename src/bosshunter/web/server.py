"""BossHunter Web Server - Bottle HTTP service + API routes.

Serves:
- /api/* → JSON data endpoints
- /* → Frontend static files (dist/)
"""

import json
import os
import time
from copy import deepcopy
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Lock, Thread

from bottle import Bottle, ServerAdapter, request, response, static_file, HTTPError

from bosshunter import __version__
from bosshunter.config import load_config, CITY_CODES
from bosshunter.environment import allows_trusted_local_port_publish

from bosshunter.db import (
	add_history,
	count_unresolved_monitor_items,
	get_daily_activity,
	get_funnel_stats,
	get_job,
	get_jobs_by_status,
	get_jobs_approved_to_send,
	get_jobs_needing_resume,
	get_jobs_needing_greeting,
	get_jobs_pending_confirmation,
	get_jobs_with_send_errors,
	get_recent_history,
	get_unresolved_resume_failures,
	get_stats,
	get_top_companies,
	reset_job_greeting,
	update_job_status,
)
from bosshunter.web.tasks import TaskAlreadyRunningError, WorkbenchTask, WorkbenchTaskRunner

# 从 .env 加载的本地环境变量（BOSSHUNTER_AUTH_TOKEN 等）在导入期即可用，
# 因此这里必须走真实环境，不能复用 _load_web_env 的覆盖逻辑。
_AUTH_TOKEN = os.environ.get("BOSSHUNTER_AUTH_TOKEN") or ""

app = Bottle()
task_runner = WorkbenchTaskRunner()

# Paths
FRONTEND_DIR = Path(__file__).parent / "frontend" / "dist"
SCHEMA_PATH = Path(__file__).parent / "config_schema.json"


def _default_base_dir() -> Path:
	"""Resolve the runtime project directory even when launched outside repo root."""
	source_root = Path(__file__).resolve().parents[3]
	if (source_root / "config.yaml").exists():
		return source_root

	cwd = Path.cwd()
	if (cwd / "config.yaml").exists():
		return cwd

	return cwd


BASE_DIR = _default_base_dir()
DATA_DIR = BASE_DIR / "data"
RESUME_DIR = DATA_DIR / "resumes"
PROFILE_DIR = DATA_DIR / "profile"
CONFIG_PATH = BASE_DIR / "config.yaml"
_assistant_service = None
# 会话级流式暂停开关：Event 未设置表示输出暂停，设置表示继续转发。
_stream_gates: dict[str, Event] = {}
_stream_gate_lock = Lock()


def _acquire_stream_gate(conversation_id: str) -> Event:
	"""为会话创建（或复用）暂停开关，初始为继续输出状态。"""
	with _stream_gate_lock:
		gate = _stream_gates.get(conversation_id)
		if gate is None:
			gate = Event()
			gate.set()
			_stream_gates[conversation_id] = gate
		return gate


def _release_stream_gate(conversation_id: str) -> None:
	"""流结束后清理暂停开关，避免会话状态残留。"""
	with _stream_gate_lock:
		_stream_gates.pop(conversation_id, None)


def set_base_dir(base_dir: Path | str) -> None:
	"""Set the runtime directory used for config.yaml, data, and uploads."""
	global BASE_DIR, DATA_DIR, RESUME_DIR, PROFILE_DIR, CONFIG_PATH, _assistant_service
	if _assistant_service is not None:
		_assistant_service.close()
		_assistant_service = None
	BASE_DIR = Path(base_dir).resolve()
	DATA_DIR = BASE_DIR / "data"
	RESUME_DIR = DATA_DIR / "resumes"
	PROFILE_DIR = DATA_DIR / "profile"
	CONFIG_PATH = BASE_DIR / "config.yaml"


def _get_web_db():
	"""打开与 Agent 服务一致的业务数据库，避免回复完成后历史记录消失。"""
	from bosshunter.db import get_business_db

	return get_business_db(CONFIG_PATH)


def _is_loopback_host(host: str) -> bool:
	"""判断监听地址是否为回环地址。

	本地绑定（127.0.0.1 / localhost / ::1）不需要鉴权；绑定到局域网
	（0.0.0.0 或具体网卡地址）时必须设置 BOSSHUNTER_AUTH_TOKEN 才能启动。
	"""
	host = str(host or "").strip().lower()
	return host in {"127.0.0.1", "localhost", "::1", "[::1]"}


def _host_requires_auth(host: str) -> bool:
	"""非回环绑定但未配置鉴权 token 时，拒绝提供未授权 Web 服务。"""
	return not _is_loopback_host(host) and not _AUTH_TOKEN and not allows_trusted_local_port_publish()


def _unauthorized() -> HTTPError:
	return HTTPError(401, "缺少或错误的访问令牌，请设置 BOSSHUNTER_AUTH_TOKEN")


def _check_web_auth():
	"""对启用鉴权的请求强制 Bearer Token 校验。

	仅保护 /api/* 数据与操作接口；静态资源（前端外壳）无需鉴权，
	否则页面都无法加载，用户无法输入令牌。回环（默认 127.0.0.1）
	请求不做校验，保持本地开发零配置；LAN 绑定场景依赖 token 防止
	局域网内任意进程访问管理面。
	"""
	if not _AUTH_TOKEN:
		return
	if not str(request.path or "").startswith("/api/"):
		return
	authorization = request.headers.get("Authorization", "")
	scheme, _, token = str(authorization).partition(" ")
	if str(token).strip() and str(scheme).lower() == "bearer":
		if _token_equals(token.strip(), _AUTH_TOKEN):
			return
	raise _unauthorized()


def _token_equals(a: str, b: str) -> bool:
	"""常数时间比较 token，避免时序侧信道。"""
	try:
		import hmac
		return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
	except Exception:
		return a == b


def _get_assistant_service():
	"""按当前运行目录延迟创建 LangGraph 服务。"""
	global _assistant_service
	if _assistant_service is None:
		from bosshunter.assistant import AssistantService

		_assistant_service = AssistantService(BASE_DIR, CONFIG_PATH, _start_assistant_action)
	return _assistant_service


def _json_response(data, status_code=200):
	"""Return JSON response with proper headers."""
	response.content_type = "application/json; charset=utf-8"
	response.status = status_code
	return json.dumps(data, ensure_ascii=False, default=str)


def _utc_timestamp(value):
	"""Mark SQLite CURRENT_TIMESTAMP values as UTC ISO-8601 strings."""
	if not isinstance(value, str) or not value.strip():
		return value
	text = value.strip()
	if text.endswith("Z") or text[-6:-5] in {"+", "-"}:
		return text
	return f"{text.replace(' ', 'T')}Z"


def _serialize_db_item(item):
	"""Copy a database record and make timestamp timezone semantics explicit."""
	record = dict(item)
	for field in ("created_at", "updated_at"):
		if field in record:
			record[field] = _utc_timestamp(record[field])
	return record


def _serialize_db_items(items):
	"""Serialize a collection of database records for browser clients."""
	return [_serialize_db_item(item) for item in items]


def _serialize_history_items(items):
	"""Expose structured history details while retaining the legacy detail field."""
	serialized = []
	for item in items:
		record = _serialize_db_item(item)
		detail = record.get("detail")
		if isinstance(detail, str) and detail.lstrip().startswith("{"):
			try:
				payload = json.loads(detail)
			except (json.JSONDecodeError, TypeError):
				payload = None
			if isinstance(payload, dict):
				record["detail_payload"] = payload
		record["resolved"] = bool(record.get("resolved"))
		serialized.append(record)
	return serialized


def _redact_config_dict(config: dict) -> dict:
	"""返回脱敏后的配置副本，隐藏 API key / auth token 明文。

	与 /api/config 响应共用同一脱敏规则，供任何需要导出配置的接口复用。
	"""
	redacted = deepcopy(config)
	ai_cfg = redacted.get("ai")
	if isinstance(ai_cfg, dict):
		key = ai_cfg.pop("api_key", None)
		if key:
			ai_cfg["api_key_masked"] = _mask_api_key(str(key))
		auth_token = ai_cfg.pop("auth_token", None)
		if auth_token:
			ai_cfg["auth_token_masked"] = _mask_api_key(str(auth_token))
	return redacted


def _mask_api_key(key):
	"""Return a display-safe API key marker."""
	if not key:
		return ""
	if len(key) > 8:
		return key[:4] + "***" + key[-4:]
	return "***"


def _redact_config_for_response(config):
	"""Hide secrets before returning config to the browser."""
	return _redact_config_dict(config)


def _sanitize_config_for_write(data):
	"""Remove browser-only fields and preserve existing secrets on blank posts."""
	cleaned = deepcopy(data)
	ai_cfg = cleaned.get("ai")
	if not isinstance(ai_cfg, dict):
		return cleaned

	ai_cfg.pop("api_key_masked", None)
	ai_cfg.pop("has_api_key", None)
	ai_cfg.pop("auth_token_masked", None)
	ai_cfg.pop("has_auth_token", None)

	existing_ai = load_config(CONFIG_PATH).get("ai", {})
	provider = ai_cfg.get("provider") or existing_ai.get("provider") or "openai_compatible"
	if provider not in {"anthropic", "openai_compatible"}:
		# 前端可能提交旧的 "openai" 值：保持用户已配置的提供商，而不是
		# 静默改成默认的 openai_compatible。
		provider = existing_ai.get("provider") or "openai_compatible"
	ai_cfg["provider"] = provider

	for field in ("api_key", "auth_token"):
		posted_value = ai_cfg.get(field)
		existing_value = existing_ai.get(field)
		existing_mask = _mask_api_key(str(existing_value)) if existing_value else ""
		should_preserve = (
			posted_value is None or str(posted_value).strip() == "" or (existing_mask and posted_value == existing_mask)
		)

		if should_preserve:
			if existing_value:
				ai_cfg[field] = existing_value
			else:
				ai_cfg.pop(field, None)

	return cleaned


def _preflight_messages(mode: str, config: dict) -> list[str]:
	"""Return user-actionable blockers before starting a dashboard task."""
	messages: list[str] = []
	if mode not in {"full", "collect", "score", "reenrich", "monitor", "import"}:
		messages.append(f"不支持的任务模式：{mode}")

	profile = config.get("profile", {})
	resume_path = profile.get("resume_path", "")
	if not resume_path or not Path(str(resume_path)).exists():
		messages.append("请先在配置页上传 Markdown 简历。")

	if mode in {"full", "collect"} and not config.get("search", {}).get("keywords"):
		messages.append("请先在配置页填写搜索关键词。")

	# 定向采集（import）跳过搜索关键词检查：它是按 URL / 公司+职位手动触发的，
	# 不依赖批量采集的 search.keywords 配置。但仍需要简历（招呼语生成）和 AI Key。
	if mode in {"full", "collect", "score", "import"}:
		ai_cfg = config.get("ai", {}) if isinstance(config.get("ai"), dict) else {}
		has_ai_key = bool(
			os.environ.get("ANTHROPIC_API_KEY")
			or os.environ.get("ANTHROPIC_AUTH_TOKEN")
			or ai_cfg.get("api_key")
			or ai_cfg.get("auth_token")
		)
		if not has_ai_key:
			messages.append("请先在配置页填写 AI API Key，或设置 ANTHROPIC_API_KEY 环境变量。")

	return messages


def _task_config(extra: dict | None = None) -> dict:
	config = load_config(CONFIG_PATH)
	if extra:
		config.update(extra)
	return config


def _log(task: WorkbenchTask, message: str) -> None:
	task.logs.append(message)
	task_runner._persist(task)


def _execute_collect(task: WorkbenchTask, config: dict) -> None:
	from bosshunter.scraper.jobs import scrape_jobs

	# 注入 stop_event，让采集/评分内部能在循环迭代间协作式中断。
	config = dict(config)
	config["_workbench_stop_event"] = task.stop_requested
	keywords = config.get("search", {}).get("keywords", [])
	_log(task, "开始采集岗位")
	scrape_jobs(config, keywords, config.get("_workbench_limit"))
	if not task.stop_requested.is_set():
		_log(task, "采集完成，岗位已进入待评分队列")


def _execute_score(task: WorkbenchTask, config: dict) -> None:
	from bosshunter.ai.scorer import score_jobs

	# 注入 stop_event，让评分循环能在岗位迭代间中断。
	config = dict(config)
	config["_workbench_stop_event"] = task.stop_requested
	_log(task, "开始 AI 评分（不重新采集）")
	scored, filtered = score_jobs(config)
	db = _get_web_db()
	try:
		remaining = len(get_jobs_by_status(db, "pending"))
	finally:
		db.close()
	_log(task, f"AI 评分结束：通过 {scored}，过滤 {filtered}，待重试 {remaining}")


def _start_assistant_action(action: dict) -> dict:
	"""将已确认的助手提案映射到既有安全后台执行器。"""
	action_type = str(action.get("type", ""))
	params = action.get("parameters") if isinstance(action.get("parameters"), dict) else {}
	if action_type == "collect_jobs":
		config = _task_config({"_workbench_limit": params.get("limit")})
		config.setdefault("search", {})["keywords"] = list(params.get("keywords", []))
		if params.get("cities"):
			config["search"]["cities"] = list(params["cities"])
		config["search"]["max_pages"] = params.get("max_pages", config["search"].get("max_pages", 3))
		messages = _preflight_messages("collect", config)
		if messages:
			raise ValueError("；".join(messages))
		return task_runner.start("collect", config)
	if action_type == "score_jobs":
		config = _task_config({"_workbench_job_ids": params.get("job_ids", [])})
		messages = _preflight_messages("score", config)
		if messages:
			raise ValueError("；".join(messages))
		return task_runner.start("score", config)
	if action_type == "complete_job_details":
		job_id = str(params.get("job_id", "")).strip()
		title = str(params.get("title", "")).strip()
		jd = str(params.get("jd", "")).strip()
		if not job_id or not title or not jd:
			raise ValueError("补充岗位信息至少需要记录、职位名称和 JD")
		db = _get_web_db()
		try:
			row = db.execute(
				"SELECT id FROM jobs WHERE id = ? AND "
				"(id LIKE 'manual_%' OR title = 'HR主动沟通' OR jd IS NULL OR TRIM(jd) = '')",
				(job_id,),
			).fetchone()
			if not row:
				raise ValueError("待补充的主动沟通记录不存在或已经是正式岗位")
			db.execute(
				"UPDATE jobs SET title = ?, jd = ?, salary = COALESCE(NULLIF(?, ''), salary), "
				"url = COALESCE(NULLIF(?, ''), url), status = 'pending', score = 0, score_reason = NULL, "
				"updated_at = CURRENT_TIMESTAMP WHERE id = ?",
				(title, jd, str(params.get("salary", "")).strip(), str(params.get("url", "")).strip(), job_id),
			)
			db.commit()
		finally:
			db.close()
		return task_runner.start("score", _task_config({"_workbench_job_ids": [job_id]}))
	if action_type == "generate_greetings":
		job_ids = [str(item) for item in params.get("job_ids", []) if str(item)]
		if not job_ids:
			raise ValueError("没有可生成招呼语的岗位")
		return task_runner.start("greeting", _task_config({"_workbench_job_ids": job_ids}))
	if action_type == "monitor_replies":
		config = _task_config()
		messages = _preflight_messages("monitor", config)
		if messages:
			raise ValueError("；".join(messages))
		return task_runner.start("monitor", config)
	if action_type == "run_pipeline":
		config = _task_config()
		messages = _preflight_messages("full", config)
		if messages:
			raise ValueError("；".join(messages))
		return task_runner.start("full", config)
	if action_type == "send_greetings":
		job_ids = [
			str(item) for item in action.get("selected_item_ids", []) if str(item) in set(params.get("job_ids", []))
		]
		if not job_ids:
			raise ValueError("请选择有效的招呼语")
		db = _get_web_db()
		try:
			for job_id in job_ids:
				update_job_status(db, job_id, "approved")
				add_history(db, job_id, "approved", "AI 助手确认投递")
		finally:
			db.close()
		return task_runner.start(
			"deliver", _task_config({"_workbench_job_ids": job_ids, "_workbench_skip_greeting": True})
		)
	if action_type == "send_hr_reply":
		if str(action.get("selected_item_ids", [""])[0]) != str(params.get("history_id")):
			raise ValueError("请选择有效的 HR 回复")
		return task_runner.start(
			"reply", _task_config({"_reply_history_id": params["history_id"], "_reply_message": params["message"]})
		)
	raise ValueError("不支持的助手任务")


def _execute_reenrich(task: WorkbenchTask, config: dict) -> None:
	"""Revisit job detail pages to fill previously missing company metadata."""
	from bosshunter.scraper.jobs import reenrich_jobs

	reenrich_config = dict(config)
	reenrich_config["_workbench_stop_event"] = task.stop_requested
	_log(task, "开始补全缺失的地区、公司背景和注册资本信息")
	updated = reenrich_jobs(reenrich_config)
	_log(task, f"企业信息补全结束：更新 {updated} 个岗位")


def _execute_monitor(task: WorkbenchTask, config: dict) -> None:
	from bosshunter.executor.monitor import monitor_and_send_resumes

	monitor_config = dict(config)
	monitor_config["_workbench_stop_event"] = task.stop_requested
	interval_min = int(config.get("monitor", {}).get("interval", 30) or 30)
	interval_sec = max(interval_min * 60, 1)
	while not task.stop_requested.is_set():
		_log(task, "执行一轮监测")
		# 周期级异常隔离：单轮 DB 抖动/页面结构异常不应终止整个长任务，
		# 记录错误后等下一个周期重试（与 CLI 循环 pipeline.py 的行为一致）。
		try:
			summary = monitor_and_send_resumes(monitor_config)
		except Exception as exc:
			_log(task, f"本轮监测出错（{type(exc).__name__}: {exc}），等待下个周期重试")
			if task.stop_requested.is_set():
				return
			task.stop_requested.wait(interval_sec)
			continue
		# monitor_and_send_resumes 内部用 console.print 打终端，不会进任务日志，
		# 这里把未跟踪会话的回复显式写入日志，避免用户在面板上看不到检测结果。
		unmatched = summary.get("unmatched_convs") or []
		if unmatched:
			names = "、".join(str(c.get("company") or c.get("hr_name") or "未知") for c in unmatched[:5])
			suffix = f"等 {len(unmatched)} 个" if len(unmatched) > 5 else ""
			_log(task, f"⚠ 检测到 {len(unmatched)} 个未跟踪会话的新回复（{names}{suffix}），请手动到 BOSS 查看")
		if task.stop_requested.is_set():
			return
		_log(task, f"本轮监测完成，{interval_min} 分钟后再次检查")
		task.stop_requested.wait(interval_sec)


def _execute_full(task: WorkbenchTask, config: dict) -> None:
	_execute_collect(task, config)
	if task.stop_requested.is_set():
		return

	db = _get_web_db()
	try:
		threshold = int(config.get("scoring", {}).get("threshold", 60) or 60)
		pending_confirmation = [
			job for job in get_jobs_pending_confirmation(db) if int(job.get("score") or 0) >= threshold
		]
	finally:
		db.close()
	if not pending_confirmation:
		task.context["waiting_confirmation"] = False
		_log(task, "没有待确认岗位，流程结束")
		return

	confirmation_event = Event()
	task.context["waiting_confirmation"] = True
	task.context["confirmation_event"] = confirmation_event
	_log(task, "等待前端确认投递")
	while not task.stop_requested.is_set() and not confirmation_event.wait(0.5):
		pass
	if task.stop_requested.is_set():
		return

	job_ids = [str(job_id) for job_id in task.context.get("confirmed_job_ids", []) if str(job_id)]
	if not job_ids:
		_log(task, "未收到前端确认岗位，流程结束")
		return

	task.context["waiting_confirmation"] = False
	_log(task, f"前端已确认 {len(job_ids)} 个岗位，继续投递")
	deliver_config = dict(config)
	deliver_config["_workbench_job_ids"] = job_ids
	_execute_deliver(task, deliver_config)
	if task.stop_requested.is_set():
		return
	_execute_monitor(task, config)


def _execute_deliver(task: WorkbenchTask, config: dict) -> None:
	from bosshunter.ai.greeter import generate_greetings
	from bosshunter.executor.sender import send_greetings

	config = dict(config)
	config["_workbench_stop_event"] = task.stop_requested
	if not config.get("_workbench_skip_greeting"):
		_log(task, "检查招呼语...")
		generated = generate_greetings(config)
		if generated > 0:
			_log(task, f"已生成 {generated} 条招呼语")
		else:
			_log(task, "所有岗位已有招呼语，跳过生成")
		if task.stop_requested.is_set():
			return
	_log(task, "发送招呼语")
	sent = send_greetings(config, force=True)
	_log(task, f"已发送 {sent} 条招呼语")


def _execute_greeting(task: WorkbenchTask, config: dict) -> None:
	from bosshunter.ai.greeter import generate_greetings

	_log(task, "生成招呼语...")
	generated = generate_greetings(config)
	if generated > 0:
		_log(task, f"已生成 {generated} 条招呼语，等待人工查看后发送")
	else:
		_log(task, "所有岗位已有招呼语，无需重新生成")


def _execute_reply(task: WorkbenchTask, config: dict) -> None:
	"""Send a user-confirmed reply through the matching BOSS conversation."""
	history_id = int(config.get("_reply_history_id") or 0)
	message = str(config.get("_reply_message") or "").strip()
	if not history_id or not message:
		raise ValueError("缺少待回复记录或回复内容")

	db = _get_web_db()
	try:
		row = db.execute(
			"SELECT j.*, h.id AS history_id, h.action AS history_action, h.detail AS pending_detail "
			"FROM history h JOIN jobs j ON j.id = h.job_id WHERE h.id = ?",
			(history_id,),
		).fetchone()
		if not row or row["history_action"] != "reply_pending":
			raise ValueError("待回复记录不存在或已处理")
		job = dict(row)
		pending_detail = str(row["pending_detail"] or "")
	finally:
		db.close()

	from bosshunter.browser import close_tab
	from bosshunter.executor.monitor import _build_reply_resolution_detail, _open_conversation, _send_message_in_chat

	_log(task, "打开对应的 BOSS 聊天会话")
	target_id = _open_conversation(job, config)
	if not target_id:
		raise RuntimeError("无法打开对应的 BOSS 聊天会话")
	try:
		if not _send_message_in_chat(target_id, message):
			raise RuntimeError("未能发送回复，请检查 BOSS 登录状态和聊天页面")
	finally:
		close_tab(target_id)

	db = _get_web_db()
	try:
		add_history(
			db,
			job["id"],
			"replied",
			_build_reply_resolution_detail(
				"replied.v1", "Web Dashboard 已确认并发送回复", pending_detail, message, history_id
			),
		)
		update_job_status(db, job["id"], "replied")
	finally:
		db.close()
	_log(task, "回复已发送并记录")


def _upsert_imported_job(job: dict) -> str:
	"""Insert a target-imported job, or fill blanks if it already exists.

	Returns the job id. Used by the import executor for both URL-mode and
	search-mode flows. Reuses ``insert_job`` (INSERT OR IGNORE) and
	``update_job_enrichment`` (only fills NULL/blank fields) so re-importing a
	job is idempotent and never clobbers an existing greeting/score/status.
	"""
	from bosshunter.db import insert_job, update_job_enrichment

	db = _get_web_db()
	try:
		# 定向采集的岗位跳过 AI 评分与 deal_breaker/agency 过滤——用户已经手动选定，
		# 不应被自动过滤掉。status 保持默认 'pending'，随后由 regenerate_greeting
		# 推到 'ready'，套用现有「今日待确定 → 人工确认 → 投递」闭环。
		insert_job(db, job)
		# 即便已存在，也用新解析的字段补空（如重新采集补全 HR 信息）。
		update_job_enrichment(db, job)
		# company_size / company_industry 不在 update_job_enrichment 覆盖范围内，
		# 但 insert_job 已写入；若已存在且这些字段为空，做一次显式补全。
		db.execute(
			"UPDATE jobs SET company_size = COALESCE(NULLIF(company_size, ''), NULLIF(:company_size, '')), "
			"company_industry = COALESCE(NULLIF(company_industry, ''), NULLIF(:company_industry, '')), "
			"hr_name = COALESCE(NULLIF(hr_name, ''), NULLIF(:hr_name, '')), "
			"hr_title = COALESCE(NULLIF(hr_title, ''), NULLIF(:hr_title, '')), "
			"hr_active = COALESCE(NULLIF(hr_active, ''), NULLIF(:hr_active, '')), "
			"jd = COALESCE(NULLIF(jd, ''), NULLIF(:jd, '')), "
			"updated_at = CURRENT_TIMESTAMP WHERE id = :id",
			job,
		)
		db.commit()
		return job["id"]
	finally:
		db.close()


def _import_generate_greeting(task: WorkbenchTask, job_id: str, config: dict) -> None:
	"""Generate a greeting for an imported job so it lands in the review queue.

	Uses ``regenerate_greeting`` which resets the job to ``approved``, generates
	the greeting, then sets status to ``ready`` — making it appear in the
	dashboard's "今日待确定" section, exactly where the manual confirmation flow
	already lives. Does NOT send anything; the user must confirm explicitly.
	"""
	from bosshunter.ai.greeter import regenerate_greeting

	import_config = dict(config)
	import_config["_workbench_stop_event"] = task.stop_requested
	_log(task, f"为岗位 {job_id} 生成招呼语…")
	greeting = regenerate_greeting(job_id, import_config)
	if greeting:
		_log(task, "招呼语已生成，请在「今日待确定」中查看并确认")
	else:
		_log(task, "⚠ 招呼语生成失败（请检查 AI 配置），岗位已入库可手动重试")


def _execute_import(task: WorkbenchTask, config: dict) -> None:
	"""Targeted one-job importer driven by the dashboard.

	Supports three sub-modes (selected by ``config["_import_params"]``):

	- ``{"mode": "url", "url": "..."}`` — collect a single job by detail URL,
	  then generate its greeting.
	- ``{"mode": "search", "company": "...", "title": "..."}`` — search by
	  company + title, store candidate list cards in ``task.context["candidates"]``
	  for the frontend to render. Stops here; the user picks one.
	- ``{"mode": "select", "url": "..."}`` — second stage of search mode:
	  collect the user-picked candidate's detail page and generate its greeting.
	"""
	from bosshunter.scraper.jobs import import_job_by_url, search_jobs_by_company_title

	params = config.get("_import_params", {}) or {}
	mode = params.get("mode")
	import_config = dict(config)
	import_config["_workbench_stop_event"] = task.stop_requested

	if mode == "url":
		url = str(params.get("url", "")).strip()
		if not url:
			raise ValueError("缺少岗位 URL")
		_log(task, f"按 URL 定向采集: {url}")
		job = import_job_by_url(url, import_config)
		if not job:
			raise RuntimeError("采集失败，请检查 URL 是否正确、BOSS 登录状态及 CDP 运行时")
		# 把解析得到的详情字段回填到 task.context，方便前端即时展示。
		task.context["job"] = {k: job.get(k) for k in ("id", "title", "company", "salary", "url")}
		job_id = _upsert_imported_job(job)
		hist_db = _get_web_db()
		try:
			add_history(hist_db, job_id, "imported", "Web Dashboard 定向采集（URL）")
		finally:
			hist_db.close()
		_import_generate_greeting(task, job_id, config)

	elif mode == "search":
		company = str(params.get("company", "")).strip()
		title = str(params.get("title", "")).strip()
		if not company and not title:
			raise ValueError("请至少填写公司名或职位名")
		_log(task, f"按公司+职位搜索: company={company!r} title={title!r}")
		candidates = search_jobs_by_company_title(company, title, import_config)
		# 候选列表写进 task.context；前端通过 /api/workbench/import/candidates 读取。
		task.context["candidates"] = candidates
		if not candidates:
			_log(task, "未找到匹配岗位，请放宽或调整关键词后重试")
		else:
			_log(task, f"找到 {len(candidates)} 个候选，请在面板中选择目标岗位")
		# search 模式到此结束：用户选定后会再次启动一个 mode=select 的 import 任务。

	elif mode == "select":
		url = str(params.get("url", "")).strip()
		if not url:
			raise ValueError("缺少所选岗位的 URL")
		_log(task, f"采集所选岗位详情: {url}")
		job = import_job_by_url(url, import_config)
		if not job:
			raise RuntimeError("采集失败，请检查 BOSS 登录状态及 CDP 运行时")
		task.context["job"] = {k: job.get(k) for k in ("id", "title", "company", "salary", "url")}
		job_id = _upsert_imported_job(job)
		hist_db = _get_web_db()
		try:
			add_history(hist_db, job_id, "imported", "Web Dashboard 定向采集（公司+职位）")
		finally:
			hist_db.close()
		_import_generate_greeting(task, job_id, config)

	else:
		raise ValueError(f"未知的导入模式: {mode!r}")


task_runner._executors.update(
	{
		"full": _execute_full,
		"collect": _execute_collect,
		"score": _execute_score,
		"reenrich": _execute_reenrich,
		"monitor": _execute_monitor,
		"deliver": _execute_deliver,
		"greeting": _execute_greeting,
		"reply": _execute_reply,
		"import": _execute_import,
	}
)


# ─── Health ───────────────────────────────────────────────


@app.route("/api/health")
def health():
	return _json_response({"status": "ok", "version": __version__})


# ─── Dashboard APIs ──────────────────────────────────────


@app.route("/api/funnel")
def api_funnel():
	db = _get_web_db()
	try:
		data = get_funnel_stats(db)
		return _json_response(data)
	finally:
		db.close()


@app.route("/api/stats")
def api_stats():
	db = _get_web_db()
	try:
		data = get_stats(db)
		return _json_response(data)
	finally:
		db.close()


@app.route("/api/activity")
def api_activity():
	days = int(request.params.get("days", 7))
	db = _get_web_db()
	try:
		data = get_daily_activity(db, days)
		return _json_response(data)
	finally:
		db.close()


@app.route("/api/jobs")
def api_jobs():
	status_filter = request.params.get("status", None)
	# Bottle 的 FormsDict.get() 会保留 latin-1 形式的原始字节；公司名包含中文时
	# 必须显式按 UTF-8 解码，否则 LIKE 收到乱码，前端看起来就像搜索没有生效。
	company_filter = (request.query.getunicode("company", "") or "").strip()
	# 默认一页 50 条，上限 200 防滥用。前端再做二级分页/滚动。
	limit = max(1, min(int(request.params.get("limit", 50)), 200))
	offset = max(0, int(request.params.get("offset", 0)))

	db = _get_web_db()
	try:
		# 当前页岗位（受 status 筛选 + 分页约束）
		query = "SELECT * FROM jobs"
		count_query = "SELECT COUNT(*) AS cnt FROM jobs"
		params = []
		filters = []
		if status_filter:
			filters.append("status = ?")
			params.append(status_filter)
		if company_filter:
			# 转义 LIKE 通配符，确保用户输入的公司名按普通文本匹配。
			escaped_company = company_filter.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
			filters.append("company LIKE ? ESCAPE '\\'")
			params.append(f"%{escaped_company}%")
		if filters:
			where_clause = " WHERE " + " AND ".join(filters)
			query += where_clause
			count_query += where_clause
		count_params = list(params)
		query += " ORDER BY score DESC, created_at DESC LIMIT ? OFFSET ?"
		params.extend([limit, offset])

		rows = db.execute(query, params).fetchall()
		jobs = _serialize_db_items(rows)

		# 当前筛选条件下的总数（用于分页器/计数标签）
		filtered_total = db.execute(count_query, count_params).fetchone()["cnt"]
		# 全量按状态计数（不受 status 筛选影响），喂给筛选 pill
		status_counts = get_stats(db)
		# 批量评分/补全企业按钮的计数基于全量，而非当前页
		pending_count = db.execute("SELECT COUNT(*) AS cnt FROM jobs WHERE status = 'pending'").fetchone()["cnt"]
		missing_company_info_count = db.execute(
			"SELECT COUNT(*) AS cnt FROM jobs "
			"WHERE district IS NULL OR TRIM(district) = '' "
			"OR company_background IS NULL OR TRIM(company_background) = '' "
			"OR registered_capital IS NULL OR TRIM(registered_capital) = ''"
		).fetchone()["cnt"]

		return _json_response(
			{
				"items": jobs,
				"total": filtered_total,
				"status_counts": status_counts,
				"pending_count": pending_count,
				"missing_company_info_count": missing_company_info_count,
			}
		)
	finally:
		db.close()


@app.route("/api/top-companies")
def api_top_companies():
	limit = int(request.params.get("limit", 5))
	db = _get_web_db()
	try:
		data = get_top_companies(db, limit)
		return _json_response(data)
	finally:
		db.close()


@app.route("/api/history")
def api_history():
	limit = int(request.params.get("limit", 15))
	include_unresolved = request.params.get("include_unresolved", "").lower() in ("1", "true", "yes")
	db = _get_web_db()
	try:
		data = get_recent_history(db, limit)
		if include_unresolved:
			seen_ids = {item["id"] for item in data}
			data.extend(item for item in get_unresolved_resume_failures(db) if item["id"] not in seen_ids)
			data.sort(
				key=lambda item: (str(item.get("created_at") or ""), int(item.get("id") or 0)),
				reverse=True,
			)
		return _json_response(_serialize_history_items(data))
	finally:
		db.close()


@app.route("/api/history/unresolved-replies/count")
def api_history_unresolved_replies_count():
	db = _get_web_db()
	try:
		return _json_response({"count": count_unresolved_monitor_items(db)})
	finally:
		db.close()


@app.route("/api/workbench")
def api_workbench():
	db = _get_web_db()
	try:
		status = task_runner.status()
		return _json_response(
			{
				"funnel": get_funnel_stats(db),
				# ready 已经是明确的待人工确认状态。定向采集岗位不会经过评分，
				# 因此不能再按分数过滤，否则会生成招呼语后从工作台消失。
				"pending_confirmation": _serialize_db_items(get_jobs_pending_confirmation(db)),
				"pending_greetings": _serialize_db_items(get_jobs_approved_to_send(db)),
				"needs_greeting": _serialize_db_items(get_jobs_needing_greeting(db)),
				"send_errors": _serialize_db_items(get_jobs_with_send_errors(db)),
				"needs_resume": _serialize_db_items(get_jobs_needing_resume(db)),
				"task": status["active"],
				"last_task": status["last_task"],
				# 并发任务的完整列表（如 monitor 运行期间启动的 reply），供前端轮询结果。
				"tasks": status["tasks"],
			}
		)
	finally:
		db.close()


@app.route("/api/workbench/preflight")
def api_workbench_preflight():
	mode = request.params.get("mode", "")
	try:
		config = load_config(CONFIG_PATH)
		keywords = [item.strip() for item in request.params.get("keywords", "").split(",") if item.strip()]
		cities = [item.strip() for item in request.params.get("cities", "").split(",") if item.strip()]
		if mode in {"full", "collect"}:
			if keywords:
				config.setdefault("search", {})["keywords"] = keywords
			if cities:
				config.setdefault("search", {})["cities"] = cities
		messages = _preflight_messages(mode, config)
		return _json_response({"ok": not messages, "messages": messages})
	except Exception as e:
		return _json_response({"ok": False, "messages": [str(e)]}, 500)


@app.route("/api/workbench/task", method="POST")
def api_workbench_task_start():
	try:
		body = request.json or {}
		mode = body.get("mode", "")
		config = _task_config()
		if mode in {"full", "collect"}:
			keywords = [str(item).strip() for item in body.get("keywords", []) if str(item).strip()]
			cities = [str(item).strip() for item in body.get("cities", []) if str(item).strip()]
			if keywords:
				config.setdefault("search", {})["keywords"] = keywords
			if cities:
				config.setdefault("search", {})["cities"] = cities
			limit = body.get("limit")
			if limit is not None:
				config["_workbench_limit"] = max(1, min(int(limit), 500))
		messages = _preflight_messages(mode, config)
		if messages:
			return _json_response({"error": "请先处理启动前检查", "messages": messages}, 400)
		task = task_runner.start(mode, config)
		return _json_response(task)
	except TaskAlreadyRunningError as e:
		return _json_response({"error": str(e)}, 409)
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/workbench/task/<task_id>/stop", method="POST")
def api_workbench_task_stop(task_id):
	try:
		return _json_response(task_runner.stop(task_id))
	except KeyError:
		return _json_response({"error": "任务不存在"}, 404)
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/workbench/task/<task_id>")
def api_workbench_task_get(task_id):
	"""读取指定后台任务，供 AI 助手任务卡轮询。"""
	task = task_runner._tasks.get(task_id)
	if task is None:
		return _json_response({"error": "任务不存在"}, 404)
	return _json_response(task.snapshot())


@app.route("/api/workbench/deliver", method="POST")
def api_workbench_deliver():
	try:
		body = request.json or {}
		job_ids = [str(job_id) for job_id in body.get("job_ids", []) if str(job_id)]
		if not job_ids:
			return _json_response({"error": "请选择要投递的岗位"}, 400)

		direct_send = bool(body.get("direct_send"))
		if not direct_send:
			# 工作台确认：允许确认任何非终态岗位（含 AI 助手导入等场景），
			# 终态（rejected/closed/sent 等）不应被重新置为待发送。
			blocked_statuses = {"rejected", "closed", "sent", "replied", "resume_sent", "follow_up_sent", "needs_resume"}
			db = _get_web_db()
			try:
				jobs = [get_job(db, job_id) for job_id in job_ids]
				missing_ids = [job_id for job_id, job in zip(job_ids, jobs) if job is None]
				if missing_ids:
					return _json_response({"error": f"岗位不存在: {', '.join(missing_ids)}"}, 404)
				ineligible = [job for job in jobs if job and job["status"] in blocked_statuses]
				if ineligible:
					return _json_response(
						{"error": "已拒绝、已关闭或已发送的岗位不能重复确认投递"},
						409,
					)
				for job_id in job_ids:
					update_job_status(db, job_id, "approved")
					add_history(db, job_id, "approved", "Web Dashboard 确认投递")
			finally:
				db.close()

		if not direct_send:
			status = task_runner.status()
			active_task = status.get("active") or {}
			waiting_task = task_runner._tasks.get(active_task.get("id"))
			if (
				waiting_task
				and waiting_task.mode == "full"
				and waiting_task.status == "running"
				and waiting_task.context.get("waiting_confirmation")
			):
				waiting_task.context["confirmed_job_ids"] = job_ids
				confirmation_event = waiting_task.context.get("confirmation_event")
				if isinstance(confirmation_event, Event):
					confirmation_event.set()
				return _json_response(waiting_task.snapshot())
			# 确认后不自动发送——岗位进入"待发送招呼语"由用户手动发送
			return _json_response({"confirmed": len(job_ids), "message": "已确认，请在「待发送招呼语」中手动发送"})

		# direct_send: 用户在"待发送招呼语"中手动点击发送。
		# 服务端强制人工确认：仅允许发送已 approved 的岗位，未确认岗位一律拒绝，
		# 避免 AI 批量导入生成的 ready 岗位绕过确认直接投递给 HR。
		# 已拒绝/已关闭岗位也禁止重发（前端入口隐藏了它们，这里作为兜底）。
		blocked_statuses = {"rejected", "closed", "sent", "replied", "resume_sent", "follow_up_sent", "needs_resume"}
		db = _get_web_db()
		try:
			jobs = [get_job(db, job_id) for job_id in job_ids]
			missing_ids = [job_id for job_id, job in zip(job_ids, jobs) if job is None]
			if missing_ids:
				return _json_response({"error": f"岗位不存在: {', '.join(missing_ids)}"}, 404)
			unapproved = [job for job in jobs if job and job["status"] != "approved"]
			if unapproved:
				return _json_response(
					{"error": "仅已确认的岗位可以发送，请先在「今日待确定」中确认后重试"},
					409,
				)
		finally:
			db.close()

		deliver_options = {
			"_workbench_job_ids": job_ids,
			"_workbench_skip_greeting": True,
			"_workbench_include_send_errors": True,
		}
		task = task_runner.start("deliver", _task_config(deliver_options))
		return _json_response(task)
	except TaskAlreadyRunningError as e:
		return _json_response({"error": str(e)}, 409)
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/workbench/greetings/regenerate", method="POST")
def api_workbench_regenerate_greetings():
	"""Regenerate selected greetings without sending them to BOSS."""
	try:
		body = request.json or {}
		job_ids = [str(job_id) for job_id in body.get("job_ids", []) if str(job_id)]
		if not job_ids:
			return _json_response({"error": "请选择要重新生成招呼语的岗位"}, 400)

		db = _get_web_db()
		try:
			jobs = [get_job(db, job_id) for job_id in job_ids]
			missing_ids = [job_id for job_id, job in zip(job_ids, jobs) if job is None]
			if missing_ids:
				return _json_response({"error": f"岗位不存在: {', '.join(missing_ids)}"}, 404)
			blocked_statuses = {"rejected", "closed"}
			ineligible = [job for job in jobs if job and job["status"] in blocked_statuses]
			if ineligible:
				return _json_response(
					{"error": "已拒绝或已关闭的岗位不能再次生成招呼语"},
					409,
				)
			reset_job_greeting(db, job_ids)
		finally:
			db.close()

		task = task_runner.start("greeting", _task_config({"_workbench_job_ids": job_ids}))
		return _json_response(task)
	except TaskAlreadyRunningError as e:
		return _json_response({"error": str(e)}, 409)
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/workbench/reject", method="POST")
def api_workbench_reject():
	try:
		body = request.json or {}
		job_ids = [str(job_id) for job_id in body.get("job_ids", []) if str(job_id)]
		if not job_ids:
			return _json_response({"error": "请选择要放弃的岗位"}, 400)

		db = _get_web_db()
		try:
			jobs = [get_job(db, job_id) for job_id in job_ids]
			missing_ids = [job_id for job_id, job in zip(job_ids, jobs) if job is None]
			if missing_ids:
				return _json_response({"error": f"岗位不存在: {', '.join(missing_ids)}"}, 404)
			eligible_statuses = {"ready", "approved", "error"}
			ineligible_jobs = [job for job in jobs if job and job["status"] not in eligible_statuses]
			if ineligible_jobs:
				return _json_response(
					{"error": "仅待确认、待发送或发送失败的岗位可以放弃，已发送的岗位不能放弃"},
					409,
				)
			for job_id in job_ids:
				update_job_status(db, job_id, "rejected")
				add_history(db, job_id, "rejected", "Web Dashboard 放弃投递")
		finally:
			db.close()

		return _json_response({"success": True, "count": len(job_ids)})
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/jobs/import", method="POST")
def api_job_import():
	"""启动定向采集任务（按 URL 或按公司+职位搜索）。

	Body:
	- ``{"mode": "url", "url": "..."}`` — 按详情页 URL 采集单岗位。
	- ``{"mode": "search", "company": "...", "title": "..."}`` — 搜索候选列表，
	  结果通过 ``/api/jobs/import/<task_id>/candidates`` 拉取。

	返回 task snapshot（异步执行）。该任务为短任务，可与 monitor/full 并行。
	"""
	try:
		body = request.json or {}
		mode = str(body.get("mode", "")).strip()
		if mode not in {"url", "search"}:
			return _json_response({"error": "mode 必须是 url 或 search"}, 400)

		params: dict = {"mode": mode}
		if mode == "url":
			url = str(body.get("url", "")).strip()
			if not url:
				return _json_response({"error": "请填写岗位 URL"}, 400)
			params["url"] = url
		else:  # search
			params["company"] = str(body.get("company", "")).strip()
			params["title"] = str(body.get("title", "")).strip()
			if not params["company"] and not params["title"]:
				return _json_response({"error": "请至少填写公司名或职位名"}, 400)

		# preflight 会校验简历与 AI Key；搜索关键词检查对 import 不适用。
		messages = _preflight_messages("import", load_config(CONFIG_PATH))
		if messages:
			return _json_response({"error": "请先处理启动前检查", "messages": messages}, 400)
		task = task_runner.start("import", _task_config({"_import_params": params}))
		return _json_response(task)
	except TaskAlreadyRunningError as e:
		return _json_response({"error": str(e)}, 409)
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/jobs/import/select", method="POST")
def api_job_import_select():
	"""search 模式第二阶段：用户选定候选后，采集详情并生成招呼语。

	Body: ``{"url": "..."}``（候选列表里那条的 url 字段）。
	"""
	try:
		body = request.json or {}
		url = str(body.get("url", "")).strip()
		if not url:
			return _json_response({"error": "请提供所选岗位的 URL"}, 400)

		messages = _preflight_messages("import", load_config(CONFIG_PATH))
		if messages:
			return _json_response({"error": "请先处理启动前检查", "messages": messages}, 400)
		task = task_runner.start(
			"import",
			_task_config({"_import_params": {"mode": "select", "url": url}}),
		)
		return _json_response(task)
	except TaskAlreadyRunningError as e:
		return _json_response({"error": str(e)}, 409)
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/jobs/import/<task_id>/candidates")
def api_job_import_candidates(task_id):
	"""读取 search 模式任务的候选岗位列表（task.context["candidates"]）。

	前端轮询任务状态变为 completed 后调此接口拿候选并渲染选择 UI。
	"""
	try:
		task = task_runner._tasks.get(task_id)
		if not task:
			return _json_response({"error": "任务不存在"}, 404)
		if task.mode != "import":
			return _json_response({"error": "该任务不是定向采集任务"}, 400)
		return _json_response({"candidates": task.context.get("candidates", [])})
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/jobs/<job_id>")
def api_job_detail(job_id):
	db = _get_web_db()
	try:
		row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
		if not row:
			return _json_response({"error": "岗位不存在"}, 404)
		return _json_response(_serialize_db_item(row))
	finally:
		db.close()


@app.route("/api/jobs/<job_id>/mark-resume-sent", method="POST")
def api_job_mark_resume_sent(job_id):
	db = _get_web_db()
	try:
		update_job_status(db, job_id, "resume_sent")
		add_history(db, job_id, "resume_sent", "Web Dashboard 标记定制简历已发送")
		return _json_response({"success": True})
	finally:
		db.close()


@app.route("/api/jobs/<job_id>/resume/download")
def api_job_resume_download(job_id):
	db = _get_web_db()
	try:
		row = db.execute("SELECT resume_path FROM jobs WHERE id = ?", (job_id,)).fetchone()
		if not row or not row["resume_path"]:
			return _json_response({"error": "定制简历不存在"}, 404)
		resume_path = Path(row["resume_path"])
		if not resume_path.exists():
			return _json_response({"error": "定制简历文件不存在"}, 404)
		return static_file(resume_path.name, root=str(resume_path.parent), download=resume_path.name)
	finally:
		db.close()


@app.route("/api/history/<history_id>/reply", method="POST")
def api_history_reply(history_id):
	db = _get_web_db()
	try:
		body = request.json or {}
		message = str(body.get("message", "")).strip()
		if not message:
			return _json_response({"error": "回复内容不能为空"}, 400)

		row = db.execute(
			"SELECT id, job_id, action, detail FROM history WHERE id = ?",
			(history_id,),
		).fetchone()
		if not row:
			return _json_response({"error": "待回复记录不存在"}, 404)
		if row["action"] != "reply_pending":
			return _json_response({"error": "只能确认待回复记录"}, 400)

		# 面板点击即为必需的人为确认；实际浏览器操作交给单任务执行器，避免阻塞 API。
		task = task_runner.start(
			"reply",
			_task_config({"_reply_history_id": int(row["id"]), "_reply_message": message}),
		)
		return _json_response(task)
	except TaskAlreadyRunningError as e:
		return _json_response({"error": str(e)}, 409)
	except Exception as e:
		return _json_response({"error": str(e)}, 500)
	finally:
		db.close()


@app.route("/api/history/<history_id>/refine", method="POST")
def api_history_refine(history_id):
	"""根据用户的修改建议重新优化一条待确认回复，不执行发送。"""
	db = _get_web_db()
	try:
		body = request.json or {}
		suggestion = str(body.get("suggestion", "")).strip()
		current_reply = str(body.get("current_reply", "")).strip()
		if not suggestion:
			return _json_response({"error": "请填写修改建议"}, 400)
		if len(suggestion) > 1000 or len(current_reply) > 2000:
			return _json_response({"error": "修改建议或回复内容过长"}, 400)

		row = db.execute(
			"SELECT id, action, detail FROM history WHERE id = ?",
			(history_id,),
		).fetchone()
		if not row:
			return _json_response({"error": "待回复记录不存在"}, 404)
		if row["action"] != "reply_pending":
			return _json_response({"error": "只能优化待确认回复"}, 400)

		from bosshunter.executor.monitor import _parse_reply_detail, _refine_reply

		payload = _parse_reply_detail(row["detail"])
		hr_question = str(payload.get("hr_question", "")).strip()
		if not current_reply:
			current_reply = str(payload.get("ai_reply", "")).strip()
		if not current_reply:
			return _json_response({"error": "当前回复内容不能为空"}, 400)

		refined = _refine_reply(
			hr_question,
			current_reply,
			suggestion,
			load_config(CONFIG_PATH),
		)
		if not refined:
			return _json_response({"error": "AI 优化失败，请检查模型配置后重试"}, 502)
		return _json_response({"message": refined})
	except Exception as e:
		return _json_response({"error": str(e)}, 500)
	finally:
		db.close()


@app.route("/api/replies/generate", method="POST")
def api_replies_generate():
	"""手动粘贴 HR 消息，生成回复语（不写库、不发送，只返回文本）。

	监测不到 HR 回复时（列表扫描漏掉、会话没进候选等），用户可把 HR 的话
	粘贴进来，调用 AI 生成回复语，自行复制到 BOSS 发送。

	请求体 JSON：
	- hr_message: 必填，HR 的消息原文。
	- conversation_history: 可选，之前的对话记录，帮助模型理解上下文。
	- job_id: 可选，关联岗位 id；提供时读取该岗位的 title/company 让回复更贴合。
	"""
	body = request.json or {}
	hr_message = str(body.get("hr_message", "")).strip()
	conversation_history = str(body.get("conversation_history", "")).strip()
	job_id = str(body.get("job_id", "") or "").strip()

	if not hr_message:
		return _json_response({"error": "请填写 HR 的消息内容"}, 400)
	if len(hr_message) > 4000 or len(conversation_history) > 4000:
		return _json_response({"error": "内容过长"}, 400)

	# 可选：关联岗位上下文（只读，不修改）。
	job = None
	if job_id:
		db = _get_web_db()
		try:
			row = db.execute("SELECT id, title, company FROM jobs WHERE id = ?", (job_id,)).fetchone()
			if row:
				job = {"title": row["title"], "company": row["company"]}
		finally:
			db.close()

	from bosshunter.executor.monitor import _generate_reply_from_text

	reply = _generate_reply_from_text(hr_message, job, load_config(CONFIG_PATH), conversation_history)
	if not reply:
		return _json_response({"error": "AI 生成回复失败，请检查模型配置后重试"}, 502)
	return _json_response({"message": reply})


@app.route("/api/greetings/generate", method="POST")
def api_greetings_generate():
	"""手动粘贴岗位信息，生成招呼语（不写库、不发送，只返回文本）。

	岗位不在岗位池或采集不到完整信息时，用户可把岗位描述（JD）粘贴进来，
	调用 AI 基于当前简历生成招呼语，自行复制到 BOSS 发送。

	请求体 JSON：
	- jd: 必填，岗位描述原文。
	- title/company/salary/city: 可选，岗位名称/公司/薪资/城市。
	- review: 可选 bool，是否启用 AI 自检优化循环（默认关闭，交互更快）。
	"""
	body = request.json or {}
	jd = str(body.get("jd", "")).strip()
	title = str(body.get("title", "")).strip()
	company = str(body.get("company", "")).strip()
	salary = str(body.get("salary", "")).strip()
	city = str(body.get("city", "")).strip()
	review = bool(body.get("review", False))

	if not jd:
		return _json_response({"error": "请填写岗位描述（JD）"}, 400)
	if len(jd) > 4000:
		return _json_response({"error": "岗位描述过长"}, 400)
	for label, value in (
		("岗位名称", title),
		("公司", company),
		("薪资", salary),
		("城市", city),
	):
		if len(value) > 200:
			return _json_response({"error": f"{label}过长"}, 400)

	from bosshunter.ai.greeter import (
		_generate_for_job_with_review,
		_generate_with_deterministic_retry,
		_get_resume_summary,
	)

	config = load_config(CONFIG_PATH)
	resume_summary = _get_resume_summary(config)
	job = {
		"id": "",
		"title": title,
		"company": company,
		"salary": salary,
		"city": city,
		"district": "",
		"jd": jd,
		"company_background": "",
		"score_reason": "",
	}

	if review:
		result = _generate_for_job_with_review(job, resume_summary, config)
		greeting = result[1] if result else None
	else:
		greeting = _generate_with_deterministic_retry(job, resume_summary, config)

	if not greeting:
		return _json_response({"error": "AI 生成招呼语失败，请检查模型配置后重试"}, 502)
	return _json_response({"message": greeting})


# ─── LangGraph AI 助手 APIs ───────────────────────────────────────────────


@app.route("/api/assistant/conversations")
def api_assistant_conversations():
	"""列出聊天会话。"""
	from bosshunter.assistant.repository import list_conversations

	db = _get_web_db()
	try:
		return _json_response({"items": _serialize_db_items(list_conversations(db))})
	finally:
		db.close()


@app.route("/api/assistant/conversations", method="POST")
def api_assistant_conversation_create():
	"""创建聊天会话。"""
	from bosshunter.assistant.repository import create_conversation

	body = request.json or {}
	title = str(body.get("title", "新的求职对话")).strip()
	db = _get_web_db()
	try:
		return _json_response(create_conversation(db, title), 201)
	finally:
		db.close()


@app.route("/api/assistant/conversations", method="DELETE")
def api_assistant_conversations_delete():
	"""批量删除对话及其消息。"""
	from bosshunter.assistant.repository import delete_conversations

	body = request.json or {}
	conversation_ids = [str(item) for item in body.get("ids", []) if str(item)]
	db = _get_web_db()
	try:
		return _json_response({"deleted": delete_conversations(db, conversation_ids)})
	finally:
		db.close()


@app.route("/api/assistant/conversations/<conversation_id>", method="DELETE")
def api_assistant_conversation_delete(conversation_id):
	"""删除单个对话及其消息。"""
	from bosshunter.assistant.repository import delete_conversations

	db = _get_web_db()
	try:
		return _json_response({"deleted": delete_conversations(db, [conversation_id])})
	finally:
		db.close()


@app.route("/api/assistant/conversations/<conversation_id>/messages")
def api_assistant_messages(conversation_id):
	"""读取一个会话的消息。"""
	from bosshunter.assistant.repository import list_message_page

	try:
		limit = int(request.query.get("limit", "40"))
	except ValueError:
		return _json_response({"error": "limit 必须是整数"}, 400)
	before = str(request.query.get("before", "")).strip() or None
	db = _get_web_db()
	try:
		page = list_message_page(db, conversation_id, before=before, limit=limit)
		return _json_response({**page, "items": _serialize_db_items(page["items"])})
	except ValueError as exc:
		return _json_response({"error": str(exc)}, 400)
	finally:
		db.close()


@app.route("/api/assistant/conversations/<conversation_id>/messages", method="POST")
def api_assistant_message_send(conversation_id):
	"""向 LangGraph 会话发送一条消息。"""
	body = request.json or {}
	message = str(body.get("message", "")).strip()
	job_id = str(body.get("job_id", "")).strip() or None
	web_enabled = bool(body.get("web_enabled", False))
	if not message:
		return _json_response({"error": "请输入消息"}, 400)
	if len(message) > 8000:
		return _json_response({"error": "消息过长，请控制在 8000 字以内"}, 400)
	try:
		result = _get_assistant_service().send_message(
			conversation_id,
			message,
			job_id=job_id,
			web_enabled=web_enabled,
		)
		return _json_response(result)
	except Exception as exc:
		return _json_response({"error": f"AI 助手执行失败：{exc}"}, 502)


@app.route("/api/assistant/conversations/<conversation_id>/messages/stream", method="POST")
def api_assistant_message_stream(conversation_id):
	"""以 SSE 分块返回助手消息，保持与普通消息接口相同的持久化流程。"""
	body = request.json or {}
	message = str(body.get("message", "")).strip()
	job_id = str(body.get("job_id", "")).strip() or None
	web_enabled = bool(body.get("web_enabled", False))
	if not message:
		return _json_response({"error": "请输入消息"}, 400)
	if len(message) > 8000:
		return _json_response({"error": "消息过长，请控制在 8000 字以内"}, 400)
	response.content_type = "text/event-stream; charset=utf-8"
	response.set_header("Cache-Control", "no-cache")
	response.set_header("X-Accel-Buffering", "no")

	gate = _acquire_stream_gate(conversation_id)

	def events():
		"""在工作线程中执行 Agent，把模型 token 实时转发为 SSE。"""
		event_queue: Queue[tuple[str, object]] = Queue()

		def run_agent() -> None:
			"""执行阻塞式 LangGraph，同时将模型增量写入队列。"""
			print(
				f"[AI助手] 请求开始：conversation={conversation_id}，"
				f"web={'开启' if web_enabled else '关闭'}，job={job_id or '自动'}",
				flush=True,
			)
			try:
				result = _get_assistant_service().send_message(
					conversation_id,
					message,
					job_id=job_id,
					web_enabled=web_enabled,
					on_run=lambda run_id: event_queue.put(("start", run_id)),
					on_token=lambda token: event_queue.put(("delta", token)),
					on_event=lambda event: event_queue.put(("trace", event)),
				)
				content_length = len(str((result.get("message") or {}).get("content", "")))
				print(f"[AI助手] 请求完成：正文={content_length} 字，status={result.get('status')}", flush=True)
				event_queue.put(("done", result))
			except Exception as exc:
				print(f"[AI助手] 请求异常：{type(exc).__name__}: {exc}", flush=True)
				event_queue.put(("error", exc))

		Thread(target=run_agent, name=f"assistant-stream-{conversation_id}", daemon=True).start()
		try:
			while True:
				try:
					event_name, value = event_queue.get(timeout=15)
				except Empty:
					yield "event: ping\ndata: {}\n\n"
					continue
				if event_name in {"delta", "trace"}:
					# 暂停期间保持连接但不转发新内容，恢复后继续输出。
					while not gate.is_set():
						yield "event: ping\ndata: {}\n\n"
						gate.wait(timeout=10)
				if event_name == "start":
					yield f"event: start\ndata: {json.dumps({'run_id': value}, ensure_ascii=False)}\n\n"
					continue
				if event_name == "delta":
					yield f"event: delta\ndata: {json.dumps({'delta': value}, ensure_ascii=False)}\n\n"
					continue
				if event_name == "trace":
					yield f"event: trace\ndata: {json.dumps(value, ensure_ascii=False, default=str)}\n\n"
					continue
				if event_name == "error":
					error_payload = json.dumps({"error": f"AI 助手执行失败：{value}"}, ensure_ascii=False)
					yield f"event: error\ndata: {error_payload}\n\n"
					return
				result = value
				break

			metadata = {
				"id": (result.get("message") or {}).get("id"),
				"run_id": result.get("run_id"),
				"content": (result.get("message") or {}).get("content", ""),
				"citations": result.get("citations", []),
				"approval": result.get("approval"),
				"task": result.get("task"),
				"tool_trace": result.get("tool_trace", []),
				"execution_plan": result.get("execution_plan", []),
				"execution_events": result.get("execution_events", []),
				"dag_result": result.get("dag_result"),
				"evidence_route": result.get("evidence_route"),
				"evidence_tasks": result.get("evidence_tasks", []),
				"evidence_results": result.get("evidence_results", []),
				"evidence_coverage": result.get("evidence_coverage", {}),
				"status": result.get("status", "completed"),
			}
			yield f"event: done\ndata: {json.dumps(metadata, ensure_ascii=False, default=str)}\n\n"
		finally:
			_release_stream_gate(conversation_id)

	return events()


@app.route("/api/assistant/conversations/<conversation_id>/stream/pause", method="POST")
def api_assistant_stream_pause(conversation_id):
	"""暂停当前会话的流式输出，生成过程继续，恢复后补发。"""
	gate = _stream_gates.get(conversation_id)
	if gate is None:
		return _json_response({"error": "当前会话没有进行中的回答"}, 400)
	gate.clear()
	return _json_response({"conversation_id": conversation_id, "status": "paused"})


@app.route("/api/assistant/conversations/<conversation_id>/stream/resume", method="POST")
def api_assistant_stream_resume(conversation_id):
	"""恢复当前会话被暂停的流式输出。"""
	gate = _stream_gates.get(conversation_id)
	if gate is None:
		return _json_response({"error": "当前会话没有进行中的回答"}, 400)
	gate.set()
	return _json_response({"conversation_id": conversation_id, "status": "resumed"})


@app.route("/api/assistant/conversations/<conversation_id>/messages/partial", method="POST")
def api_assistant_partial_message(conversation_id):
	"""打断后保存已生成的部分回答，作为后续补充提问的上下文。"""
	body = request.json or {}
	content = str(body.get("content", "")).strip()
	run_id = str(body.get("run_id", "")).strip() or None
	if not content:
		return _json_response({"error": "没有可保存的部分回答"}, 400)
	from bosshunter.assistant.repository import add_message

	db = _get_web_db()
	try:
		saved = add_message(db, conversation_id, "assistant", content, "text", {
			"run_id": run_id,
			"status": "interrupted",
		})
		return _json_response(saved)
	finally:
		db.close()


@app.route("/api/assistant/runs/<run_id>/cancel", method="POST")
def api_assistant_run_cancel(run_id):
	"""请求当前 Agent Run 在下一个安全检查点停止。"""
	try:
		_get_assistant_service().cancel(str(run_id))
		return _json_response({"run_id": str(run_id), "status": "cancelled"})
	except Exception as exc:
		return _json_response({"error": str(exc)}, 400)


@app.route("/api/assistant/conversations/<conversation_id>/resume", method="POST")
def api_assistant_resume(conversation_id):
	"""恢复一个等待人工确认的 LangGraph 会话。"""
	body = request.json or {}
	action = str(body.get("action", "")).strip()
	if action not in {"approve", "edit", "reject"}:
		return _json_response({"error": "无效的确认操作"}, 400)
	decision = {"action": action}
	decision["action_id"] = str(body.get("action_id", "")).strip()
	decision["sql_hash"] = str(body.get("sql_hash", "")).strip()
	selected_item_ids = body.get("selected_item_ids", [])
	if isinstance(selected_item_ids, list):
		decision["selected_item_ids"] = [str(value) for value in selected_item_ids if str(value)]
	if action == "edit":
		edited_reply = str(body.get("edited_reply", "")).strip()
		if not edited_reply:
			return _json_response({"error": "请填写修改后的回复"}, 400)
		decision["edited_reply"] = edited_reply
	try:
		return _json_response(_get_assistant_service().resume(conversation_id, decision))
	except Exception as exc:
		return _json_response({"error": f"恢复会话失败：{exc}"}, 502)


@app.route("/api/assistant/memories")
def api_assistant_memories():
	"""列出可由用户管理的长期记忆。"""
	from bosshunter.assistant.repository import list_memories

	status = str(request.query.get("status", "active"))
	if status not in {"active", "history"}:
		return _json_response({"error": "无效的记忆状态"}, 400)
	db = _get_web_db()
	try:
		return _json_response({"items": _serialize_db_items(list_memories(db, status=status))})
	finally:
		db.close()


@app.route("/api/assistant/bad-cases", method="POST")
def api_assistant_bad_case_create():
	"""记录一条不理想回答，并自动附带意图和路由快照。"""
	from bosshunter.assistant.repository import create_bad_case

	body = request.json or {}
	conversation_id = str(body.get("conversation_id", "")).strip()
	message_id = str(body.get("message_id", "")).strip()
	category = str(body.get("category", "")).strip()
	if not conversation_id or not message_id or not category:
		return _json_response({"error": "缺少会话、消息或问题分类"}, 400)
	db = _get_web_db()
	try:
		item = create_bad_case(
			db,
			conversation_id,
			message_id,
			category,
			str(body.get("expected_intent", "")),
			str(body.get("feedback", "")),
		)
		return _json_response(item, 201)
	except ValueError as exc:
		return _json_response({"error": str(exc)}, 400)
	finally:
		db.close()


@app.route("/api/assistant/bad-cases")
def api_assistant_bad_cases():
	"""导出 Bad Case 数据，供离线评估和回归测试使用。"""
	from bosshunter.assistant.repository import list_bad_cases

	status = str(request.query.get("status", "open"))
	if status not in {"open", "resolved", "all"}:
		return _json_response({"error": "无效的 Bad Case 状态"}, 400)
	try:
		limit = int(request.query.get("limit", "200"))
	except ValueError:
		return _json_response({"error": "limit 必须是整数"}, 400)
	db = _get_web_db()
	try:
		return _json_response({"items": _serialize_db_items(list_bad_cases(db, status, limit))})
	finally:
		db.close()


@app.route("/api/assistant/message-ratings", method="POST")
def api_assistant_message_rating_create():
	"""记录每条助手回答下方的好坏手势。"""
	from bosshunter.assistant.repository import rate_assistant_message

	body = request.json or {}
	conversation_id = str(body.get("conversation_id", "")).strip()
	message_id = str(body.get("message_id", "")).strip()
	rating = str(body.get("rating", "")).strip()
	if not conversation_id or not message_id or not rating:
		return _json_response({"error": "缺少会话、消息或评价"}, 400)
	db = _get_web_db()
	try:
		return _json_response(rate_assistant_message(db, conversation_id, message_id, rating), 201)
	except ValueError as exc:
		return _json_response({"error": str(exc)}, 400)
	finally:
		db.close()


@app.route("/api/assistant/memories/<memory_id>", method="DELETE")
def api_assistant_memory_delete(memory_id):
	"""删除一条长期记忆。"""
	from bosshunter.assistant.repository import forget_memory

	db = _get_web_db()
	try:
		if not forget_memory(db, memory_id):
			return _json_response({"error": "记忆不存在"}, 404)
		return _json_response({"success": True})
	finally:
		db.close()


@app.route("/api/assistant/memories/<memory_id>", method="PATCH")
def api_assistant_memory_edit(memory_id):
	"""修改一条记忆，并保留被替换版本。"""
	from bosshunter.assistant.repository import edit_memory

	body = request.json or {}
	content = str(body.get("content", "")).strip()
	if not content:
		return _json_response({"error": "记忆内容不能为空"}, 400)
	db = _get_web_db()
	try:
		item = edit_memory(db, memory_id, content)
		if item is None:
			return _json_response({"error": "有效记忆不存在"}, 404)
		return _json_response(item)
	finally:
		db.close()


@app.route("/api/assistant/memories/<memory_id>/restore", method="POST")
def api_assistant_memory_restore(memory_id):
	"""把指定历史记忆恢复成新的有效版本。"""
	from bosshunter.assistant.repository import restore_memory

	db = _get_web_db()
	try:
		item = restore_memory(db, memory_id)
		if item is None:
			return _json_response({"error": "记忆不存在"}, 404)
		return _json_response(item)
	finally:
		db.close()


@app.route("/api/history/<history_id>/dismiss", method="POST")
def api_history_dismiss(history_id):
	db = _get_web_db()
	try:
		row = db.execute(
			"SELECT id, job_id, action, detail FROM history WHERE id = ?",
			(history_id,),
		).fetchone()
		if not row:
			return _json_response({"error": "待回复记录不存在"}, 404)
		if row["action"] != "reply_pending":
			return _json_response({"error": "只能放弃待回复记录"}, 400)

		from bosshunter.executor.monitor import _build_reply_resolution_detail

		add_history(
			db,
			row["job_id"],
			"reply_dismissed",
			_build_reply_resolution_detail(
				"reply_dismissed.v1",
				"Web Dashboard 放弃回复建议",
				row["detail"],
				pending_history_id=int(row["id"]),
			),
		)
		return _json_response({"success": True})
	finally:
		db.close()


# ─── Config APIs ─────────────────────────────────────────


@app.route("/api/config")
def api_config_get():
	try:
		config = _redact_config_for_response(load_config(CONFIG_PATH))
		return _json_response(config)
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/config", method="POST")
def api_config_post():
	try:
		import yaml

		data = request.json
		if not data:
			return _json_response({"error": "Empty body"}, 400)
		if not isinstance(data, dict):
			return _json_response({"error": "Config body must be an object"}, 400)
		data = _sanitize_config_for_write(data)

		# Basic validation
		profile = data.get("profile", {})
		if profile.get("salary_min", 0) > profile.get("salary_max", 0) and profile.get("salary_max", 0) > 0:
			return _json_response({"error": "salary_min must be <= salary_max"}, 400)

		# Write YAML (backend exclusively owns YAML serialization)
		with open(CONFIG_PATH, "w", encoding="utf-8") as f:
			yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

		return _json_response({"success": True, "message": "配置已保存"})
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/config/schema")
def api_config_schema():
	try:
		with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
			schema = json.load(f)
		return _json_response(schema)
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/config/download")
def api_config_download():
	"""下载脱敏后的配置副本，绝不泄露 API key / auth token 明文。

	浏览器只读得到打码后的配置，密钥通过 _mask_api_key 掩码展示。
	"""
	config = _redact_config_dict(load_config(CONFIG_PATH))
	import yaml
	body = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
	response.content_type = "application/x-yaml; charset=utf-8"
	response.headers["Content-Disposition"] = "attachment; filename=config.yaml"
	return body


@app.route("/api/config/cities")
def api_cities():
	return _json_response(CITY_CODES)


# ─── Resume APIs ─────────────────────────────────────────


@app.route("/api/resume")
def api_resume_get():
	try:
		config = load_config(CONFIG_PATH)
		resume_path = config.get("profile", {}).get("resume_path", "")
		if resume_path and Path(resume_path).exists():
			p = Path(resume_path)
			stat = p.stat()
			return _json_response(
				{
					"filename": p.name,
					"size": stat.st_size,
					"uploaded_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)),
					"path": str(p),
				}
			)
		return _json_response(None)
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/resume/upload", method="POST")
def api_resume_upload():
	try:
		import yaml

		upload = request.files.get("file")
		if not upload:
			return _json_response({"error": "No file uploaded"}, 400)

		# Validate extension
		name = upload.filename
		if not name.endswith(".md"):
			return _json_response({"error": "仅支持 .md 格式"}, 400)

		# Validate size (10MB max)
		content = upload.file.read()
		if len(content) > 10 * 1024 * 1024:
			return _json_response({"error": "文件大小超过 10MB 限制"}, 400)

		# Sanitize filename
		safe_name = Path(name).name.replace("..", "").replace("/", "").replace("\\", "")
		RESUME_DIR.mkdir(parents=True, exist_ok=True)
		dest = RESUME_DIR / safe_name
		dest.write_bytes(content)

		# Update config
		config = load_config(CONFIG_PATH)
		config.setdefault("profile", {})["resume_path"] = str(dest)
		with open(CONFIG_PATH, "w", encoding="utf-8") as f:
			yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

		return _json_response({"success": True, "filename": safe_name, "size": len(content), "path": str(dest)})
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/resume", method="DELETE")
def api_resume_delete():
	try:
		import yaml

		config = load_config(CONFIG_PATH)

		# Never delete the master resume from disk; only detach it from config.
		config.setdefault("profile", {})["resume_path"] = ""
		with open(CONFIG_PATH, "w", encoding="utf-8") as f:
			yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

		return _json_response({"success": True})
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/avatar")
def api_avatar_get():
	"""Return metadata for the locally configured resume portrait."""
	try:
		config = load_config(CONFIG_PATH)
		avatar_path = config.get("profile", {}).get("avatar_path", "")
		if avatar_path and Path(avatar_path).is_file():
			path = Path(avatar_path)
			stat = path.stat()
			return _json_response(
				{
					"filename": path.name,
					"size": stat.st_size,
					"uploaded_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)),
					"path": str(path),
				}
			)
		return _json_response(None)
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/avatar/upload", method="POST")
def api_avatar_upload():
	"""Save a local portrait and attach it to resume presentation config."""
	try:
		import yaml

		upload = request.files.get("file")
		if not upload:
			return _json_response({"error": "No file uploaded"}, 400)

		suffix = Path(upload.filename or "").suffix.lower()
		if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
			return _json_response({"error": "仅支持 JPG、PNG、WebP 图片"}, 400)

		content = upload.file.read()
		if not content:
			return _json_response({"error": "头像文件为空"}, 400)
		if len(content) > 5 * 1024 * 1024:
			return _json_response({"error": "头像大小超过 5MB 限制"}, 400)

		PROFILE_DIR.mkdir(parents=True, exist_ok=True)
		destination = PROFILE_DIR / f"avatar{suffix}"
		destination.write_bytes(content)

		config = load_config(CONFIG_PATH)
		config.setdefault("profile", {})["avatar_path"] = str(destination)
		with open(CONFIG_PATH, "w", encoding="utf-8") as f:
			yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

		return _json_response(
			{
				"success": True,
				"filename": destination.name,
				"size": len(content),
				"path": str(destination),
			}
		)
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/avatar", method="DELETE")
def api_avatar_delete():
	"""Detach the portrait without deleting the user's local image."""
	try:
		import yaml

		config = load_config(CONFIG_PATH)
		config.setdefault("profile", {})["avatar_path"] = ""
		with open(CONFIG_PATH, "w", encoding="utf-8") as f:
			yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
		return _json_response({"success": True})
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


# ─── Static Files + SPA Fallback ─────────────────────────


@app.route("/assets/<filepath:path>")
def serve_assets(filepath):
	return static_file(filepath, root=str(FRONTEND_DIR / "assets"))


@app.route("/")
@app.route("/<filepath:path>")
def serve_spa(filepath="index.html"):
	if str(filepath).startswith("api/"):
		return _json_response({"error": "Not found"}, 404)

	# Try serving the exact file first
	file_path = FRONTEND_DIR / filepath
	if file_path.is_file():
		return static_file(filepath, root=str(FRONTEND_DIR))
	# SPA fallback: return index.html for all non-API routes
	return static_file("index.html", root=str(FRONTEND_DIR))


# ─── Error Handlers ──────────────────────────────────────


@app.error(404)
def error404(error):
	if request.path.startswith("/api/"):
		response.content_type = "application/json; charset=utf-8"
		return json.dumps({"error": "Not found"}, ensure_ascii=False)
	# SPA fallback for non-API 404s
	return static_file("index.html", root=str(FRONTEND_DIR))


@app.error(500)
def error500(error):
	response.content_type = "application/json; charset=utf-8"
	return json.dumps({"error": str(error.body)}, ensure_ascii=False)


# ─── Run ─────────────────────────────────────────────────


class ThreadedWSGIRefServer(ServerAdapter):
	"""每请求一线程的 wsgiref 服务器适配器。

	Bottle 默认的 WSGIRefServer 是单线程 wsgiref——SSE 流会长时间占住唯一的
	工作线程，assistant 流式回答运行期间，暂停/恢复、任务管理等所有其他接口
	全部不可达（暂停后无法恢复，服务整体卡死）。这里用 ThreadingMixIn 让每个
	请求在独立线程中服务，SSE 长连接不再阻塞其他接口。
	"""

	def run(self, app):
		from socket import AF_INET, AF_INET6
		from socketserver import ThreadingMixIn
		from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

		class FixedHandler(WSGIRequestHandler):
			def address_string(self):
				# 避免对客户端地址做反向 DNS 查询。
				return self.client_address[0]

			def log_request(self, *args, **kw):
				if not getattr(self, "quiet", False):
					return WSGIRequestHandler.log_request(self, *args, **kw)

		class _Server(ThreadingMixIn, WSGIServer):
			daemon_threads = True
			request_queue_size = 64

		server_cls = _Server
		if ":" in self.host and getattr(server_cls, "address_family") == AF_INET:
			class server_cls(server_cls):  # noqa: F811 - bottle 同款 IPv6 修正
				address_family = AF_INET6

		self.srv = make_server(self.host, self.port, app, server_cls, FixedHandler)
		self.port = self.srv.server_port
		try:
			self.srv.serve_forever()
		except KeyboardInterrupt:
			self.srv.server_close()
			raise


def run_server(host: str = "127.0.0.1", port: int = 8686, open_browser: bool = False):
	"""Start the web server.

	绑定非回环地址（局域网暴露）且未设置 ``BOSSHUNTER_AUTH_TOKEN`` 时拒绝启动，
	防止未鉴权的管理面暴露到局域网。所有请求在监听入口统一校验 token。
	"""
	if _host_requires_auth(host):
		raise RuntimeError(
			"BOSSHUNTER_HOST 绑定到非回环地址时必须设置 BOSSHUNTER_AUTH_TOKEN 环境变量，"
			"否则拒绝启动 Web 服务"
		)

	if open_browser:
		import webbrowser
		import threading

		def _open():
			time.sleep(1)
			webbrowser.open(f"http://{host}:{port}")

		threading.Thread(target=_open, daemon=True).start()

	app.add_hook("before_request", _check_web_auth)
	app.run(host=host, port=port, quiet=False, reloader=False, server=ThreadedWSGIRefServer)
