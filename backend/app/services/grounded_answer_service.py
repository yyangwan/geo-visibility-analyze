"""Grounded answer generation from Bocha search results and DeepSeek."""

import asyncio
import re
import time
from typing import Any

import httpx

from app.adapters.base import ErrorCode, PlatformResponse
from app.config import settings
from app.services.bocha_search_service import (
    BochaSearchError,
    BochaSearchService,
    SearchResult,
)

_SOURCE_MARKER_RE = re.compile(r"\[S(\d+)\]")
_LATIN_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]{2,}")
_COMMON_SOURCE_WORDS = {
    "http",
    "https",
    "www",
    "com",
    "cn",
    "html",
    "coffee",
}


class GroundedAnswerService:
    """Search with Bocha, then synthesize an answer with DeepSeek citations."""

    def __init__(
        self,
        platform: str,
        api_key: str,
        base_url: str,
        model: str,
        platform_config: dict[str, Any] | None = None,
        runtime_context: dict[str, Any] | None = None,
        trace_headers: dict[str, str] | None = None,
    ):
        self.platform = platform
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.platform_config = platform_config or {}
        self.runtime_context = runtime_context or {}
        self.trace_headers = trace_headers or {}
        self.timeout = settings.query_timeout_seconds
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_per_platform)
        self._llm_client: httpx.AsyncClient | None = None
        self._search_service: BochaSearchService | None = None

    async def _get_llm_client(self) -> httpx.AsyncClient:
        if self._llm_client is None or self._llm_client.is_closed:
            self._llm_client = httpx.AsyncClient(timeout=self.timeout, trust_env=False)
        return self._llm_client

    def _get_search_service(self) -> BochaSearchService:
        if self._search_service is None:
            self._search_service = BochaSearchService()
        return self._search_service

    async def query(self, prompts: list[str]) -> list[PlatformResponse]:
        return await asyncio.gather(*(self._query_single(prompt) for prompt in prompts))

    async def _query_single(self, prompt: str) -> PlatformResponse:
        async with self.semaphore:
            start = time.monotonic()
            request_params: dict[str, Any] = {"search": {}, "llm": {}}

            try:
                grounding_config = self.platform_config.get("grounding", {})
                search_count = grounding_config.get("search_count", settings.bocha_result_count)
                top_k = grounding_config.get("top_k", settings.bocha_top_k)
                max_per_domain = grounding_config.get("max_per_domain", settings.bocha_max_per_domain)
                search_overrides = grounding_config.get("bocha_request", {})

                search_service = self._get_search_service()
                sources, search_payload, search_request = await search_service.search(
                    prompt,
                    count=search_count,
                    top_k=top_k,
                    max_per_domain=max_per_domain,
                    trace_headers=self.trace_headers,
                    request_overrides=search_overrides if isinstance(search_overrides, dict) else None,
                )
                request_params["search"] = search_request

                if not sources:
                    latency = int((time.monotonic() - start) * 1000)
                    return PlatformResponse(
                        platform=self.platform,
                        prompt=prompt,
                        response_text="没有检索到可用来源，无法基于 Bocha 搜索生成可引用回答。",
                        latency_ms=latency,
                        search_enabled=True,
                        raw_response={"search": search_payload, "selected_sources": []},
                        raw_response_text="",
                        search_metadata=self._build_search_metadata(prompt, [], []),
                        request_params=request_params,
                    )

                llm_request = self._build_llm_request(prompt, sources)
                request_params["llm"] = llm_request
                llm_response = await self._call_deepseek(llm_request)

                latency = int((time.monotonic() - start) * 1000)
                text = _extract_llm_text(llm_response)
                if text is None:
                    return PlatformResponse(
                        platform=self.platform,
                        prompt=prompt,
                        response_text="",
                        error_code=ErrorCode.FORMAT_ERROR,
                        error_message=f"Unexpected response format: {list(llm_response.keys())}",
                        latency_ms=latency,
                        raw_response=llm_response,
                        request_params=request_params,
                    )

                text = append_missing_source_names(text, sources)
                marker_indexes = parse_source_markers(text)
                citations = citations_for_markers(sources, marker_indexes)
                usage = llm_response.get("usage", {})

                return PlatformResponse(
                    platform=self.platform,
                    prompt=prompt,
                    response_text=text,
                    latency_ms=latency,
                    citations=citations,
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    response_model=llm_response.get("model", self.model),
                    finish_reason=llm_response.get("choices", [{}])[0].get("finish_reason", ""),
                    search_enabled=True,
                    raw_response={
                        "search": search_payload,
                        "selected_sources": [source.to_citation() for source in sources],
                        "llm_response": llm_response,
                    },
                    raw_response_text=text,
                    search_metadata=self._build_search_metadata(prompt, sources, marker_indexes),
                    request_params=request_params,
                )
            except BochaSearchError as exc:
                return self._error_response(prompt, start, request_params, ErrorCode.AUTH_FAILED, str(exc))
            except httpx.TimeoutException:
                return self._error_response(
                    prompt,
                    start,
                    request_params,
                    ErrorCode.TIMEOUT,
                    f"Timeout after {self.timeout}s",
                )
            except httpx.HTTPStatusError as exc:
                code = ErrorCode.AUTH_FAILED if exc.response.status_code == 401 else ErrorCode.PLATFORM_DOWN
                return self._error_response(prompt, start, request_params, code, str(exc))
            except Exception as exc:
                return self._error_response(prompt, start, request_params, ErrorCode.UNKNOWN, str(exc))

    def _build_llm_request(self, prompt: str, sources: list[SearchResult]) -> dict[str, Any]:
        request_config = self.platform_config.get("request", {})
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": build_grounded_prompt(prompt, sources)}],
            "temperature": request_config.get("temperature", 0.3),
        }
        max_tokens = request_config.get("max_tokens")
        if max_tokens:
            body["max_tokens"] = max_tokens
        return body

    async def _call_deepseek(self, request_body: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise BochaSearchError("DeepSeek API key is not configured")

        headers = {"Authorization": f"Bearer {self.api_key}"}
        headers.update(self.trace_headers)
        client = await self._get_llm_client()
        response = await client.post(
            f"{self.base_url}/chat/completions",
            headers=headers,
            json=request_body,
        )
        response.raise_for_status()
        return response.json()

    def _build_search_metadata(
        self,
        prompt: str,
        sources: list[SearchResult],
        marker_indexes: list[int],
    ) -> dict[str, Any]:
        return {
            "search_enabled": True,
            "search_triggered": True,
            "search_provider": "bocha",
            "grounding_mode": "bocha_grounded",
            "search_query": prompt,
            "search_queries": [prompt],
            "search_results_count": len(sources),
            "selected_results_count": len(sources),
            "selected_source_ids": [source.source_id for source in sources],
            "citation_markers": marker_indexes,
        }

    def _error_response(
        self,
        prompt: str,
        start: float,
        request_params: dict[str, Any],
        code: ErrorCode,
        message: str,
    ) -> PlatformResponse:
        latency = int((time.monotonic() - start) * 1000)
        return PlatformResponse(
            platform=self.platform,
            prompt=prompt,
            response_text="",
            error_code=code,
            error_message=message,
            latency_ms=latency,
            search_enabled=True,
            request_params=request_params,
        )


def build_grounded_prompt(prompt: str, sources: list[SearchResult]) -> str:
    source_lines = []
    for source in sources:
        source_lines.append(
            "\n".join(
                [
                    f"[{source.source_id}] title: {source.title}",
                    f"url: {source.url}",
                    f"domain: {source.domain}",
                    f"source_names: {', '.join(extract_source_names(source)) or 'none'}",
                    f"snippet: {source.snippet}",
                ]
            )
        )
    sources_text = "\n\n".join(source_lines)

    return (
        "你是一个基于搜索来源回答问题的助手。\n\n"
        f"USER_QUESTION:\n{prompt}\n\n"
        "SOURCES:\n"
        f"{sources_text}\n\n"
        "INSTRUCTIONS:\n"
        "- 用中文回答。\n"
        "- 只使用 SOURCES 中的信息，不要引入未给出的事实。\n"
        "- 必须单独列出 SOURCES 的标题或摘要中出现的具体品牌和型号名，并标注来源。\n"
        "- 每个关键结论后用 [S1]、[S2] 这样的来源标记引用对应来源。\n"
        "- 如果来源不足以回答，明确说明缺少哪些信息。\n"
        "- 不要直接输出未在 SOURCES 中出现的 URL。"
    )


def parse_source_markers(text: str) -> list[int]:
    seen: set[int] = set()
    markers: list[int] = []
    for match in _SOURCE_MARKER_RE.finditer(text):
        marker = int(match.group(1))
        if marker not in seen:
            markers.append(marker)
            seen.add(marker)
    return markers


def extract_source_names(source: SearchResult) -> list[str]:
    """Extract obvious brand/model-like names from a compact source."""
    text = source.title
    names: list[str] = []
    seen: set[str] = set()
    for match in _LATIN_NAME_RE.finditer(text):
        value = match.group(0).strip()
        key = value.lower()
        if key in _COMMON_SOURCE_WORDS or key in seen:
            continue
        names.append(value)
        seen.add(key)
    return names[:8]


def append_missing_source_names(text: str, sources: list[SearchResult]) -> str:
    """Append source-derived brand/model names if the model omitted them."""
    missing_by_source: list[tuple[SearchResult, list[str]]] = []
    text_lower = text.lower()

    for source in sources:
        names = [
            name for name in extract_source_names(source)
            if name.lower() not in text_lower
        ]
        if names:
            missing_by_source.append((source, names[:5]))

    if not missing_by_source:
        return text

    fragments = [
        f"{'、'.join(names)} [{source.source_id}]"
        for source, names in missing_by_source[:5]
    ]
    return (
        text.rstrip()
        + "\n\n"
        + "来源中还出现的品牌/型号包括："
        + "；".join(fragments)
        + "。"
    )


def citations_for_markers(sources: list[SearchResult], marker_indexes: list[int]) -> list[dict[str, Any]]:
    by_rank = {source.rank: source for source in sources}
    citations = []
    for marker in marker_indexes:
        source = by_rank.get(marker)
        if source:
            citations.append(source.to_citation())
    return citations


def _extract_llm_text(payload: dict[str, Any]) -> str | None:
    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
