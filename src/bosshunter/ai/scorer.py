"""AI Scorer - Match jobs against resume using Claude API."""

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from bosshunter.ai.credentials import call_anthropic_text, get_anthropic_api_key
from bosshunter.db import get_db, get_jobs_by_status, update_job_score, update_job_status, update_job_quick_score, update_job_greeting, add_history
from bosshunter.ai.prefilter import quick_score
from bosshunter.ai.greeter import generate_greetings_batch

console = Console()


def _recover_jobs_with_failed_greetings(db, passed_jobs: list[dict]) -> int:
    """招呼语批量生成后，把仍无招呼语的 ready 岗位回退到 approved，使其可被 regenerate 恢复。

    评分阶段会先把通过的岗位置 ready，再批量生成招呼语；AI 返回空/异常时招呼语
    会缺失。若不处理，这些岗位会卡在 ready（sender 过滤空招呼语，永久不可见、不可发）。
    回退到 approved 后，get_jobs_needing_greeting 能重新选中它们，用户可一键 regenerate。
    写一条 error 历史让失败可见、可追溯。返回回退的岗位数。
    """
    recovered = 0
    for job in passed_jobs:
        row = db.execute("SELECT greeting FROM jobs WHERE id = ?", (job["id"],)).fetchone()
        greeting = (row["greeting"] if row else "") or ""
        if not greeting.strip():
            update_job_status(db, job["id"], "approved")
            add_history(db, job["id"], "error", "招呼语生成失败（AI 返回空或调用异常），已回退到待确认，可重新生成")
            recovered += 1
    if recovered:
        console.print(
            f"[yellow]  {recovered} 个岗位招呼语生成失败，已回退到待确认（approved），可在工作台重新生成[/yellow]"
        )
    return recovered


def _stop_requested(config: dict) -> bool:
    """Return whether the workbench has requested cooperative cancellation."""
    stop_event = config.get("_workbench_stop_event")
    return bool(stop_event and stop_event.is_set())

SCORING_PROMPT = """你是一位专业的求职顾问。请根据以下简历和岗位JD，评估候选人与该岗位的匹配度。

## 候选人简历
{resume}

## 候选人求职意向
{job_intent}

## 岗位信息
- 职位：{title}
- 公司：{company}
- 工作地点：{city}{district}
- 公司背景：{company_background}
- 注册资本：{registered_capital}
- 薪资：{salary}
- 要求：{experience}
- JD：{jd}

## 评估要求
请从以下维度评估匹配度，给出0-100的综合评分：
1. 职能技能匹配度（最重要）：候选人的核心职能技能是否覆盖岗位要求，这是评分的最主要依据
2. 工作年限匹配度：工作年限是否符合要求
3. 薪资合理性：期望薪资与岗位薪资是否匹配
4. 行业背景相关性（加分项，非必须）：有相关行业经验可以加分，但行业不同不应大幅扣分——职能能力可以跨行业迁移
5. 公司背景：结合公司行业、规模、融资或业务标签判断岗位稳定性与发展匹配度；信息缺失时保持中性，不可臆测
6. 注册资本：作为公司实力佐证，注册资本越高抗风险能力越强、可适当给更高分（例如≥1000万接近满分、<100万显著低分）；信息缺失时保持中性，不可臆测
7. 区域：结合具体区县与候选人目标城市判断通勤与地域匹配；未提供区县时只参考城市

**重要原则**：行业背景属于加分项而非硬性门槛。如果JD中某项行业要求标注为"优先"、"加分"或"更佳"而非"必须"，请不要将其作为扣分依据。评估时应从候选人简历中提取其真实掌握的领域与技术栈作为相关经验依据，不要因行业标签不同而过度扣分；也不要在 missing 中写出简历已体现的能力。

请严格按以下JSON格式输出，不要输出其他内容：
{{"score": 75, "reason": "匹配理由简述（50字内）", "missing": "缺失的关键技能或经验（30字内）}}
"""


def _load_resume(config: dict) -> str:
    """Load resume from configured path."""
    resume_path = Path(config.get("profile", {}).get("resume_path", "./resume.md"))
    if not resume_path.exists():
        return ""
    return resume_path.read_text(encoding="utf-8")


