"""Real AI-platform citation checks for product website analysis."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from app.adapters.base import PlatformResponse
from app.adapters.registry import get_adapters
from app.config import settings


@dataclass
class ProductWebsiteCitationCheck:
    enabled: bool
    platforms: list[dict]
    prompts: list[str]


def configured_product_website_citation_platforms() -> list[str]:
    allowed = {"deepseek", "doubao", "hunyuan", "qwen", "kimi"}
    platforms = [
        item.strip().lower()
        for item in settings.product_website_ai_citation_platforms.split(",")
        if item.strip()
    ]
    return [platform for platform in platforms if platform in allowed]


def build_product_website_citation_prompts(input_snapshot: dict, target_url: str) -> list[str]:
    project = input_snapshot.get("project") or {}
    product_name = (project.get("product_name") or project.get("name") or "").strip()
    industry = (project.get("industry") or "").strip()
    keywords = [kw for kw in project.get("product_keywords", []) if isinstance(kw, str) and kw.strip()]
    domain = urlparse(target_url).hostname or target_url

    base_subject = product_name or domain
    keyword_hint = f"，重点关注 {', '.join(keywords[:4])}" if keywords else ""
    industry_hint = f"{industry}领域" if industry else "相关领域"
    prompts = [
        f"请搜索并总结{industry_hint}中 {base_subject} 的产品信息{keyword_hint}，回答时给出你引用的信息来源链接。",
        f"当用户想了解 {base_subject} 是否值得选择时，你会引用哪些网页或资料？请列出来源并简要说明理由。",
        f"请判断 {domain} 这个产品网站是否能支撑 AI 回答引用，并说明可引用的页面或证据。",
    ]
    limit = max(1, min(settings.product_website_ai_citation_prompt_limit, len(prompts)))
    return prompts[:limit]


async def run_product_website_citation_check(input_snapshot: dict, target_url: str) -> ProductWebsiteCitationCheck:
    if not settings.product_website_ai_citation_enabled:
        return ProductWebsiteCitationCheck(enabled=False, platforms=[], prompts=[])

    platforms = configured_product_website_citation_platforms()
    prompts = build_product_website_citation_prompts(input_snapshot, target_url)
    adapters = get_adapters(platforms)
    platform_results: list[dict] = []

    for adapter in adapters:
        adapter.set_runtime_context({"feature": "product_website_ai_citation"})
        try:
            responses = await adapter.query(prompts)
        except Exception as exc:  # pragma: no cover - adapter/network errors vary
            platform_results.append({
                "platform": adapter.platform_name,
                "status": "failed",
                "error": str(exc),
                "citationCount": 0,
                "mentionsProduct": False,
                "responses": [],
            })
            continue

        platform_results.append(_summarize_platform_responses(adapter.platform_name, responses, input_snapshot, target_url))

    return ProductWebsiteCitationCheck(enabled=True, platforms=platform_results, prompts=prompts)


def _summarize_platform_responses(
    platform: str,
    responses: list[PlatformResponse],
    input_snapshot: dict,
    target_url: str,
) -> dict:
    project = input_snapshot.get("project") or {}
    product_name = (project.get("product_name") or project.get("name") or "").strip().lower()
    domain = (urlparse(target_url).hostname or "").lower()
    citations: list[dict] = []
    response_items: list[dict] = []
    mentions_product = False

    for response in responses:
        text = response.response_text or ""
        text_lower = text.lower()
        if product_name and product_name in text_lower:
            mentions_product = True
        if domain and domain in text_lower:
            mentions_product = True

        response_citations = _normalize_citations(response.citations)
        citations.extend(response_citations)
        response_items.append({
            "prompt": response.prompt,
            "success": response.success,
            "latencyMs": response.latency_ms,
            "mentionsProduct": bool(
                (product_name and product_name in text_lower)
                or (domain and domain in text_lower)
            ),
            "citationCount": len(response_citations),
            "citations": response_citations[:10],
            "error": response.error_message,
            "searchEnabled": response.search_enabled,
            "responseModel": response.response_model,
        })

    unique_citations = _dedupe_citations(citations)
    own_domain_citations = [
        citation for citation in unique_citations
        if domain and domain in str(citation.get("url", "")).lower()
    ]
    return {
        "platform": platform,
        "status": "completed",
        "citationCount": len(unique_citations),
        "ownDomainCitationCount": len(own_domain_citations),
        "mentionsProduct": mentions_product,
        "citations": unique_citations[:20],
        "responses": response_items,
    }


def _normalize_citations(citations: list[dict] | None) -> list[dict]:
    normalized: list[dict] = []
    for citation in citations or []:
        if not isinstance(citation, dict):
            continue
        url = citation.get("url") or citation.get("link") or citation.get("source_url")
        title = citation.get("title") or citation.get("name") or citation.get("snippet")
        domain = citation.get("domain") or (urlparse(str(url)).hostname if url else None)
        if not url:
            continue
        normalized.append({
            "url": str(url),
            "title": str(title or ""),
            "domain": str(domain or ""),
        })
    return normalized


def _dedupe_citations(citations: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique: list[dict] = []
    for citation in citations:
        key = str(citation.get("url", "")).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(citation)
    return unique
