"""Shared base for OpenAI-compatible platform adapters.

All domestic AI platforms (DeepSeek, Qwen, Doubao, Kimi, Hunyuan)
support the OpenAI chat completions protocol, so we extract common logic here.
"""

import asyncio
import time

import httpx

from app.adapters.base import (
    ErrorCode,
    PlatformAdapter,
    PlatformResponse,
)
from app.config import settings
from app.services.response_parser import (
    extract_citations,
    extract_search_metadata,
    extract_with_parse_fallback,
)


class OpenAICompatAdapter(PlatformAdapter):
    """Adapter for any platform that implements OpenAI-compatible chat/completions."""

    platform_name: str = ""
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    # Subclasses override to enable platform-specific search mode
    search_enabled: bool = False

    def __init__(self):
        super().__init__()  # Initialize base class (sets up _platform_config)
        self.timeout = settings.query_timeout_seconds
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_per_platform)
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                trust_env=False,  # bypass system proxy — domestic APIs don't need it
            )
        return self._client

    def _build_headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        headers.update(self.build_trace_headers())
        return headers

    def _is_rate_limited_response(self, resp: httpx.Response) -> bool:
        """Detect rate limits across OpenAI-compatible providers.

        Some providers return rate-limit errors as HTTP 400 with an error code
        instead of HTTP 429.
        """
        if resp.status_code == 429:
            return True
        if resp.status_code != 400:
            return False

        try:
            data = resp.json()
        except Exception:
            return False

        error = data.get("error") if isinstance(data, dict) else None
        if not isinstance(error, dict):
            return False

        error_type = str(error.get("type", "")).lower()
        error_code = str(error.get("code", "")).lower()
        message = str(error.get("message", "")).lower()
        return (
            "rate_limit" in error_type
            or "rate_limit" in error_code
            or "rate limit" in message
            or "请求频繁" in message
        )

    async def query(self, prompts: list[str]) -> list[PlatformResponse]:
        tasks = [self._query_single(p) for p in prompts]
        return await asyncio.gather(*tasks)

    def _build_request_body(self, prompt: str) -> dict:
        """Build the JSON body for the API request. Subclasses override for search mode.

        Issue 2.1: Merges platform config into request body.
        """
        # Start with base request
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
        }

        # Set default temperature
        body["temperature"] = 0.3

        # Merge platform config if available
        config = self.get_platform_config()
        if config:
            # Merge request defaults from config
            request_config = config.get("request", {})
            if "temperature" in request_config:
                body["temperature"] = request_config["temperature"]
            if "max_tokens" in request_config and request_config["max_tokens"]:
                body["max_tokens"] = request_config["max_tokens"]

            # Merge search config
            search_config = config.get("search", {})
            enable_search = search_config.get("enable_search", self.search_enabled)

            if enable_search:
                # For platforms that use enable_search flag (DeepSeek-style)
                body["enable_search"] = True
                # Merge search_options if provided, otherwise use default
                if "search_options" in search_config:
                    body["search_options"] = search_config["search_options"]
                else:
                    body["search_options"] = {"forced_search": True}

                # Platform-specific tools (Kimi-style)
                if "tools" in search_config:
                    body["tools"] = search_config["tools"]
        else:
            # Use defaults from adapter
            if self.search_enabled:
                body["enable_search"] = True
                body["search_options"] = {"forced_search": True}

        return body

    def _extract_citations_with_error(self, data: dict) -> tuple[list[dict], str | None]:
        """Extract structured citations from API response with parse fallback.

        Issue 3.1: Uses response_parser for platform-agnostic citation extraction.
        """
        config = self.get_platform_config()
        parsing_config = config.get("parsing", {})

        citation_format = parsing_config.get("citation_format", "none")
        citation_path = parsing_config.get("citation_path")

        return extract_with_parse_fallback(
            data,
            lambda payload: extract_citations(payload, citation_format, citation_path),
            default_value=[],
        )

    def _extract_citations(self, data: dict) -> list[dict]:
        """Extract structured citations from API response."""
        citations, _ = self._extract_citations_with_error(data)
        return citations

    def _extract_search_metadata_with_error(self, data: dict) -> tuple[dict | None, str | None]:
        """Extract search metadata from API response with parse fallback.

        Issue 3.2: Uses response_parser for platform-agnostic search metadata extraction.

        Returns dict with:
        - search_enabled: bool
        - search_triggered: bool
        - search_query: str | None
        - search_reasoning: str | None
        - search_results_count: int
        """
        config = self.get_platform_config()
        parsing_config = config.get("parsing", {})

        return extract_with_parse_fallback(
            data,
            lambda payload: extract_search_metadata(
                payload,
                search_enabled=self.search_enabled or config.get("search", {}).get("enable_search", False),
                search_status_path=parsing_config.get("search_status_path"),
                search_query_path=parsing_config.get("search_query_path"),
                search_reasoning_path=parsing_config.get("search_reasoning_path"),
                search_results_path=parsing_config.get("search_results_path"),
            ),
            default_value=None,
        )

    def _extract_search_metadata(self, data: dict) -> dict | None:
        """Extract search metadata from API response."""
        search_metadata, _ = self._extract_search_metadata_with_error(data)
        return search_metadata

    # Max retries on rate-limit (429) responses
    _rate_limit_retries: int = 3

    async def _query_single(self, prompt: str) -> PlatformResponse:
        async with self.semaphore:
            start = time.monotonic()
            request_params = self._build_request_body(prompt)
            try:
                client = await self._get_client()

                # Retry loop for rate-limiting
                for attempt in range(self._rate_limit_retries + 1):
                    resp = await client.post(
                        f"{self.base_url}/chat/completions",
                        headers=self._build_headers(),
                        json=request_params,
                    )
                    if (
                        not self._is_rate_limited_response(resp)
                        or attempt == self._rate_limit_retries
                    ):
                        break
                    await asyncio.sleep(2 ** attempt)

                latency = int((time.monotonic() - start) * 1000)

                if resp.status_code == 401:
                    return PlatformResponse(
                        platform=self.platform_name,
                        prompt=prompt,
                        response_text="",
                        error_code=ErrorCode.AUTH_FAILED,
                        error_message="Invalid API key",
                        latency_ms=latency,
                        request_params=request_params,
                    )

                if self._is_rate_limited_response(resp):
                    return PlatformResponse(
                        platform=self.platform_name,
                        prompt=prompt,
                        response_text="",
                        error_code=ErrorCode.RATE_LIMITED,
                        error_message="Rate limit exceeded",
                        latency_ms=latency,
                        request_params=request_params,
                    )

                resp.raise_for_status()
                data = resp.json()

                try:
                    text = data["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError):
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

                # Extract usage/metadata
                usage = data.get("usage", {})
                citations, citations_parse_error = self._extract_citations_with_error(data)

                # Issue 2.2: Extract search metadata
                search_metadata, search_parse_error = self._extract_search_metadata_with_error(data)
                parse_errors = [error for error in (citations_parse_error, search_parse_error) if error]
                parse_error = " | ".join(parse_errors) if parse_errors else None

                return PlatformResponse(
                    platform=self.platform_name,
                    prompt=prompt,
                    response_text=text,
                    latency_ms=latency,
                    citations=citations,
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    response_model=data.get("model", self.model),
                    finish_reason=data.get("choices", [{}])[0].get("finish_reason", ""),
                    search_enabled=self.search_enabled,
                    # Issue 2.2: Raw response archiving
                    raw_response=data,
                    raw_response_text=text,
                    search_metadata=search_metadata,
                    parse_error=parse_error,
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
            except httpx.HTTPStatusError as e:
                return PlatformResponse(
                    platform=self.platform_name,
                    prompt=prompt,
                    response_text="",
                    error_code=ErrorCode.PLATFORM_DOWN,
                    error_message=str(e),
                    request_params=request_params,
                )
            except Exception as e:
                return PlatformResponse(
                    platform=self.platform_name,
                    prompt=prompt,
                    response_text="",
                    error_code=ErrorCode.UNKNOWN,
                    error_message=str(e),
                    request_params=request_params,
                )

    async def health_check(self) -> bool:
        try:
            client = await self._get_client()
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers=self._build_headers(),
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 5,
                },
            )
            return resp.status_code == 200
        except Exception:
            return False