def _build_job_intent(config: dict) -> str:
    """Build a human-readable summary of the candidate's job preferences.

    Feeds the scorer's prompt so evaluation is driven by the candidate's
    actual intent (期望薪资/目标城市/排除项), not just raw resume text.
    Missing fields are omitted rather than emitting noise like "0K".
    """
    profile = config.get("profile", {}) if isinstance(config, dict) else {}
    parts: list[str] = []

    salary_min = profile.get("salary_min")
    salary_max = profile.get("salary_max")
    # Only surface salary when at least one bound is a positive number.
    try:
        lo = float(salary_min) if salary_min is not None else 0
        hi = float(salary_max) if salary_max is not None else 0
    except (TypeError, ValueError):
        lo = hi = 0
    if lo > 0 or hi > 0:
        hi_str = f"-{int(hi)}K" if hi > 0 else "K以上"
        parts.append(f"期望薪资：{int(lo)}{hi_str}")

    target_cities = profile.get("target_cities") or []
    if target_cities:
        parts.append("目标城市：" + "、".join(target_cities))

    deal_breakers = profile.get("deal_breakers") or []
    if deal_breakers:
        parts.append("排除：" + "、".join(deal_breakers))

    if not profile.get("allow_internship", False):
        parts.append("不接受实习/管培")

    return "；".join(parts) if parts else "（未配置明确求职意向，请主要依据简历评估）"


def _call_claude_with_retry(prompt: str, config: dict) -> str | None:
    """Wrap ``_call_claude`` with a single retry on API failure.

    Scoring failures used to silently leave jobs in ``pending``; one short
    retry (2s backoff) recovers transient network/API hiccups without
    multiplying cost on persistent errors. Parse failures are not retried
    (a bad response won't improve on re-send).
    """
    response = _call_claude(prompt, config)
    if response is not None:
        return response
    stop_event = config.get("_workbench_stop_event")
    if stop_event:
        if stop_event.wait(2):
            return None
    else:
        time.sleep(2)
    if _stop_requested(config):
        return None
    return _call_claude(prompt, config)


def _call_claude(prompt: str, config: dict) -> str | None:
    """Call Claude API and return response text."""
    if not get_anthropic_api_key(config):
        console.print("[red]未设置 ANTHROPIC_API_KEY 环境变量或 config.yaml ai.api_key[/red]")
        return None

    try:
        return call_anthropic_text(prompt, config, 256)
    except httpx.HTTPStatusError as e:
        provider = config.get("ai", {}).get("provider", "anthropic")
        if e.response.status_code == 404:
            console.print(
                f"[red]AI 接口返回 404：当前 provider={provider}，接口协议或 Base URL 不匹配。"
                "DeepSeek 等服务应选择 openai_compatible。[/red]"
            )
        else:
            console.print(f"[red]API 调用失败: HTTP {e.response.status_code}[/red]")
        return None
    except Exception as e:
        console.print(f"[red]API 调用失败: {e}[/red]")
        return None


def _parse_score_response(text: str) -> dict | None:
    """Parse JSON response from Claude."""
    try:
        # Try to find JSON in response
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
    except json.JSONDecodeError:
        pass
    return None


def _build_score_prompt(job: dict, resume: str, job_intent: str) -> str:
    """Build the immutable deep-scoring prompt for one job."""
    return SCORING_PROMPT.format(
        resume=resume[:3000],
        job_intent=job_intent,
        title=job["title"],
        company=job["company"],
        city=job.get("city", ""),
        district=f"·{job['district']}" if job.get("district") else "",
        company_background=job.get("company_background") or job.get("company_industry") or "未采集",
        registered_capital=job.get("registered_capital") or "未采集",
        salary=job["salary"],
        experience=job["experience"],
        jd=job["jd"][:2000],
    )


def _normalise_score(result: dict) -> tuple[int, str]:
    """Clamp an LLM scoring response and assemble the display reason."""
    try:
        score = max(0, min(100, int(round(float(result.get("score", 0))))))
    except (TypeError, ValueError):
        score = 0
    reason = str(result.get("reason", ""))
    missing = str(result.get("missing", ""))
    return score, f"{reason} | 缺失: {missing}" if missing else reason


