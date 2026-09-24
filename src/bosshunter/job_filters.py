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
# 注意不要加宽泛的"咨询"：大量正常公司叫"XX信息咨询"，会误伤。
_AGENCY_KEYWORDS = (
    "代招", "猎头", "劳务派遣", "劳务外包", "人力外包", "外包", "派遣", "劳务",
    "人力资源", "人才服务", "人才中介", "人才招聘", "企业管理咨询",
    "人力",
)

_WHITESPACE_PATTERN = re.compile(r"\s+")

# JD 里自曝是外包/驻场岗的高精度短语。
#
# 刻意不匹配单词"外包"/"驻场"：大量自研公司的 JD 会写"非外包"、
# "接受大厂外包经历"、"管理外包团队"，单词匹配会大面积误伤。
# 真实反例（来自用户库内数据，已固化为回归测试）：
#   华云中盛「不要再问我是不是外包岗位了 T-T 公司内部产品研发岗位，非外包非外包！！」
#   谷米智能「接受大厂外包/OD/驻场经历，我们看重大厂带来的规范」
#   维尔迪  「串联硬件固件、屏幕 GUI、手机 APP 外包团队」
#   奥尼电子「接受短期/中长期客户现场驻场出差」
# 代价是召回率偏低（"接受出差与驻场"这类边界岗不会被命中），
# 这是刻意的取舍：宁可漏掉边界岗，也不要误杀正常公司。
#
# 也因此刻意不收「驻场交付」「现场驻场」这两个短语 —— "驻场"到底是
# 常驻派遣还是短期出差，短语层面分不出来，收进来会误伤 FDE 交付岗。
_AGENCY_JD_PATTERNS: tuple[tuple[str, str], ...] = (
    ("外包项目", r"外包项目"),
    ("外包岗位", r"外包岗位"),
    ("外包驻场", r"外包驻场"),
    ("人力外派", r"人力外派"),
    ("劳务派遣", r"劳务派遣"),
    ("劳务外包", r"劳务外包"),
    ("外派驻场", r"外派驻场"),
    ("驻场开发", r"驻场开发"),
    ("长期驻场", r"长期驻场"),
    ("银行外包", r"银行外包"),
    ("驻场招商", r"驻场招商"),
    ("项目制合作", r"项目制合作"),
    # "常驻<地点>现场"：公众「常驻联想现场」，地点名长度不固定，故留 0-6 字窗口。
    ("常驻…现场", r"常驻.{0,6}现场"),
)

_AGENCY_JD_COMPILED = tuple((label, re.compile(pattern)) for label, pattern in _AGENCY_JD_PATTERNS)

# 否定语境：命中这些说明"外包"二字指的是别的事，不是本岗位性质。
# 先于短语匹配剔除，避免"非外包岗位"被"外包岗位"命中。
_AGENCY_NEGATION_PATTERN = re.compile(
    "|".join(("非外包", "不是外包", "并非外包", "外包协调", "外包团队",
              "外包经验", "外包经历", "外包页面", "外包供应商", "管理外包"))
)


def matching_agency_phrase(text: str | None) -> str | None:
    """在 JD 文本中查找外包特征，返回命中特征的说明；未命中返回 None。

    匹配前先去掉所有空白：BOSS 的 JD 会插入换行、空格，甚至把
    「【银行 外包 驻场】」断成三截，不归一化就匹配不到。

    Args:
        text: JD 原文，可为空或 None。

    Returns:
        命中特征的展示文案；未命中返回 None。
    """
    if not text:
        return None
    normalized = _WHITESPACE_PATTERN.sub("", text)
    normalized = _AGENCY_NEGATION_PATTERN.sub("", normalized)
    for label, pattern in _AGENCY_JD_COMPILED:
        if pattern.search(normalized):
            return label
    return None


def is_agency_job(job: dict, config: dict | None = None) -> str | None:
    """综合判断岗位是否为代招/猎头/外包，返回命中证据；正常岗位返回 None。

    三道检查，从确定到模糊：

    1. 公司名命中 ``profile.blocked_companies``（用户维护的黑名单，最可靠）。
       用来兜住"JD 写得跟甲方一模一样"的外包公司 —— 例如中软国际、外企德科，
       它们的 JD 干净得像自研公司，只能靠名单。
    2. 公司名是"某大型XX公司"这类占位名，或含人力/劳务/猎头等词。
    3. JD 自曝外包（见 :data:`_AGENCY_JD_PHRASES`），可用
       ``profile.filter_agency_jobs: false`` 关闭。

    Args:
        job: 岗位字典，读取 ``company`` 与 ``jd`` 字段。
        config: 运行配置；读取 ``profile.blocked_companies`` 与
            ``profile.filter_agency_jobs``，两者均可缺省。

    Returns:
        命中的证据说明（写入评分原因，便于用户回溯为什么被过滤）；
        未命中返回 None。
    """
    profile = (config or {}).get("profile") or {}
    company = job.get("company") or ""

    # 白名单优先：用户显式说"这家公司的岗位我要"，比任何关键词推断都可信。
    # 放在这里而不是只交给调用方短路，是为了让优先级在过滤器层面就成立。
    if is_whitelisted_company(company, config):
        return None

    for blocked in profile.get("blocked_companies") or []:
        keyword = str(blocked).strip()
        # 子串匹配：BOSS 的公司名常被截断成"万宝盛华企业管理..."，
        # 用短前缀反而比全名更可靠。
        if keyword and keyword.lower() in company.lower():
            return f"公司黑名单: {keyword}"

    if is_agency_company(company):
        return "代招/猎头公司"

    if profile.get("filter_agency_jobs", True):
        phrase = matching_agency_phrase(job.get("jd"))
        if phrase:
            return f"JD 外包特征「{phrase}」"

    return None


def is_whitelisted_company(company: str | None, config: dict | None = None) -> bool:
    """公司名是否命中用户白名单。

    白名单的语义是"这家公司的岗位我要，别自动筛掉"：命中后跳过**全部**自动过滤
    （外包过滤、实习/薪资/注册资本预筛、AI 评分阈值），直接进入待人工确认，
    由用户自己逐条判断。评分照常执行并记录，只是不再据此淘汰。

    与 :func:`is_agency_job` 的 ``blocked_companies`` 互为反面；两个名单都命中时
    白名单优先——用户显式说"要"比关键词推断更可信。

    Args:
        company: 公司名字符串，可为空或 None。
        config: 运行配置，读取 ``profile.whitelist_companies``。

    Returns:
        True 表示在白名单中；未配置白名单或公司名为空时返回 False。
    """
    name = (company or "").strip().lower()
    if not name:
        return False
    profile = (config or {}).get("profile") or {}
    for allowed in profile.get("whitelist_companies") or []:
        keyword = str(allowed).strip().lower()
        # 与黑名单一致用子串匹配：BOSS 常把公司名截断成"华为技术有限..."。
        if keyword and keyword in name:
            return True
    return False


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
