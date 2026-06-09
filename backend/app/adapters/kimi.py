"""Kimi platform adapter by Moonshot AI.

Uses the Moonshot API (OpenAI-compatible).
Supports web search via $web_search builtin_function tool_call.
Requires a multi-turn flow: first request triggers search, then
echo arguments back for the model to generate the final answer.
"""

import asyncio
import time

import httpx

from app.adapters.base import ErrorCode, PlatformResponse
from app.adapters.openai_compat import OpenAICompatAdapter
from app.config import settings

# Max rounds of tool_calls loop (safety guard against infinite loops)
_MAX_TOOL_ROUNDS = 5


class KimiAdapter(OpenAICompatAdapter):
    platform_name = "kimi"
    search_enabled = True

    _rate_limit_retries: int = 5  # more retries for strict RPM limit (20)

    def __init__(self):
        self.api_key = settings.kimi_api_key
        self.base_url = settings.kimi_base_url
        self.model = settings.kimi_model
        super().__init__()
        self.semaphore = asyncio.Semaphore(1)  # serialise; RPM limit is 20
        self._quota_exhausted = False
        self._quota_exhausted_message: str | None = None

    def _build_request_body(self, prompt: str, messages: list | None = None) -> dict:
        body = {
            "model": self.model,
            "messages": messages or [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "tools": [
                {
                    "type": "builtin_function",
                    "function": {"name": "$web_search"},
                }
            ],
        }
        return body

    def _extract_error_message(self, resp: httpx.Response) -> str:
        """Extract a human-readable error from an API response."""
        try:
            data = resp.json()
        except Exception:
            return resp.text[:300] if getattr(resp, "text", "") else ""

        if isinstance(data, dict):
            error = data.get("error", {})
            if isinstance(error, dict):
                message = error.get("message")
                if isinstance(message, str) and message.strip():
                    return message.strip()
            message = data.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        return ""

    def _is_quota_exhausted(self, resp: httpx.Response) -> bool:
        """Detect persistent 429s caused by billing/quota state, not burst limits."""
        try:
            data = resp.json()
        except Exception:
            return False

        if not isinstance(data, dict):
            return False

        error = data.get("error", {})
        if not isinstance(error, dict):
            return False

        message = str(error.get("message", "")).lower()
        error_type = str(error.get("type", "")).lower()
        error_code = str(error.get("code", "")).lower()

        return (
            "insufficient balance" in message
            or ("account" in message and "suspended" in message)
            or ("quota" in message and "suspended" in message)
            or "exceeded_current_quota_error" in error_type
            or "exceeded_current_quota_error" in error_code
        )

    async def _query_single(self, prompt: str) -> PlatformResponse:
        """Query Kimi with $web_search enabled.

        The $web_search builtin function requires a multi-turn flow:
        1. Send request with the $web_search tool declared
        2. Model returns finish_reason="tool_calls" with $web_search call
        3. Echo arguments back as a tool message (Kimi executes search internally)
        4. Model generates final answer with finish_reason="stop"
        """
        async with self.semaphore:
            start = time.monotonic()
            try:
                if self._quota_exhausted:
                    return PlatformResponse(
                        platform=self.platform_name,
                        prompt=prompt,
                        response_text="",
                        error_code=ErrorCode.RATE_LIMITED,
                        error_message=self._quota_exhausted_message
                        or "Kimi account quota exhausted",
                        latency_ms=int((time.monotonic() - start) * 1000),
                    )

                client = await self._get_client()
                messages = [{"role": "user", "content": prompt}]

                for _round in range(_MAX_TOOL_ROUNDS):
                    for attempt in range(self._rate_limit_retries + 1):
                        resp = await client.post(
                            f"{self.base_url}/chat/completions",
                            headers={"Authorization": f"Bearer {self.api_key}"},
                            json=self._build_request_body(prompt, messages),
                        )
                        if resp.status_code != 429:
                            break

                        if self._is_quota_exhausted(resp):
                            self._quota_exhausted = True
                            self._quota_exhausted_message = (
                                self._extract_error_message(resp)
                                or "Kimi account quota exhausted"
                            )
                            return PlatformResponse(
                                platform=self.platform_name,
                                prompt=prompt,
                                response_text="",
                                error_code=ErrorCode.RATE_LIMITED,
                                error_message=self._quota_exhausted_message,
                                latency_ms=int((time.monotonic() - start) * 1000),
                            )

                        if attempt == self._rate_limit_retries:
                            break
                        await asyncio.sleep(2 ** attempt)

                    if resp.status_code == 401:
                        return PlatformResponse(
                            platform=self.platform_name,
                            prompt=prompt,
                            response_text="",
                            error_code=ErrorCode.AUTH_FAILED,
                            error_message="Invalid API key",
                            latency_ms=int((time.monotonic() - start) * 1000),
                        )

                    if resp.status_code == 429:
                        return PlatformResponse(
                            platform=self.platform_name,
                            prompt=prompt,
                            response_text="",
                            error_code=ErrorCode.RATE_LIMITED,
                            error_message=self._extract_error_message(resp)
                            or "Rate limit exceeded",
                            latency_ms=int((time.monotonic() - start) * 1000),
                        )

                    if resp.status_code == 400:
                        # Capture actual error body for diagnosis
                        err_body = resp.text[:300]
                        return PlatformResponse(
                            platform=self.platform_name,
                            prompt=prompt,
                            response_text="",
                            error_code=ErrorCode.PLATFORM_DOWN,
                            error_message=f"400 Bad Request: {err_body}",
                            latency_ms=int((time.monotonic() - start) * 1000),
                        )
                    resp.raise_for_status()
                    data = resp.json()

                    try:
                        choice = data["choices"][0]
                    except (KeyError, IndexError, TypeError):
                        return PlatformResponse(
                            platform=self.platform_name,
                            prompt=prompt,
                            response_text="",
                            error_code=ErrorCode.FORMAT_ERROR,
                            error_message=f"Unexpected response format: {list(data.keys())}",
                            latency_ms=int((time.monotonic() - start) * 1000),
                        )

                    finish_reason = choice.get("finish_reason", "")

                    if finish_reason != "tool_calls":
                        # Final answer - extract text and return
                        text = choice["message"].get("content", "")
                        usage = data.get("usage", {})
                        latency = int((time.monotonic() - start) * 1000)
                        return PlatformResponse(
                            platform=self.platform_name,
                            prompt=prompt,
                            response_text=text,
                            latency_ms=latency,
                            citations=self._extract_citations(data),
                            prompt_tokens=usage.get("prompt_tokens", 0),
                            completion_tokens=usage.get("completion_tokens", 0),
                            response_model=data.get("model", self.model),
                            finish_reason=finish_reason,
                            search_enabled=True,
                        )

                    # Handle tool_calls - add assistant message to context
                    assistant_msg = choice["message"]
                    messages.append(assistant_msg)

                    for tool_call in assistant_msg.get("tool_calls", []):
                        tool_call_name = tool_call["function"]["name"]
                        tool_call_args = tool_call["function"]["arguments"]

                        if tool_call_name == "$web_search":
                            # Echo arguments back - Kimi executes search server-side
                            tool_result = tool_call_args
                        else:
                            tool_result = f"Error: unknown tool '{tool_call_name}'"

                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "name": tool_call_name,
                            "content": tool_result,
                        })

                # Safety: too many rounds
                return PlatformResponse(
                    platform=self.platform_name,
                    prompt=prompt,
                    response_text="",
                    error_code=ErrorCode.UNKNOWN,
                    error_message="Exceeded max tool_call rounds",
                    latency_ms=int((time.monotonic() - start) * 1000),
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