def _score_jobs_sequential(config: dict) -> tuple[int, int]:
    """Legacy sequential scorer kept as a reference implementation."""
    db = get_db()
    resume = _load_resume(config)
    if not resume:
        console.print("[red]无法读取简历文件[/red]")
        db.close()
        return 0, 0

    threshold = config.get("scoring", {}).get("threshold", 60)
    job_intent = _build_job_intent(config)  # 求职意向在所有岗位间不变，循环外计算一次
    pending_jobs = get_jobs_by_status(db, "pending")

    if not pending_jobs:
        console.print("[yellow]没有待评分的岗位[/yellow]")
        db.close()
        return 0, 0

    scored = 0
    filtered = 0
    prefiltered = 0
    failed = 0
    # 评分通过的岗位在循环结束后批量并发生成招呼语（原为循环内串行生成，是评分阶段的次要瓶颈）。
    passed_jobs: list[dict] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console
    ) as progress:
        task = progress.add_task(f"评分中 (0/{len(pending_jobs)})", total=len(pending_jobs))

        for job in pending_jobs:
            if _stop_requested(config):
                console.print("[yellow]已收到停止请求，结束当前评分批次[/yellow]")
                break
            # Stage 1: Keyword pre-filter (free, no API calls)
            qs, qs_reason = quick_score(job, config)
            update_job_quick_score(db, job["id"], qs)

            if qs == 0:
                update_job_score(db, job["id"], qs, f"预筛不通过: {qs_reason}")
                update_job_status(db, job["id"], "filtered")
                filtered += 1
                prefiltered += 1
                progress.update(task, advance=1, description=f"评分中 ({scored+filtered}/{len(pending_jobs)}) [预筛淘汰{prefiltered}]")
                continue

            # Stage 2: LLM deep evaluation
            prompt = SCORING_PROMPT.format(
                resume=resume[:3000],
                job_intent=job_intent,
                title=job["title"],
                company=job["company"],
                city=job.get("city", ""),
                district=f"·{job['district']}" if job.get("district") else "",
                company_background=job.get("company_background") or job.get("company_industry") or "未采集",
                registered_capital=job.get("registered_capital") or "未采集",
                salary=job["salary"],
                experience=job["experience"],
                jd=job["jd"][:2000],
            )

            response = _call_claude_with_retry(prompt, config)
            if not response:
                failed += 1
                progress.update(
                    task,
                    advance=1,
                    description=f"评分中 ({scored+filtered+failed}/{len(pending_jobs)}) [失败{failed}]",
                )
                continue

            result = _parse_score_response(response)
            if not result:
                failed += 1
                progress.update(
                    task,
                    advance=1,
                    description=f"评分中 ({scored+filtered+failed}/{len(pending_jobs)}) [解析失败{failed}]",
                )
                continue

            # Clamp score to [0, 100] and coerce to int — LLMs occasionally
            # return 105, -5, "75", or 75.0, which breaks comparisons/sorting.
            try:
                score = max(0, min(100, int(round(float(result.get("score", 0))))))
            except (TypeError, ValueError):
                score = 0
            reason = result.get("reason", "")
            missing = result.get("missing", "")
            full_reason = f"{reason} | 缺失: {missing}" if missing else reason

            update_job_score(db, job["id"], score, full_reason)

            if score >= threshold:
                update_job_status(db, job["id"], "ready")
                # 评分通过：先收集，循环结束后批量并发生成招呼语（确认时用户可直接预览）。
                passed_jobs.append(job)
                scored += 1
            else:
                update_job_status(db, job["id"], "filtered")
                filtered += 1

            progress.update(task, advance=1, description=f"评分中 ({scored+filtered}/{len(pending_jobs)}) [预筛淘汰{prefiltered}]")

    if prefiltered > 0:
        console.print(f"[dim]  预筛阶段淘汰 {prefiltered} 个岗位（节省 {prefiltered} 次 API 调用）[/dim]")
    if failed > 0:
        console.print(f"[yellow]  {failed} 个岗位 AI 评分失败，已保留为待评分，可修正 AI 配置后重试[/yellow]")

    # 批量并发生成评分通过岗位的招呼语（失败不阻塞评分流程）。
    if passed_jobs and not _stop_requested(config):
        console.print(f"[dim]  为 {len(passed_jobs)} 个通过岗位并发生成招呼语...[/dim]")
        try:
            greetings = generate_greetings_batch(passed_jobs, resume[:1500], config, with_review=False)
            for job_id, greeting in greetings:
                update_job_greeting(db, job_id, greeting)
        except Exception as exc:  # noqa: BLE001 - 招呼语生成失败不阻塞评分流程
            console.print(f"[yellow]  招呼语生成出错（不阻塞评分）: {exc}[/yellow]")
        # 无论批量成功与否，都把仍无招呼语的 ready 岗位回退到 approved，避免卡死。
        _recover_jobs_with_failed_greetings(db, passed_jobs)

    db.close()
    return scored, filtered


