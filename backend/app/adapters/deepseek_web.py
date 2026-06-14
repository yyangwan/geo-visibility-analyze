"""DeepSeek official web adapter.

This adapter targets the DeepSeek web SSE endpoint instead of the public
OpenAI-compatible API. It keeps the same `PlatformAdapter` contract so the
audit pipeline can switch between web capture and API compatibility without
changing downstream code.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from app.adapters.base import ErrorCode, PlatformAdapter, PlatformResponse
from app.config import settings
from app.logging_config import get_logger
from app.services.response_parser import (
    align_deepseek_web_citations,
    extract_deepseek_web_text,
    parse_deepseek_citation_markers,
    parse_deepseek_web_results,
)

logger = get_logger("deepseek_web")

_DEFAULT_ENDPOINT = "https://chat.deepseek.com/api/v0/chat/completion"


class DeepSeekWebAdapter(PlatformAdapter):
    """Capture DeepSeek web responses from the official SSE endpoint."""

    platform_name = "deepseek"
    search_enabled = True

    def __init__(self):
        super().__init__()
        self.model = settings.deepseek_model
        self.timeout = settings.query_timeout_seconds
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_per_platform)
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                trust_env=False,
            )
        return self._client

    def _resolve_web_config(self) -> dict[str, Any]:
        config = self.get_platform_config()
        web_config = config.get("web", {}) if isinstance(config, dict) else {}
        resolved = dict(web_config) if isinstance(web_config, dict) else {}

        # Allow env-driven defaults so a real browser session can be injected
        # without editing platform rows.
        if not resolved.get("auth_token") and settings.deepseek_web_auth_token:
            resolved["auth_token"] = settings.deepseek_web_auth_token
        if not resolved.get("cookie") and settings.deepseek_web_cookie:
            resolved["cookie"] = settings.deepseek_web_cookie
        if not resolved.get("chat_session_id") and settings.deepseek_web_chat_session_id:
            resolved["chat_session_id"] = settings.deepseek_web_chat_session_id
        if not resolved.get("parent_message_id") and settings.deepseek_web_parent_message_id:
            resolved["parent_message_id"] = settings.deepseek_web_parent_message_id
        if not resolved.get("model_type") and settings.deepseek_web_model_type:
            resolved["model_type"] = settings.deepseek_web_model_type

        parent_message_id = resolved.get("parent_message_id")
        if isinstance(parent_message_id, str):
            parent_message_id = parent_message_id.strip()
            if parent_message_id.isdigit():
                resolved["parent_message_id"] = int(parent_message_id)
            elif not parent_message_id:
                resolved["parent_message_id"] = None

        headers = dict(resolved.get("headers", {})) if isinstance(resolved.get("headers"), dict) else {}
        if settings.deepseek_web_user_agent and "User-Agent" not in headers:
            headers["User-Agent"] = settings.deepseek_web_user_agent

        extra_headers_json = settings.deepseek_web_headers_json.strip()
        if extra_headers_json:
            try:
                extra_headers = json.loads(extra_headers_json)
            except json.JSONDecodeError:
                logger.warning("deepseek_web_extra_headers_json_invalid")
            else:
                if isinstance(extra_headers, dict):
                    headers.update({str(k): str(v) for k, v in extra_headers.items() if v is not None})

        resolved["headers"] = headers

        return resolved

    def _build_headers(self) -> dict[str, str]:
        web_config = self._resolve_web_config()
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": "https://chat.deepseek.com",
            "Referer": "https://chat.deepseek.com/",
        }
        custom_headers = web_config.get("headers", {})
        if isinstance(custom_headers, dict):
            headers.update({str(k): str(v) for k, v in custom_headers.items() if v is not None})

        auth_token = web_config.get("auth_token") or web_config.get("token")
        if auth_token and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {auth_token}"

        cookie = web_config.get("cookie")
        if cookie and "Cookie" not in headers:
            headers["Cookie"] = str(cookie)

        headers.update(self.build_trace_headers())

        return headers

    def _build_request_body(self, prompt: str) -> dict[str, Any]:
        web_config = self._resolve_web_config()
        body: dict[str, Any] = {
            "prompt": prompt,
            "chat_session_id": web_config.get("chat_session_id"),
            "parent_message_id": web_config.get("parent_message_id"),
            "model_type": web_config.get("model_type"),
            "ref_file_ids": web_config.get("ref_file_ids", []),
            "thinking_enabled": web_config.get("thinking_enabled", False),
            "search_enabled": web_config.get("search_enabled", True),
            "action": web_config.get("action"),
            "preempt": web_config.get("preempt", False),
        }

        # Some deployments may still expect a model hint or older envelope.
        # Keep those as optional config-driven overrides rather than defaults.
        model = web_config.get("model")
        if model is not None:
            body["model"] = model

        # Allow config-driven overrides for the web payload when the upstream
        # endpoint expects a slightly different envelope.
        request_body = web_config.get("request_body", {})
        if isinstance(request_body, dict):
            body.update(request_body)

        required_nullable_fields = {
            "chat_session_id",
            "parent_message_id",
            "model_type",
            "action",
        }
        return {
            key: value
            for key, value in body.items()
            if value is not None or key in required_nullable_fields
        }

    def _build_endpoint(self) -> str:
        web_config = self._resolve_web_config()
        endpoint = web_config.get("endpoint")
        if isinstance(endpoint, str) and endpoint.strip():
            return endpoint.strip()
        return _DEFAULT_ENDPOINT

    def _extract_event_payload(self, lines: list[str]) -> dict[str, Any] | None:
        data = "\n".join(line for line in lines if line)
        if not data:
            return None
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            logger.debug("deepseek_web_event_parse_failed", data=data[:200])
            return None

    def _is_final_payload(self, payload: dict[str, Any]) -> bool:
        response = payload.get("response", {}) if isinstance(payload, dict) else {}
        if not isinstance(response, dict):
            return False
        status = response.get("status") or payload.get("status")
        if isinstance(status, str) and status.upper() == "FINISHED":
            return True
        fragments = response.get("fragments")
        if isinstance(fragments, list) and fragments:
            last_fragment = fragments[-1]
            if isinstance(last_fragment, dict):
                fragment_status = last_fragment.get("status")
                if isinstance(fragment_status, str) and fragment_status.upper() == "FINISHED":
                    return True
        return False

    async def _consume_stream(self, resp: httpx.Response) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        events: list[dict[str, Any]] = []
        current_data: list[str] = []
        final_payload: dict[str, Any] | None = None

        async for line in resp.aiter_lines():
            if line.startswith(":"):
                continue

            if not line:
                payload = self._extract_event_payload(current_data)
                current_data = []
                if payload is None:
                    continue
                events.append(payload)
                if self._is_final_payload(payload):
                    final_payload = payload
                continue

            if line.startswith("data:"):
                current_data.append(line[5:].lstrip())
            elif line.startswith("event:"):
                continue
            else:
                current_data.append(line)

        if current_data:
            payload = self._extract_event_payload(current_data)
            if payload is not None:
                events.append(payload)
                if self._is_final_payload(payload):
                    final_payload = payload

        if final_payload is None and events:
            final_payload = events[-1]

        return events, final_payload

    async def _query_single(self, prompt: str) -> PlatformResponse:
        async with self.semaphore:
            start = time.monotonic()
            request_params = self._build_request_body(prompt)
            endpoint = self._build_endpoint()
            headers = self._build_headers()

            try:
                client = await self._get_client()

                async with client.stream(
                    "POST",
                    endpoint,
                    headers=headers,
                    json=request_params,
                ) as resp:
                    latency = int((time.monotonic() - start) * 1000)

                    if resp.status_code == 401:
                        return PlatformResponse(
                            platform=self.platform_name,
                            prompt=prompt,
                            response_text="",
                            error_code=ErrorCode.AUTH_FAILED,
                            error_message="Invalid DeepSeek web session",
                            latency_ms=latency,
                            request_params=request_params,
                        )

                    if resp.status_code == 429:
                        return PlatformResponse(
                            platform=self.platform_name,
                            prompt=prompt,
                            response_text="",
                            error_code=ErrorCode.RATE_LIMITED,
                            error_message="DeepSeek web rate limit exceeded",
                            latency_ms=latency,
                            request_params=request_params,
                        )

                    if resp.status_code >= 400:
                        body = await resp.aread()
                        body_text = body.decode("utf-8", errors="ignore")[:300]
                        return PlatformResponse(
                            platform=self.platform_name,
                            prompt=prompt,
                            response_text="",
                            error_code=ErrorCode.PLATFORM_DOWN,
                            error_message=f"HTTP {resp.status_code}: {body_text}",
                            latency_ms=latency,
                            request_params=request_params,
                        )

                    content_type = resp.headers.get("content-type", "")
                    if "text/event-stream" in content_type:
                        events, final_payload = await self._consume_stream(resp)
                    else:
                        raw_data = await resp.aread()
                        try:
                            final_payload = json.loads(raw_data.decode("utf-8"))
                        except json.JSONDecodeError:
                            final_payload = {"raw_text": raw_data.decode("utf-8", errors="ignore")}
                        events = [final_payload] if isinstance(final_payload, dict) else []

                response_payload = final_payload or {}
                response_text = extract_deepseek_web_text(response_payload)
                citations = parse_deepseek_web_results(response_payload)
                citations = align_deepseek_web_citations(citations, response_text)
                markers = parse_deepseek_citation_markers(response_text)

                raw_response = {
                    "endpoint": endpoint,
                    "events": events,
                    "final_payload": response_payload,
                }

                search_metadata = {
                    "search_enabled": True,
                    "search_triggered": bool(citations),
                    "search_query": request_params.get("input") or prompt,
                    "search_reasoning": None,
                    "search_results_count": len(citations),
                    "citation_markers": markers,
                }

                response_block = response_payload.get("response", {}) if isinstance(response_payload, dict) else {}
                usage = response_payload.get("usage", {}) if isinstance(response_payload, dict) else {}
                if not usage and isinstance(response_block, dict):
                    usage = response_block.get("usage", {}) or {}
                response_model = (
                    response_payload.get("model")
                    if isinstance(response_payload, dict)
                    else None
                ) or (response_block.get("model") if isinstance(response_block, dict) else None) or self.model
                finish_reason = (
                    response_payload.get("finish_reason")
                    if isinstance(response_payload, dict)
                    else None
                ) or (response_block.get("status") if isinstance(response_block, dict) else "") or ""

                return PlatformResponse(
                    platform=self.platform_name,
                    prompt=prompt,
                    response_text=response_text,
                    latency_ms=int((time.monotonic() - start) * 1000),
                    citations=citations,
                    prompt_tokens=usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0) or usage.get("output_tokens", 0),
                    response_model=response_model,
                    finish_reason=finish_reason,
                    search_enabled=True,
                    raw_response=raw_response,
                    raw_response_text=response_text,
                    search_metadata=search_metadata,
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

    async def query(self, prompts: list[str]) -> list[PlatformResponse]:
        tasks = [self._query_single(prompt) for prompt in prompts]
        return await asyncio.gather(*tasks)

    async def health_check(self) -> bool:
        try:
            client = await self._get_client()
            async with client.stream(
                "POST",
                self._build_endpoint(),
                headers=self._build_headers(),
                json=self._build_request_body("ping"),
            ) as resp:
                return resp.status_code == 200
        except Exception:
            return False
