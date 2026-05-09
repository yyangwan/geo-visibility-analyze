"""Shared base for OpenAI-compatible platform adapters.

All domestic AI platforms (DeepSeek, Qwen, Doubao, Kimi, Wenxin, Hunyuan)
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


class OpenAICompatAdapter(PlatformAdapter):
    """Adapter for any platform that implements OpenAI-compatible chat/completions."""

    platform_name: str = ""
    base_url: str = ""
    api_key: str = ""
    model: str = ""

    def __init__(self):
        self.timeout = settings.query_timeout_seconds
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_per_platform)
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def query(self, prompts: list[str]) -> list[PlatformResponse]:
        tasks = [self._query_single(p) for p in prompts]
        return await asyncio.gather(*tasks)

    async def _query_single(self, prompt: str) -> PlatformResponse:
        async with self.semaphore:
            start = time.monotonic()
            try:
                client = await self._get_client()
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "model": self.model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.3,
                    },
                )

                latency = int((time.monotonic() - start) * 1000)

                if resp.status_code == 401:
                    return PlatformResponse(
                        platform=self.platform_name,
                        prompt=prompt,
                        response_text="",
                        error_code=ErrorCode.AUTH_FAILED,
                        error_message="Invalid API key",
                        latency_ms=latency,
                    )

                if resp.status_code == 429:
                    return PlatformResponse(
                        platform=self.platform_name,
                        prompt=prompt,
                        response_text="",
                        error_code=ErrorCode.RATE_LIMITED,
                        error_message="Rate limit exceeded",
                        latency_ms=latency,
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
                    )

                return PlatformResponse(
                    platform=self.platform_name,
                    prompt=prompt,
                    response_text=text,
                    latency_ms=latency,
                )

            except httpx.TimeoutException:
                return PlatformResponse(
                    platform=self.platform_name,
                    prompt=prompt,
                    response_text="",
                    error_code=ErrorCode.TIMEOUT,
                    error_message=f"Timeout after {self.timeout}s",
                )
            except httpx.HTTPStatusError as e:
                return PlatformResponse(
                    platform=self.platform_name,
                    prompt=prompt,
                    response_text="",
                    error_code=ErrorCode.PLATFORM_DOWN,
                    error_message=str(e),
                )
            except Exception as e:
                return PlatformResponse(
                    platform=self.platform_name,
                    prompt=prompt,
                    response_text="",
                    error_code=ErrorCode.UNKNOWN,
                    error_message=str(e),
                )

    async def health_check(self) -> bool:
        try:
            client = await self._get_client()
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 5,
                },
            )
            return resp.status_code == 200
        except Exception:
            return False
