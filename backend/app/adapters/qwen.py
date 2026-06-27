"""Qwen (Tongyi Qianwen) platform adapter.

Uses DashScope's native generation API because the OpenAI-compatible endpoint
does not expose web-search source lists. The native endpoint returns
``output.search_info.search_results`` when source/citation search is enabled.
"""

import asyncio
import time

import httpx

from app.adapters.base import ErrorCode, PlatformResponse
from app.adapters.openai_compat import OpenAICompatAdapter
from app.config import settings


class QwenAdapter(OpenAICompatAdapter):
    platform_name = "qwen"
    search_enabled = True
    native_endpoint = "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation"

    def __init__(self):
        self.api_key = settings.qwen_api_key
        self.base_url = settings.qwen_base_url
        self.model = settings.qwen_model
        super().__init__()

    async def query(self, prompts: list[str]) -> list[PlatformResponse]:
        tasks = [self._query_single_native(prompt) for prompt in prompts]
        return await asyncio.gather(*tasks)

    def _build_native_request_body(self, prompt: str) -> dict:
        config = self.get_platform_config()
        request_config = config.get("request", {}) if isinstance(config, dict) else {}
        search_config = config.get("search", {}) if isinstance(config, dict) else {}
        search_options = dict(search_config.get("search_options") or {})
        search_options.setdefault("forced_search", True)
        search_options.setdefault("enable_source", True)
        search_options.setdefault("enable_citation", True)

        parameters = {
            "result_format": "message",
            "enable_search": search_config.get("enable_search", True),
            "search_options": search_options,
        }
        if "temperature" in request_config:
            parameters["temperature"] = request_config["temperature"]
        else:
            parameters["temperature"] = 0.3
        if request_config.get("max_tokens"):
            parameters["max_tokens"] = request_config["max_tokens"]

        return {
            "model": self.model,
            "input": {"messages": [{"role": "user", "content": prompt}]},
            "parameters": parameters,
        }

    async def _query_single_native(self, prompt: str) -> PlatformResponse:
        async with self.semaphore:
            start = time.monotonic()
            request_params = self._build_native_request_body(prompt)
            try:
                client = await self._get_client()
                response = await client.post(
                    self.native_endpoint,
                    headers=self._build_headers(),
                    json=request_params,
                )
                latency = int((time.monotonic() - start) * 1000)

                if response.status_code == 401:
                    return PlatformResponse(
                        platform=self.platform_name,
                        prompt=prompt,
                        response_text="",
                        error_code=ErrorCode.AUTH_FAILED,
                        error_message="Invalid API key",
                        latency_ms=latency,
                        request_params=request_params,
                    )
                if response.status_code == 429:
                    return PlatformResponse(
                        platform=self.platform_name,
                        prompt=prompt,
                        response_text="",
                        error_code=ErrorCode.RATE_LIMITED,
                        error_message="Rate limit exceeded",
                        latency_ms=latency,
                        request_params=request_params,
                    )

                response.raise_for_status()
                data = response.json()
                output = data.get("output", {})
                choices = output.get("choices", [])
                message = choices[0].get("message", {}) if choices else {}
                text = message.get("content")
                if not isinstance(text, str):
                    return PlatformResponse(
                        platform=self.platform_name,
                        prompt=prompt,
                        response_text="",
                        error_code=ErrorCode.FORMAT_ERROR,
                        error_message=f"Unexpected response format: {list(data.keys())}",
                        latency_ms=latency,
                        raw_response=data,
                        request_params=request_params,
                    )

                search_info = output.get("search_info") if isinstance(output, dict) else None
                search_results = search_info.get("search_results", []) if isinstance(search_info, dict) else []
                citations = _normalize_qwen_search_results(search_results)
                usage = data.get("usage", {})

                return PlatformResponse(
                    platform=self.platform_name,
                    prompt=prompt,
                    response_text=text,
                    latency_ms=latency,
                    citations=citations,
                    prompt_tokens=usage.get("input_tokens", usage.get("prompt_tokens", 0)),
                    completion_tokens=usage.get("output_tokens", usage.get("completion_tokens", 0)),
                    response_model=data.get("model", self.model),
                    finish_reason=choices[0].get("finish_reason", "") if choices else "",
                    search_enabled=True,
                    raw_response=data,
                    raw_response_text=text,
                    search_metadata={
                        "search_enabled": True,
                        "search_triggered": bool(citations),
                        "search_provider": "qwen_native",
                        "citation_mode": "dashscope_search_info",
                        "search_query": prompt,
                        "search_queries": [prompt],
                        "search_results_count": len(citations),
                    },
                    request_params=request_params,
                )
            except httpx.TimeoutException:
                return PlatformResponse(
                    platform=self.platform_name,
                    prompt=prompt,
                    response_text="",
                    error_code=ErrorCode.TIMEOUT,
                    error_message=f"Timeout after {self.timeout}s",
                    request_params=request_params,
                )
            except httpx.HTTPStatusError as exc:
                return PlatformResponse(
                    platform=self.platform_name,
                    prompt=prompt,
                    response_text="",
                    error_code=ErrorCode.PLATFORM_DOWN,
                    error_message=str(exc),
                    request_params=request_params,
                )
            except Exception as exc:
                return PlatformResponse(
                    platform=self.platform_name,
                    prompt=prompt,
                    response_text="",
                    error_code=ErrorCode.UNKNOWN,
                    error_message=str(exc),
                    request_params=request_params,
                )


def _normalize_qwen_search_results(search_results: object) -> list[dict]:
    if not isinstance(search_results, list):
        return []

    citations = []
    for result in search_results:
        if not isinstance(result, dict):
            continue
        url = result.get("url") or result.get("link")
        if not url:
            continue
        citations.append(
            {
                "url": url,
                "title": result.get("title") or result.get("site_name") or "",
                "domain": result.get("domain") or _extract_domain(url),
                "site_name": result.get("site_name"),
                "cite_index": result.get("index"),
                "provider": "qwen_native",
                "citation_mode": "dashscope_search_info",
            }
        )
    return citations


def _extract_domain(url: str) -> str | None:
    if not url or not isinstance(url, str):
        return None
    host = url.split("://")[-1].split("/")[0].split(":")[0].lower()
    return host.removeprefix("www.") if host else None
