"""AI Greeter - Generate personalized greeting messages with self-review."""

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from bosshunter.ai.credentials import call_anthropic_text
from bosshunter.db import get_db, get_jobs_needing_greeting, update_job_greeting, update_job_status, reset_job_greeting

console = Console()

# 并发生成招呼语时的默认线程数；过高会触发 AI 接口的速率限制。
DEFAULT_GREETING_CONCURRENCY = 4


def _greeting_concurrency(config: dict) -> int:
    """读取并钳制招呼语生成的并发度，至少为 1。"""
    raw = config.get("ai", {}).get("greeting_concurrency", DEFAULT_GREETING_CONCURRENCY)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_GREETING_CONCURRENCY

GREETING_PROMPT = """你是一位正在主动了解机会的求职者，需要在BOSS直聘上给招聘方发送第一条消息。请根据以下信息生成一条自然、具体、容易回复的招呼语。

## 我的背景
{resume_summary}

## 目标岗位与公司
- 职位：{title}
- 公司：{company}
- 薪资：{salary}
- 工作地点：{city}{district}
- 公司背景：{company_background}
- 岗位要求摘要：{jd_summary}
- 候选人匹配点：{match_reason}

## 额外亮点（适时融入，不要生硬罗列）
{extra_highlights}

## 写作方法
请写成 3 句连续的 IM 消息，控制在 55-110 字：
1. 固定用“您好，”自然开场，紧接岗位中最值得讨论的一个具体信号，例如业务场景、技术难点或当前建设重点；不要复述职位名称。
2. 只从简历中选择与该信号最相关的 1 条可核验经历，说明做过什么以及对应关系。不要堆技术名词或同时罗列多个项目；不同岗位应选择不同证据，不要总用同一个项目或同一组指标。
3. 最后必须提出一个招聘方容易直接回答的具体问题，优先使用“更侧重 A，还是 B？”“目前主要在解决 A 吗？”这类低门槛问法，引出岗位现状；不要用“我很愿意聊聊/补充案例”代替问题。

## 参考示例
✅ 正例（岗位信号+一条证据+容易回答的问题）：
“您好，这个岗位同时提到多轮交互和评测体系，我之前用 LangGraph 做过订单问数工作流，处理过状态流转和结果可追溯。想了解一下，团队现阶段更急的是 Agent 效果评测，还是生产稳定性？”
❌ 反例（套话开场，无具体证据）：
“您好，看到贵司在招聘这个岗位，我对这个岗位很感兴趣，相信我能胜任，希望有机会进一步沟通。”

## 硬性限制
1. 必须以“您好，”开头；禁止“看到贵司”“看到你们”“看到团队”“我对这个岗位很感兴趣”“我对该岗位”“相信我能胜任”“希望能有机会”“期待您的回复”等模板表达。
2. 禁止空泛形容词堆砌，例如“热爱学习、抗压、责任心强、综合能力强”。
3. 不要写分数、不要复述“缺失技能”，不要出现“JD”“HR”“贵司”等招聘话术。
4. 【严禁】不得捏造简历中没有的经历、头衔、数字或项目；不得把岗位 JD 的描述写成我的经历。
5. 作品集链接仅在简历明确包含、且与岗位直接相关时出现一次。
6. 必须以问号结束，只输出最终招呼语文本，不加标题、引号、编号或解释。
{critique_section}
"""

REVIEW_PROMPT = """请评估以下BOSS直聘招呼语的质量。

## 岗位
{title} @ {company}

## 招呼语
{greeting}

## 评估维度（每项1-10分）
1. 自然度：是否像真人发出的短消息，而非招聘模板
2. 相关性：是否有岗位切入点和一条具体、可核验的简历证据
3. 差异化：是否避开“看到贵司”“很感兴趣”等套话
4. 真实性：是否没有捏造候选人经历或误把岗位要求当经历
5. 可回复性：结尾是否为与岗位现状有关、招聘方容易直接回答的具体问题

请严格按JSON格式输出，不要输出其他内容：
{{"naturalness": 8, "relevance": 7, "differentiation": 6, "truthfulness": 10, "replyability": 8, "avg": 7.6, "critique": "改进建议（20字内）"}}
"""

