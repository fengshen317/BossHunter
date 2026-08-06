"""Shared job filtering helpers."""

import re


def matching_deal_breaker(title: str, deal_breakers: list[str]) -> str | None:
    """Return the first deal-breaker keyword found in a job title."""
    title_lower = title.lower()
    for keyword in deal_breakers:
        cleaned_keyword = keyword.strip()
        if cleaned_keyword and cleaned_keyword.lower() in title_lower:
            return keyword
    return None


_CAPITAL_UNIT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(亿|万)")


def parse_registered_capital_wan(text: str | None) -> float | None:
    """Parse a registered-capital string into a numeric value in "万" units.

    采集的公司详情页注册资本为字符串（如 "100万"、"1亿"、"100万人民币"）。
    这里统一换算为以"万"为单位的数值，方便阈值比较（如 50万、500万）。

    Args:
        text: 原始注册资本字符串，可为空或 None。

    Returns:
        "万"为单位的数值（亿 ×10000），无法解析时返回 None —— 调用方用 None
        区分"注册资本缺失/未知"与"明确低于阈值"，避免误伤未补全的岗位。
    """
    if not text:
        return None
    match = _CAPITAL_UNIT_PATTERN.search(text)
    if not match:
        return None
    try:
        number = float(match.group(1))
    except (TypeError, ValueError):
        return None
    return number * 10000 if match.group(2) == "亿" else number


# 代招/猎头/外包性质的公司名关键词。
# 这类公司替别的企业发岗位，往往公司名模糊或直接命名为人力/咨询类。
_AGENCY_KEYWORDS = (
    "代招", "猎头", "劳务派遣", "劳务外包", "人力外包",
    "人力资源", "人才服务", "人才中介", "人才招聘", "企业管理咨询",
)


def is_agency_company(company: str | None) -> bool:
    """判断公司名是否为代招/猎头/外包性质。

    代招岗位常用模糊占位公司名（如"深圳某大型计算机软件公司"、
    "某知名互联网公司"），这类公司名含"某"字且不是真实注册名；
    另有一类直接以"人力资源/猎头/劳务派遣/咨询"等词命名。

    Args:
        company: 公司名字符串，可为空或 None。

    Returns:
        True 表示建议过滤（代招/猎头/外包）；False 表示正常或无法判断。
    """
    name = (company or "").strip()
    if not name:
        return False
    # 模糊占位名：真实公司名几乎不含"某"，带"某"的基本是代招代发。
    if "某" in name:
        return True
    return any(keyword in name for keyword in _AGENCY_KEYWORDS)
