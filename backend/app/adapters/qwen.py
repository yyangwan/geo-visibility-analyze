"""Qwen (Tongyi Qianwen) platform adapter.

Uses the OpenAI-compatible API provided by Alibaba Cloud DashScope.
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


class QwenAdapter(PlatformAdapter):
    platform_name = "qwen"

    def __init__(self):
        self.base_url = settings.qwen_base_url
        self.api_key = settings.qwen_api_key
        self.model = settings.qwen_model
        self.timeout = settings.query_timeout_seconds
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_per_platform)

    async def query(self, prompts: list[str]) -> list[PlatformResponse]:
        tasks = [self._query_single(p) for p in prompts]
        return await asyncio.gather(*tasks)

    async def _query_single(self, prompt: str) -> PlatformResponse:
        async with self.semaphore:
            start = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
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
                text = data["choices"][0]["message"]["content"]

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
            async with httpx.AsyncClient(timeout=10) as client:
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
