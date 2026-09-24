"""Pre-filter module - hard filtering before LLM evaluation."""

import re

from bosshunter.job_filters import (
    is_agency_job,
    is_whitelisted_company,
    matching_deal_breaker,
    parse_registered_capital_wan,
)


_INTERNSHIP_KEYWORDS = ("实习", "intern", "internship", "管培")


def quick_score(job: dict, config: dict) -> tuple[int, str]:
    """Apply hard filters before LLM scoring."""
    profile = config.get("profile", {})
    deal_breakers = profile.get("deal_breakers", [])
    title = job.get("title") or ""

    # 白名单公司跳过全部自动预筛（外包过滤、实习、薪资、注册资本、一票否决词），
    # 直接进入待人工确认由用户判断。放在最前面：用户显式说"要"优先于所有关键词推断。
    if is_whitelisted_company(job.get("company"), config):
        return 100, "白名单公司（跳过预筛）"

    breaker = matching_deal_breaker(title, deal_breakers)
    if breaker:
        return 0, f"触发排除词: {breaker}"

    # 代招/猎头/外包：公司黑名单、模糊占位公司名（"某大型XX公司"），
    # 以及 JD 自曝外包/驻场的岗位。采集阶段只有列表卡片、拿不到 JD，
    # 这里在详情页补全后再拦一道。
    agency_reason = is_agency_job(job, config)
    if agency_reason:
        return 0, agency_reason

    if not profile.get("allow_internship", False) and _contains_internship_signal(job):
        return 0, "实习/管培岗位"

    salary_min = _as_number(profile.get("salary_min", 0))
    salary_max = _parse_salary_max_k(job.get("salary") or "")
    if salary_min > 0 and salary_max is not None and salary_max < salary_min:
        return 0, f"薪资低于硬性要求: {_format_k(salary_max)}K < {_format_k(salary_min)}K"

    # 注册资本硬过滤：只有明确解析出数值且低于阈值才排除。
    # 缺失/未补全（None）保留，避免误伤尚未 reenrich 的岗位。
    min_capital = _as_number(config.get("scoring", {}).get("min_registered_capital_wan", 50))
    capital = parse_registered_capital_wan(job.get("registered_capital") or "")
    if capital is not None and capital < min_capital:
        return 0, f"注册资本过低: {capital:.0f}万 < {min_capital:.0f}万"

    return 100, "预筛通过"


def _contains_internship_signal(job: dict) -> bool:
    title = (job.get("title") or "").lower()
    return any(keyword.lower() in title for keyword in _INTERNSHIP_KEYWORDS)


def _parse_salary_max_k(salary: str) -> float | None:
    range_match = re.search(r"(\d+(?:\.\d+)?)\s*[kK]?\s*-\s*(\d+(?:\.\d+)?)\s*[kK]", salary)
    if range_match:
        return max(float(range_match.group(1)), float(range_match.group(2)))

    single_match = re.search(r"(\d+(?:\.\d+)?)\s*[kK]", salary)
    if single_match:
        return float(single_match.group(1))

    return None


def _as_number(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0


def _format_k(value: float) -> str:
    return str(int(value)) if value.is_integer() else str(value)
