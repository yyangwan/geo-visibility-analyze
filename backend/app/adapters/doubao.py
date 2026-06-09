"""Doubao (豆包) platform adapter by ByteDance.

Uses the Volcengine Ark API (OpenAI-compatible).
Model parameter uses endpoint ID (ep-xxxxx) or model name.

Web search is supported through the Responses API (/responses endpoint)
with the built-in web_search tool, NOT through Chat Completions API.
This adapter overrides _query_single to use the Responses API.
"""

import asyncio
import time

import httpx

from app.adapters.base import ErrorCode, PlatformResponse
from app.adapters.openai_compat import OpenAICompatAdapter
from app.config import settings


class DoubaoAdapter(OpenAICompatAdapter):
    platform_name = "doubao"
    search_enabled = True

    def __init__(self):
        self.api_key = settings.doubao_api_key
        self.base_url = settings.doubao_base_url
        self.model = settings.doubao_model
        super().__init__()

    def _build_responses_body(self, prompt: str) -> dict:
        """Build request body for the Responses API with web_search tool."""
        return {
            "model": self.model,
            "input": [
                {"role": "user", "content": prompt},
            ],
            "tools": [
                {"type": "web_search"},
            ],
        }

    def _parse_responses_output(self, data: dict) -> str:
        """Extract text from a Responses API response."""
        output_items = data.get("output", [])
        for item in output_items:
            if item.get("type") == "message":
                contents = item.get("content", [])
                for c in contents:
                    if c.get("type") == "output_text":
                        return c.get("text", "")
        return ""

    async def _query_single(self, prompt: str) -> PlatformResponse:
        """Query Doubao via Responses API with web_search enabled."""
        async with self.semaphore:
            start = time.monotonic()
            try:
                client = await self._get_client()

                for attempt in range(self._rate_limit_retries + 1):
                    resp = await client.post(
                        f"{self.base_url}/responses",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json=self._build_responses_body(prompt),
                    )
                    if resp.status_code != 429 or attempt == self._rate_limit_retries:
                        break
                    await asyncio.sleep(2 ** attempt)

                latency = int((time.monotonic() - start) * 1000)

                if resp.status_code == 401:
                    return PlatformResponse(
                        platform=self.platform_name, prompt=prompt,
                        response_text="", error_code=ErrorCode.AUTH_FAILED,
                        error_message="Invalid API key",
                        latency_ms=latency,
                    )

                if resp.status_code == 429:
                    return PlatformResponse(
                        platform=self.platform_name, prompt=prompt,
                        response_text="", error_code=ErrorCode.RATE_LIMITED,
                        error_message="Rate limit exceeded",
                        latency_ms=latency,
                    )

                resp.raise_for_status()
                data = resp.json()

                text = self._parse_responses_output(data)
                if not text:
                    return PlatformResponse(
                        platform=self.platform_name, prompt=prompt,
                        response_text="", error_code=ErrorCode.FORMAT_ERROR,
                        error_message=f"Unexpected Responses API format: {list(data.keys())}",
                        latency_ms=latency,
                    )

                usage = data.get("usage", {})
                return PlatformResponse(
                    platform=self.platform_name, prompt=prompt,
                    response_text=text, latency_ms=latency,
                    prompt_tokens=usage.get("input_tokens", 0),
                    completion_tokens=usage.get("output_tokens", 0),
                    response_model=data.get("model", self.model),
                    finish_reason="stop",
                    search_enabled=True,
                )

            except httpx.TimeoutException:
                return PlatformResponse(
                    platform=self.platform_name, prompt=prompt,
                    response_text="", error_code=ErrorCode.TIMEOUT,
                    error_message=f"Timeout after {self.timeout}s",
                )
            except httpx.HTTPStatusError as e:
                return PlatformResponse(
                    platform=self.platform_name, prompt=prompt,
                    response_text="", error_code=ErrorCode.PLATFORM_DOWN,
                    error_message=str(e),
                )
            except Exception as e:
                return PlatformResponse(
                    platform=self.platform_name, prompt=prompt,
                    response_text="", error_code=ErrorCode.UNKNOWN,
                    error_message=str(e),
                )