_BANNED_GREETING_PATTERNS = (
    "你们好",
    "看到贵司",
    "看到你们",
    "看到团队",
    "看到Anker",
    "我对这个岗位很感兴趣",
    "我对该岗位",
    "相信我能胜任",
    "希望能有机会",
    "期待您的回复",
)


def _get_resume_summary(config: dict) -> str:
    """Get a brief resume summary for greeting generation."""
    resume_path = Path(config.get("profile", {}).get("resume_path", "./resume.md"))
    if not resume_path.exists():
        return ""
    content = resume_path.read_text(encoding="utf-8")
    return content[:1500]


def _call_claude(prompt: str, config: dict) -> str | None:
    """Call Claude API and return response text."""
    try:
        return call_anthropic_text(prompt, config, 300)
    except Exception as e:
        console.print(f"[red]API 调用失败: {e}[/red]")
        return None


def _review_greeting(greeting: str, job: dict, config: dict) -> dict | None:
    """Self-evaluate a greeting. Returns scores dict or None on failure."""
    prompt = REVIEW_PROMPT.format(
        title=job["title"],
        company=job["company"],
        greeting=greeting,
    )
    response = _call_claude(prompt, config)
    if not response:
        return None
    try:
        start = response.find("{")
        end = response.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(response[start:end])
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def _quality_issues(greeting: str) -> list[str]:
    """Return deterministic quality issues that an AI self-review can miss."""
    normalized = re.sub(r"\s+", "", greeting)
    issues: list[str] = []
    if len(normalized) < 45 or len(normalized) > 120:
        issues.append("长度不在 45-120 字范围内")
    if not normalized.startswith("您好，"):
        issues.append("没有以“您好，”自然开场")
    if any(pattern in normalized for pattern in _BANNED_GREETING_PATTERNS):
        issues.append("包含模板化开场或承诺")
    if normalized.count("我") > 5:
        issues.append("第一人称过多，语气不够自然")
    if not any(mark in normalized for mark in ("，", "。", "；", "！")):
        issues.append("缺少自然的短句结构")
    if not normalized.endswith(("？", "?")):
        issues.append("没有以完整、具体的问题收尾")
    if normalized.endswith(("，", "；", "、", "：")):
        issues.append("结尾是残句")
    return issues


def _generate_greeting_once(job: dict, resume_summary: str, config: dict, critique: str = "") -> str | None:
    """Generate a single greeting attempt."""
    jd_summary = job["jd"][:500] if job["jd"] else "无详细描述"
    critique_section = f"\n上次生成的问题：{critique}。请针对性修正。\n" if critique else ""

    # Build extra highlights from config (portfolio URL, personal strengths, etc.)
    profile_cfg = config.get("profile", {})
    highlights = profile_cfg.get("extra_highlights", [])
    portfolio_url = profile_cfg.get("portfolio_url", "")
    highlight_lines = [f"- {h}" for h in highlights]
    if portfolio_url:
        highlight_lines.append(f"- 个人作品集网址：{portfolio_url}")
    extra_highlights = "\n".join(highlight_lines) if highlight_lines else "（无额外亮点配置）"

    prompt = GREETING_PROMPT.format(
        resume_summary=resume_summary,
        title=job["title"],
        company=job["company"],
        salary=job["salary"] or "面议",
        city=job.get("city", ""),
        district=f"·{job['district']}" if job.get("district") else "",
        company_background=job.get("company_background") or job.get("company_industry") or "未采集",
        jd_summary=jd_summary,
        match_reason=(job.get("score_reason") or "").split("| 缺失:")[0],
        critique_section=critique_section,
        extra_highlights=extra_highlights,
    )

    greeting = _call_claude(prompt, config)
    if not greeting:
        return None

    # 不再硬截断模型输出。硬切可能留下“如果团队正在……”等残句，
    # 超长文本交给统一质量门禁触发重写，仍不合格则不落库。
    return re.sub(r"\s+", " ", greeting.strip('"\'')).strip()


