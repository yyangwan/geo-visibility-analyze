"""Kimi platform adapter by Moonshot AI.

Uses the Moonshot API (OpenAI-compatible).
Supports web search via $web_search builtin_function tool_call.
Requires a multi-turn flow: first request triggers search, then
echo arguments back for the model to generate the final answer.

Issue 2.3: Capture intermediate tool calls, extract search query,
and build complete raw_response with full conversation history.
"""

import asyncio
import json
import time

import httpx

from app.adapters.base import ErrorCode, PlatformResponse
from app.adapters.openai_compat import OpenAICompatAdapter
from app.config import settings

# Max rounds of tool_calls loop (safety guard against infinite loops)
_MAX_TOOL_ROUNDS = 5
_DEFAULT_SYSTEM_PROMPT = (
    "你是 Kimi，一个由月之暗面（Moonshot AI）开发的人工智能助手。"
    "你擅长中英文对话，能够基于上下文提供有帮助、无害且诚实的回答。"
    "当需要获取实时信息时，你会主动调用搜索工具。"
    "回答时先给出结论，再给出关键依据。"
    "如使用搜索，请在回答末尾列出引用来源名称和 URL。"
)


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
        config = self.get_platform_config()
        request_config = config.get("request", {}) if isinstance(config, dict) else {}
        search_config = config.get("search", {}) if isinstance(config, dict) else {}
        model = request_config.get("model") or config.get("model") or self.model
        temperature = request_config.get("temperature", 0.6)
        top_p = request_config.get("top_p", 0.95)
        max_tokens = request_config.get("max_tokens", 8192)

        body = {
            "model": model,
            "messages": messages or self._build_initial_messages(prompt),
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
            # Kimi docs require thinking to be disabled when using $web_search.
            "thinking": {"type": "disabled"},
        }

        if search_config.get("enable_search", True):
            body["tool_choice"] = search_config.get("tool_choice", "auto")
            body["tools"] = [
                {
                    "type": "builtin_function",
                    "function": {"name": "$web_search"},
                }
            ]
        return body

    def _build_initial_messages(self, prompt: str) -> list[dict]:
        config = self.get_platform_config()
        request_config = config.get("request", {}) if isinstance(config, dict) else {}
        system_prompt = request_config.get("system_prompt") or _DEFAULT_SYSTEM_PROMPT
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

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

    def _extract_search_query_from_tool_call(self, tool_call: dict) -> str | None:
        """Extract search query from $web_search tool_call arguments.

        Args:
            tool_call: Tool call dict from Kimi API response

        Returns:
            Search query string or None if not found
        """
        if not isinstance(tool_call, dict):
            return None

        function = tool_call.get("function", {})
        if function.get("name") != "$web_search":
            return None

        arguments = function.get("arguments", "")
        if isinstance(arguments, str):
            try:
                args_dict = json.loads(arguments)
                return args_dict.get("query")
            except json.JSONDecodeError:
                return None
        elif isinstance(arguments, dict):
            return arguments.get("query")

        return None

    def _extract_web_search_metadata_from_tool_call(self, tool_call: dict) -> dict:
        """Extract observable metadata from Kimi $web_search tool calls.

        Kimi's builtin search usually returns a search_id instead of exposing
        structured result URLs. Treat the presence of this tool call as the
        reliable search-trigger signal.
        """
        if not isinstance(tool_call, dict):
            return {}

        function = tool_call.get("function", {})
        if function.get("name") != "$web_search":
            return {}

        arguments = function.get("arguments", "")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return {}

        if not isinstance(arguments, dict):
            return {}

        search_result = arguments.get("search_result")
        usage = arguments.get("usage")
        metadata = {
            "search_triggered": True,
        }

        if isinstance(search_result, dict):
            search_id = search_result.get("search_id")
            if search_id:
                metadata["search_id"] = search_id

        if isinstance(usage, dict):
            total_tokens = usage.get("total_tokens")
            if total_tokens is not None:
                metadata["search_total_tokens"] = total_tokens

        query = arguments.get("query")
        if query:
            metadata["search_query"] = query

        return metadata

    async def _query_single(self, prompt: str) -> PlatformResponse:
        """Query Kimi with $web_search enabled.

        The $web_search builtin function requires a multi-turn flow:
        1. Send request with the $web_search tool declared
        2. Model returns finish_reason="tool_calls" with $web_search call
        3. Echo arguments back as a tool message (Kimi executes search internally)
        4. Model generates final answer with finish_reason="stop"

        Issue 2.3: Capture intermediate tool calls, extract search query,
        and build complete raw_response with full conversation history.
        """
        async with self.semaphore:
            start = time.monotonic()
            request_params = self._build_request_body(prompt)

            # Issue 2.3: Collect all API responses for complete raw_response
            all_responses: list[dict] = []
            search_query: str | None = None
            web_search_metadata: dict = {}
            final_usage: dict = {}

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
                        request_params=request_params,
                    )

                client = await self._get_client()
                messages = self._build_initial_messages(prompt)

                for _round in range(_MAX_TOOL_ROUNDS):
                    # Build request body with current messages
                    current_request = {
                        **request_params,
                        "messages": messages,
                    }

                    for attempt in range(self._rate_limit_retries + 1):
                        resp = await client.post(
                            f"{self.base_url}/chat/completions",
                            headers={"Authorization": f"Bearer {self.api_key}"},
                            json=current_request,
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
                                request_params=request_params,
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
                            request_params=request_params,
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
                            request_params=request_params,
                        )

                    if resp.status_code == 400:
                        err_body = resp.text[:300]
                        return PlatformResponse(
                            platform=self.platform_name,
                            prompt=prompt,
                            response_text="",
                            error_code=ErrorCode.PLATFORM_DOWN,
                            error_message=f"400 Bad Request: {err_body}",
                            latency_ms=int((time.monotonic() - start) * 1000),
                            request_params=request_params,
                        )
                    resp.raise_for_status()
                    data = resp.json()

                    # Issue 2.3: Collect this response for complete raw_response
                    all_responses.append(data)

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
                            raw_response={"responses": all_responses},
                            request_params=request_params,
                        )

                    finish_reason = choice.get("finish_reason", "")

                    if finish_reason != "tool_calls":
                        # Final answer - extract text and build complete response
                        text = choice["message"].get("content", "")
                        usage = data.get("usage", {})
                        final_usage = usage
                        latency = int((time.monotonic() - start) * 1000)

                        # Issue 2.3: Build complete raw_response with all conversation rounds
                        raw_response = {
                            "conversation": messages.copy(),
                            "responses": all_responses,
                            "final_response": data,
                        }

                        citations, citations_parse_error = self._extract_citations_with_error(data)

                        # Issue 2.3: Build search_metadata with extracted search query
                        search_metadata = {
                            "search_enabled": True,
                            "search_triggered": bool(web_search_metadata.get("search_triggered")),
                            "search_query": search_query,
                            "search_reasoning": None,
                            "search_results_count": 0,
                            "tool_call_rounds": len(all_responses) - 1,  # Exclude final response
                        }
                        search_metadata.update(web_search_metadata)

                        return PlatformResponse(
                            platform=self.platform_name,
                            prompt=prompt,
                            response_text=text,
                            latency_ms=latency,
                            citations=citations,
                            prompt_tokens=usage.get("prompt_tokens", 0),
                            completion_tokens=usage.get("completion_tokens", 0),
                            response_model=data.get("model", self.model),
                            finish_reason=finish_reason,
                            search_enabled=True,
                            # Issue 2.3: Complete raw response and search metadata
                            raw_response=raw_response,
                            raw_response_text=text,
                            search_metadata=search_metadata,
                            parse_error=citations_parse_error,
                            request_params=request_params,
                        )

                    # Handle tool_calls - extract search query and continue
                    assistant_msg = choice["message"]
                    messages.append(assistant_msg)

                    # Issue 2.3: Extract search query from $web_search tool call
                    for tool_call in assistant_msg.get("tool_calls", []):
                        tool_call_name = tool_call["function"]["name"]

                        if tool_call_name == "$web_search":
                            # Extract search query for metadata
                            if search_query is None:
                                search_query = self._extract_search_query_from_tool_call(tool_call)
                            web_search_metadata.update(
                                self._extract_web_search_metadata_from_tool_call(tool_call)
                            )

                        # Build tool response message
                        tool_call_args = tool_call["function"]["arguments"]
                        if tool_call_name == "$web_search":
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
                    raw_response={"responses": all_responses},
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
