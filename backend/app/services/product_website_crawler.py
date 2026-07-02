"""Crawler providers for product website visibility analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.config import settings


@dataclass
class ProductWebsiteCrawlResult:
    html: str
    final_url: str
    status_code: int | None = None
    method: str = "native_fetch"
    duration_ms: int | None = None
    content_length: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ProductWebsiteCrawler(Protocol):
    method: str

    async def fetch(self, url: str) -> ProductWebsiteCrawlResult:
        ...


class NativeProductWebsiteCrawler:
    method = "native_fetch"

    async def fetch(self, url: str) -> ProductWebsiteCrawlResult:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=settings.product_website_crawler_timeout_seconds,
            trust_env=False,
        ) as client:
            response = await client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 GeniLinkBot/1.0"},
            )
            response.raise_for_status()
            return ProductWebsiteCrawlResult(
                html=response.text,
                final_url=str(response.url),
                status_code=response.status_code,
                method=self.method,
                content_length=len(response.text),
                metadata={"contentType": response.headers.get("content-type")},
            )


class FirecrawlProductWebsiteCrawler:
    method = "firecrawl_scrape"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else settings.firecrawl_api_key
        self.base_url = (base_url or settings.firecrawl_base_url).rstrip("/")
        self.timeout_seconds = timeout_seconds or settings.product_website_crawler_timeout_seconds

    async def fetch(self, url: str) -> ProductWebsiteCrawlResult:
        if not self.api_key:
            raise RuntimeError("Firecrawl API key is not configured")

        payload = {
            "url": url,
            "formats": ["rawHtml", "html", "markdown"],
            "onlyMainContent": False,
            "removeBase64Images": True,
            "blockAds": True,
            "timeout": max(1000, self.timeout_seconds * 1000),
            "waitFor": max(0, settings.firecrawl_wait_for_ms),
            "maxAge": max(0, settings.firecrawl_max_age_ms),
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            response = await client.post(f"{self.base_url}/v2/scrape", json=payload, headers=headers)
            response.raise_for_status()
            body = response.json()

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            data = body if isinstance(body, dict) else {}

        html = self._extract_html(data)
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        final_url = (
            metadata.get("sourceURL")
            or metadata.get("url")
            or data.get("url")
            or data.get("sourceUrl")
            or url
        )

        return ProductWebsiteCrawlResult(
            html=html,
            final_url=str(final_url),
            status_code=response.status_code,
            method=self.method,
            content_length=len(html),
            metadata={
                "firecrawl": {
                    "scrapeId": body.get("id") if isinstance(body, dict) else None,
                    "success": body.get("success") if isinstance(body, dict) else None,
                    "metadata": metadata,
                    "usedFormat": self._detect_used_format(data),
                }
            },
        )

    @staticmethod
    def _extract_html(data: dict[str, Any]) -> str:
        raw_html = data.get("rawHtml") or data.get("raw_html")
        html = data.get("html")
        markdown = data.get("markdown")
        if isinstance(raw_html, str) and raw_html.strip():
            return raw_html
        if isinstance(html, str) and html.strip():
            return html
        if isinstance(markdown, str) and markdown.strip():
            return f"<html><body><pre>{markdown}</pre></body></html>"
        raise RuntimeError("Firecrawl response did not include usable page content")

    @staticmethod
    def _detect_used_format(data: dict[str, Any]) -> str:
        for key in ("rawHtml", "raw_html", "html", "markdown"):
            if isinstance(data.get(key), str) and data[key].strip():
                return key
        return "unknown"


def get_product_website_crawler(provider_override: str | None = None) -> ProductWebsiteCrawler:
    provider = (provider_override or settings.product_website_crawler_provider).strip().lower()
    if provider == "firecrawl" and settings.firecrawl_api_key:
        return FirecrawlProductWebsiteCrawler()
    return NativeProductWebsiteCrawler()