def _generate_with_deterministic_retry(
	job: dict,
	resume_summary: str,
	config: dict,
) -> str | None:
	"""Generate once, then retry once if deterministic quality rules fail.

	Used by the scoring-stage batch path (``with_review=False``) so that
	banned-phrase/length issues are still caught without paying for an AI
	self-review. Only local rules run — no extra AI review API call. If the
	second attempt still fails the rules, the best result is returned anyway
	(no data loss), mirroring the lenient behaviour the scoring path had before.
	"""
	greeting = _generate_greeting_once(job, resume_summary, config)
	if not greeting:
		return None
	issues = _quality_issues(greeting)
	if not issues:
		return greeting
	# Deterministic rule failed: regenerate once with the critique surfaced.
	regenerated = _generate_greeting_once(
		job, resume_summary, config, critique="；".join(issues)
	)
	if regenerated and not _quality_issues(regenerated):
		return regenerated
	return None


def _generate_for_job_with_review(job: dict, resume_summary: str, config: dict) -> tuple[str, str] | None:
    """为单个岗位生成招呼语（含自审循环），线程安全。

    将原 generate_greetings 循环体的逻辑抽出为纯函数：不读写数据库、不打印 console，
    便于并发执行。唯一优化：当确定性规则 _quality_issues 已能指出问题时，跳过当轮的
    AI 审查调用（确定性 critique 已足够指导下一轮生成，避免每次迭代都额外调一次 AI）。
    其余行为与原串行版完全一致。

    Returns:
        (job_id, greeting) 成功；失败返回 None。
    """
    ai_cfg = config.get("ai", {})
    review_threshold = ai_cfg.get("greeting_review_threshold", 7.0)
    max_iterations = ai_cfg.get("greeting_max_iterations", 2)

    best_greeting: str | None = None

    for iteration in range(max_iterations + 1):
        critique = ""
        if iteration > 0 and best_greeting:
            deterministic_issues = _quality_issues(best_greeting)
            # 确定性规则已发现问题时直接用作 critique，跳过本轮 AI 审查；
            # 否则才花一次 AI 审查判定质量是否达标。
            if deterministic_issues:
                critique = "；".join(deterministic_issues)
            else:
                review = _review_greeting(best_greeting, job, config)
                review_passed = (
                    review
                    and review.get("avg", 0) >= review_threshold
                    and review.get("truthfulness", 0) >= 8
                    and review.get("replyability", 0) >= 7
                )
                if review_passed:
                    break  # 质量已达标
                critique_parts = [review.get("critique", "")] if review else []
                critique = "；".join(part for part in critique_parts if part)

        greeting = _generate_greeting_once(job, resume_summary, config, critique)
        if not greeting:
            break

        if iteration == 0:
            best_greeting = greeting
            if max_iterations == 0:
                break
        else:
            # 新生成的招呼语已融入上轮 critique，保留新结果。
            best_greeting = greeting

    if not best_greeting or _quality_issues(best_greeting):
        return None
    return (str(job["id"]), best_greeting)


def generate_greetings_batch(
    jobs: list[dict],
    resume_summary: str,
    config: dict,
    with_review: bool = True,
) -> list[tuple[str, str]]:
    """并发生成多个岗位的招呼语（线程池），返回 (job_id, greeting) 列表。

    scorer（评分阶段附带生成）与 generate_greetings（带自审的完整生成）的共享并发入口。
    仅对 AI 调用（I/O 阻塞）并发；本函数不读写数据库、不打印 console，调用方负责落库。
    并发度读取 config["ai"]["greeting_concurrency"]，默认 DEFAULT_GREETING_CONCURRENCY。

    Args:
        with_review: True 走 _generate_for_job_with_review（含自审循环），
            False 走 _generate_greeting_once（单次生成，无自审，用于评分阶段）。
    """
    if not jobs:
        return []

    concurrency = _greeting_concurrency(config)
    results: list[tuple[str, str]] = []

    with ThreadPoolExecutor(max_workers=min(concurrency, len(jobs))) as executor:
        if with_review:
            futures = [
                executor.submit(_generate_for_job_with_review, job, resume_summary, config)
                for job in jobs
            ]
        else:
            def _run_once(job: dict) -> tuple[str, str] | None:
                # 评分阶段也做一道确定性质量校验（禁词/长度），命中则重试 1 次，
                # 不调 AI 审查 API，避免禁词/套话招呼语直接落库。
                greeting = _generate_with_deterministic_retry(job, resume_summary, config)
                return (str(job["id"]), greeting) if greeting else None

            futures = [executor.submit(_run_once, job) for job in jobs]

        for future in futures:
            result = future.result()
            if result:
                results.append(result)

    return results


