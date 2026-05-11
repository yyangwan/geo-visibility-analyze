"""Source extraction from AI platform responses.

Three-tier approach for extracting cited sources:
- Tier 1: Structured citations from API response metadata (URLs, titles)
- Tier 1.5: Chinese named-source keyword detection ("知乎" → zhihu.com)
- Tier 2: URL regex fallback

Per D6: Tier 1 + Tier 1.5 + Tier 2 all run in parallel, merged by domain.
"""

import re
from dataclasses import dataclass, field

# ── Named-source keyword map (Tier 1.5) ──
# Chinese AI responses cite by name, not URL. Map common names → domains.
SOURCE_KEYWORD_MAP: dict[str, str] = {
    # Knowledge / Q&A
    "知乎": "zhihu.com",
    "zhihu": "zhihu.com",
    "百度百科": "baike.baidu.com",
    "百度知道": "zhidao.baidu.com",
    "百度文库": "wenku.baidu.com",
    "百度贴吧": "tieba.baidu.com",
    "维基百科": "wikipedia.org",
    # Social / Community
    "小红书": "xiaohongshu.com",
    "微信": "weixin.qq.com",
    "微信公众号": "weixin.qq.com",
    "微博": "weibo.com",
    "豆瓣": "douban.com",
    # Tech / Developer
    "CSDN": "csdn.net",
    "csdn": "csdn.net",
    "掘金": "juejin.cn",
    "Stack Overflow": "stackoverflow.com",
    "GitHub": "github.com",
    # News / Media
    "搜狐": "sohu.com",
    "网易": "163.com",
    "今日头条": "toutiao.com",
    "头条": "toutiao.com",
    # Video
    "抖音": "douyin.com",
    "B站": "bilibili.com",
    "bilibili": "bilibili.com",
    "哔哩哔哩": "bilibili.com",
    "YouTube": "youtube.com",
    # E-commerce
    "淘宝": "taobao.com",
    "京东": "jd.com",
    "天猫": "tmall.com",
    # Government / Official
    "中国政府网": "gov.cn",
    "国家统计局": "stats.gov.cn",
    # Academic
    "知网": "cnki.net",
    "中国知网": "cnki.net",
    "Google Scholar": "scholar.google.com",
}

# ── URL regex (Tier 2) ──
_URL_PATTERN = re.compile(r'https?://[^\s<>"\'）】\)】\)]+')


@dataclass
class ExtractedSource:
    """A source extracted from an AI response."""
    domain: str
    urls: list[str] = field(default_factory=list)
    title: str = ""


def extract_sources(
    response_text: str,
    api_citations: list[dict] | None = None,
) -> list[ExtractedSource]:
    """Extract cited sources from an AI platform response.

    Runs Tier 1 (structured) + Tier 1.5 (keyword) + Tier 2 (URL regex)
    in parallel, then merges by domain with normalization.

    Args:
        response_text: The raw AI platform response.
        api_citations: Structured citations from API metadata
                       [{url, title, domain?}]

    Returns:
        Deduplicated list of ExtractedSource, keyed by domain.
    """
    if not response_text:
        return []

    # Run all three tiers (all fast — regex + string matching)
    tier1_sources = _extract_tier1(api_citations or [])
    tier15_sources = _extract_tier15(response_text)
    tier2_sources = _extract_tier2(response_text)

    # Merge by domain (normalizes subdomains)
    return _merge_by_domain(tier1_sources + tier15_sources + tier2_sources)


def _extract_tier1(api_citations: list[dict]) -> list[ExtractedSource]:
    """Tier 1: Extract sources from structured API response metadata."""
    sources = []
    for cite in api_citations:
        url = cite.get("url", "")
        if not url:
            continue
        domain = _extract_domain(url)
        if not domain:
            continue
        sources.append(ExtractedSource(
            domain=domain,
            urls=[url],
            title=cite.get("title", ""),
        ))
    return sources


def _extract_tier15(text: str) -> list[ExtractedSource]:
    """Tier 1.5: Detect Chinese named-source references in text.

    Scans for keywords like "知乎", "百度百科" and maps them to domains.
    """
    sources = []
    seen_domains: set[str] = set()

    for keyword, domain in SOURCE_KEYWORD_MAP.items():
        if keyword in text and domain not in seen_domains:
            seen_domains.add(domain)
            sources.append(ExtractedSource(domain=domain))

    return sources


def _extract_tier2(text: str) -> list[ExtractedSource]:
    """Tier 2: URL regex fallback — extract URLs from plain text."""
    urls = _URL_PATTERN.findall(text)
    sources = []
    seen_domains: set[str] = set()

    for url in urls:
        domain = _extract_domain(url)
        if not domain or domain in seen_domains:
            continue
        seen_domains.add(domain)
        sources.append(ExtractedSource(domain=domain, urls=[url]))

    return sources


def _merge_by_domain(sources: list[ExtractedSource]) -> list[ExtractedSource]:
    """Merge sources by domain, combining URLs and keeping richest title.

    Normalizes subdomains: zhuanlan.zhihu.com → zhihu.com when the root
    domain appears in SOURCE_KEYWORD_MAP values. This ensures keyword
    matches ("知乎") and URL matches (zhuanlan.zhihu.com) merge correctly.
    """
    known_roots: set[str] = set(SOURCE_KEYWORD_MAP.values())

    by_domain: dict[str, ExtractedSource] = {}
    for src in sources:
        # Normalize to root domain if it's a known source
        normalized = _normalize_domain(src.domain, known_roots)

        if normalized in by_domain:
            existing = by_domain[normalized]
            existing_urls = set(existing.urls)
            for url in src.urls:
                if url not in existing_urls:
                    existing.urls.append(url)
                    existing_urls.add(url)
            if src.title and (not existing.title or len(src.title) > len(existing.title)):
                existing.title = src.title
        else:
            by_domain[normalized] = ExtractedSource(
                domain=normalized,
                urls=list(src.urls),
                title=src.title,
            )
    return list(by_domain.values())


def _normalize_domain(domain: str, known_roots: set[str]) -> str:
    """Normalize subdomain to root domain for known sources.

    zhuanlan.zhihu.com → zhihu.com (if zhihu.com is in known_roots)
    unknown.example.com → unknown.example.com (unchanged)
    """
    parts = domain.split(".")
    # Try stripping subdomains from left until we match a known root
    for i in range(len(parts) - 1):
        candidate = ".".join(parts[i:])
        if candidate in known_roots:
            return candidate
    return domain


def _extract_domain(url: str) -> str:
    """Extract registered domain from URL.

    Handles: https://www.zhihu.com/question/123 → zhihu.com
    Strips www. prefix for dedup purposes.
    """
    # Remove protocol
    domain = url.split("://", 1)[-1] if "://" in url else url
    # Remove path
    domain = domain.split("/", 1)[0]
    # Remove port
    domain = domain.split(":", 1)[0]
    # Strip www.
    if domain.startswith("www."):
        domain = domain[4:]
    return domain.lower() if domain else ""
