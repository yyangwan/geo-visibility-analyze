"""Deterministic source quality scoring for cited domains."""

from urllib.parse import urlparse

HIGH_AUTHORITY_DOMAINS = {
    "gov.cn",
    "stats.gov.cn",
    "samr.gov.cn",
    "edu.cn",
    "wikipedia.org",
    "baike.baidu.com",
    "cnki.net",
    "scholar.google.com",
}

INDUSTRY_MEDIA_DOMAINS = {
    "36kr.com",
    "huxiu.com",
    "ifanr.com",
    "ithome.com",
    "chinapp.com",
    "alibaba.com",
}

COMMUNITY_DOMAINS = {
    "zhihu.com",
    "xiaohongshu.com",
    "douban.com",
    "weibo.com",
    "bilibili.com",
}

ECOMMERCE_DOMAINS = {
    "taobao.com",
    "tmall.com",
    "jd.com",
    "amazon.com",
}

LOW_QUALITY_HINTS = (
    "mip.",
    "wap.",
    "m.",
    "list",
    "tag",
    "search",
)


def score_source_authority(domain: str, urls: list[str] | None = None, title: str | None = None) -> float:
    """Return a stable 1-5 authority score for a cited source.

    The score is intentionally conservative. It is used for reporting source
    quality only; it does not alter platform answers or source counts.
    """
    normalized = _normalize_domain(domain)
    if not normalized:
        return 0.0

    if normalized in HIGH_AUTHORITY_DOMAINS or normalized.endswith(".gov.cn") or normalized.endswith(".edu.cn"):
        score = 5.0
    elif normalized in INDUSTRY_MEDIA_DOMAINS:
        score = 4.0
    elif normalized in COMMUNITY_DOMAINS:
        score = 3.0
    elif normalized in ECOMMERCE_DOMAINS:
        score = 2.5
    else:
        score = 3.0

    title_text = (title or "").lower()
    if any(word in title_text for word in ("官方", "指南", "测评", "报告", "百科", "标准")):
        score += 0.3

    for url in urls or []:
        parsed_path = urlparse(url).path.lower()
        if any(hint in parsed_path for hint in LOW_QUALITY_HINTS):
            score -= 0.3
            break

    return round(max(1.0, min(score, 5.0)), 1)


def is_valid_source_domain(domain: str) -> bool:
    """Return whether a model-extracted source looks like a real domain."""
    normalized = _normalize_domain(domain)
    if not normalized:
        return False
    if normalized.startswith("source_"):
        return False
    if "." not in normalized:
        return False
    if any(char.isspace() for char in normalized):
        return False
    return True


def clean_cited_sources(sources: list[dict] | None) -> list[dict]:
    """Filter LLM-extracted cited sources down to real-looking domains."""
    clean_sources: list[dict] = []
    for source in sources or []:
        if not isinstance(source, dict):
            continue
        domain = _normalize_domain(str(source.get("domain", "")))
        if not is_valid_source_domain(domain):
            continue
        cleaned = dict(source)
        cleaned["domain"] = domain
        clean_sources.append(cleaned)
    return clean_sources


def _normalize_domain(domain: str) -> str:
    value = (domain or "").strip().lower()
    if "://" in value:
        value = urlparse(value).netloc.lower()
    value = value.split("/")[0].split(":")[0]
    return value.removeprefix("www.")