def generate_greetings(config: dict) -> int:
    """为缺少招呼语的已确认岗位生成招呼语（含自审循环）。返回生成数量。

    优先使用 get_jobs_needing_greeting 查询，只处理 status='approved' 且 greeting 为空的岗位，
    已有招呼语的岗位会被自动跳过（招呼语现在在评分阶段就已生成）。
    """
    db = get_db()
    jobs = get_jobs_needing_greeting(db)
    _workbench_job_ids = {str(job_id) for job_id in config.get("_workbench_job_ids", [])}
    if _workbench_job_ids:
        jobs = [job for job in jobs if str(job["id"]) in _workbench_job_ids]

    if not jobs:
        console.print("[yellow]没有已确认的岗位可生成招呼语。请先运行 `bosshunter confirm`，或使用 `bosshunter run` 执行完整流程。[/yellow]")
        db.close()
        return 0

    resume_summary = _get_resume_summary(config)
    if not resume_summary:
        console.print("[red]无法读取简历[/red]")
        db.close()
        return 0

    # 并发度：从 ai.greeting_concurrency 读取，默认 DEFAULT_GREETING_CONCURRENCY。
    # 仅对纯函数 _generate_for_job_with_review（AI 调用，I/O 阻塞）并发；
    # 数据库写入与进度更新回到主线程串行执行，规避 SQLite 跨线程与 Rich 并发打印。
    count = 0
    total = len(jobs)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console
    ) as progress:
        task = progress.add_task(f"生成招呼语 (0/{total})", total=total)

        with ThreadPoolExecutor(max_workers=min(_greeting_concurrency(config), total or 1)) as executor:
            futures = {
                executor.submit(_generate_for_job_with_review, job, resume_summary, config): job
                for job in jobs
            }
            # 流式收集：每完成一个就回主线程写库 + 推进进度，避免输出错乱与跨线程写库。
            for future in as_completed(futures):
                result = future.result()
                if result:
                    job_id, greeting = result
                    update_job_greeting(db, job_id, greeting)
                    update_job_status(db, job_id, "ready")
                    count += 1
                progress.update(task, advance=1, description=f"生成招呼语 ({count}/{total})")

    db.close()
    return count


def regenerate_greeting(job_id: str, config: dict) -> str | None:
    """重新生成单个岗位的招呼语。

    清空原有招呼语后重新调用 AI 生成，返回新的招呼语文本。
    生成失败返回 None。

    Args:
        job_id: 岗位 ID
        config: 应用配置

    Returns:
        新招呼语文本，失败返回 None
    """
    db = get_db()
    reset_job_greeting(db, [job_id])

    resume_summary = _get_resume_summary(config)
    if not resume_summary:
        console.print("[red]无法读取简历[/red]")
        db.close()
        return None

    # 获取岗位信息用于生成
    job = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not job:
        console.print(f"[red]未找到岗位: {job_id}[/red]")
        db.close()
        return None

    job_dict = dict(job)
    greeting = _generate_with_deterministic_retry(job_dict, resume_summary, config)

    if greeting:
        update_job_greeting(db, job_id, greeting)
        update_job_status(db, job_id, "ready")
        console.print(f"[green]✓ 招呼语已重新生成: {job_dict['company']} - {job_dict['title']}[/green]")
    else:
        console.print(f"[red]✗ 招呼语生成失败: {job_dict['company']} - {job_dict['title']}[/red]")

    db.close()
    return greeting


def regenerate_greetings(job_ids: list[str], config: dict) -> int:
    """批量重新生成招呼语。返回成功数量。

    Args:
        job_ids: 岗位 ID 列表
        config: 应用配置

    Returns:
        成功重新生成的数量
    """
    count = 0
    for job_id in job_ids:
        greeting = regenerate_greeting(job_id, config)
        if greeting:
            count += 1
    return count
