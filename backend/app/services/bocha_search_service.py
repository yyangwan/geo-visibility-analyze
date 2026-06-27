"""Bocha web search client and result normalization."""

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from app.config import settings


class BochaSearchError(Exception):
    """Raised when Bocha search cannot be completed."""


@dataclass(frozen=True)
class SearchResult:
    source_id: str
    title: str
    url: str
    domain: str
    snippet: str
    published_at: str | None
    rank: int
    query: str
    provider: str = "bocha"

    def to_citation(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "cite_index": self.rank,
            "title": self.title,
            "url": self.url,
            "domain": self.domain,
            "snippet": self.snippet,
            "published_at": self.published_at,
            "provider": self.provider,
        }


class BochaSearchService:
    """Thin Bocha search wrapper with tolerant response parsing.

    Bocha payload shape can vary by endpoint/version, so normalization accepts
    the common result containers and field aliases.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        endpoint: str | None = None,
        timeout: int | None = None,
    ):
        self.api_key = api_key if api_key is not None else settings.bocha_api_key
        self.base_url = (base_url or settings.bocha_base_url).rstrip("/")
        self.endpoint = endpoint or settings.bocha_search_endpoint
        self.timeout = timeout or settings.query_timeout_seconds
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout, trust_env=False)
        return self._client

    def _url(self) -> str:
        if self.endpoint.startswith(("http://", "https://")):
            return self.endpoint
        endpoint = self.endpoint if self.endpoint.startswith("/") else f"/{self.endpoint}"
        return f"{self.base_url}{endpoint}"

    def _headers(self, trace_headers: dict[str, str] | None = None) -> dict[str, str]:
        if not self.api_key:
            raise BochaSearchError("Bocha API key is not configured")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if trace_headers:
            headers.update(trace_headers)
        return headers

    async def search(
        self,
        query: str,
        count: int | None = None,
        top_k: int | None = None,
        max_per_domain: int | None = None,
        trace_headers: dict[str, str] | None = None,
        request_overrides: dict[str, Any] | None = None,
    ) -> tuple[list[SearchResult], dict[str, Any], dict[str, Any]]:
        result_count = count or settings.bocha_result_count
        body: dict[str, Any] = {
            "query": query,
            "count": result_count,
            "summary": True,
        }
        if request_overrides:
            body.update(request_overrides)

        client = await self._get_client()
        response = await client.post(self._url(), headers=self._headers(trace_headers), json=body)
        response.raise_for_status()
        payload = response.json()

        results = normalize_bocha_results(
            payload,
            query=query,
            top_k=top_k,
            max_per_domain=max_per_domain,
        )
        return results, payload, {"url": self._url(), "json": body}


def normalize_bocha_results(
    payload: Any,
    query: str,
    top_k: int | None = None,
    max_per_domain: int | None = None,
) -> list[SearchResult]:
    raw_items = _extract_result_items(payload)
    results: list[SearchResult] = []
    seen_urls: set[str] = set()
    domain_counts: dict[str, int] = {}
    domain_limit = max(max_per_domain or settings.bocha_max_per_domain, 1)

    for item in raw_items:
        if not isinstance(item, dict):
            continue

        url = _first_str(item, ("url", "link", "href"))
        if not url:
            continue

        normalized_url = _normalize_url(url)
        if not normalized_url or normalized_url in seen_urls:
            continue

        domain = _domain(normalized_url)
        if not domain:
            continue

        if domain_counts.get(domain, 0) >= domain_limit:
            continue

        seen_urls.add(normalized_url)
        domain_counts[domain] = domain_counts.get(domain, 0) + 1
        rank = len(results) + 1
        title = _first_str(item, ("title", "name")) or domain
        snippet = _first_str(item, ("snippet", "summary", "content", "description")) or ""
        published_at = _first_str(item, ("published_at", "publishedAt", "publishedDate", "date"))

        results.append(
            SearchResult(
                source_id=f"S{rank}",
                title=title,
                url=normalized_url,
                domain=domain,
                snippet=snippet,
                published_at=published_at,
                rank=rank,
                query=query,
            )
        )

    result_limit = max(top_k or settings.bocha_top_k, 1)
    return results[:result_limit]


def _extract_result_items(payload: Any) -> list[Any]:
    containers = [
        ("data", "webPages", "value"),
        ("webPages", "value"),
        ("data", "results"),
        ("data", "list"),
        ("results",),
        ("items",),
        ("data",),
    ]
    for path in containers:
        value = payload
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        if isinstance(value, list):
            return value
    return payload if isinstance(payload, list) else []


def _first_str(item: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = item.get(key)
        if value is None:
            continue
        value_str = str(value).strip()
        if value_str:
            return value_str
    return None


def _normalize_url(url: str) -> str | None:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    normalized = parsed._replace(fragment="", netloc=parsed.netloc.lower())
    return urlunparse(normalized)


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")