def score_jobs(config: dict) -> tuple[int, int]:
    """Score pending jobs with bounded concurrent AI requests.

    The cheap keyword pre-filter and all SQLite writes stay on the caller
    thread. Only independent network-bound LLM calls run concurrently, which
    avoids sharing SQLite connections across threads and keeps cancellation
    deterministic for jobs that have not yet been persisted.
    """
    db = get_db()
    resume = _load_resume(config)
    if not resume:
        console.print("[red]无法读取简历文件[/red]")
        db.close()
        return 0, 0

    scoring_config = config.get("scoring", {})
    threshold = int(scoring_config.get("threshold", 60))
    try:
        concurrency = max(1, min(5, int(scoring_config.get("concurrency", 3))))
    except (TypeError, ValueError):
        concurrency = 3
    job_intent = _build_job_intent(config)
    pending_jobs = get_jobs_by_status(db, "pending")
    if not pending_jobs:
        console.print("[yellow]没有待评分的岗位[/yellow]")
        db.close()
        return 0, 0

    scored = 0
    filtered = 0
    prefiltered = 0
    failed = 0
    passed_jobs: list[dict] = []
    deep_jobs: list[tuple[dict, str]] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task(f"评分中 (0/{len(pending_jobs)})", total=len(pending_jobs))

        for job in pending_jobs:
            if _stop_requested(config):
                console.print("[yellow]已收到停止请求，结束当前评分批次[/yellow]")
                break
            quick, quick_reason = quick_score(job, config)
            update_job_quick_score(db, job["id"], quick)
            if quick == 0:
                update_job_score(db, job["id"], quick, f"预筛不通过: {quick_reason}")
                update_job_status(db, job["id"], "filtered")
                filtered += 1
                prefiltered += 1
                progress.update(task, advance=1, description=f"评分中 ({filtered}/{len(pending_jobs)}) [预筛淘汰{prefiltered}]")
                continue
            deep_jobs.append((job, _build_score_prompt(job, resume, job_intent)))

        executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="score")
        futures = {
            executor.submit(_call_claude_with_retry, prompt, config): job
            for job, prompt in deep_jobs
        }
        cancelled = False
        try:
            for future in as_completed(futures):
                if _stop_requested(config):
                    cancelled = True
                    for pending_future in futures:
                        pending_future.cancel()
                    break

                job = futures[future]
                try:
                    response = future.result()
                except Exception as exc:  # noqa: BLE001 - isolate a single provider failure
                    console.print(f"[yellow]  {job['title']} 评分请求失败: {exc}[/yellow]")
                    response = None
                result = _parse_score_response(response) if response else None
                if not result:
                    failed += 1
                else:
                    score, reason = _normalise_score(result)
                    update_job_score(db, job["id"], score, reason)
                    if score >= threshold:
                        update_job_status(db, job["id"], "ready")
                        passed_jobs.append(job)
                        scored += 1
                    else:
                        update_job_status(db, job["id"], "filtered")
                        filtered += 1
                completed = scored + filtered + failed
                progress.update(task, advance=1, description=f"评分中 ({completed}/{len(pending_jobs)}) [并发{concurrency}]" )
        finally:
            executor.shutdown(wait=not cancelled, cancel_futures=cancelled)

    if prefiltered:
        console.print(f"[dim]预筛淘汰 {prefiltered} 个岗位，未调用 AI。[/dim]")
    if failed:
        console.print(f"[yellow]{failed} 个岗位评分失败，保留为待评分，可稍后重试。[/yellow]")

    if passed_jobs and not _stop_requested(config):
        console.print(f"[dim]为 {len(passed_jobs)} 个通过岗位并发生成招呼语...[/dim]")
        try:
            greetings = generate_greetings_batch(passed_jobs, resume[:1500], config, with_review=False)
            for job_id, greeting in greetings:
                update_job_greeting(db, job_id, greeting)
        except Exception as exc:  # noqa: BLE001 - greetings must not roll back scoring
            console.print(f"[yellow]招呼语生成出错（不阻塞评分）: {exc}[/yellow]")
        # 无论批量成功与否，都把仍无招呼语的 ready 岗位回退到 approved，避免卡死。
        _recover_jobs_with_failed_greetings(db, passed_jobs)

    db.close()
    return scored, filtered
