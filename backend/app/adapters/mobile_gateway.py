"""Mobile-app capture adapter backed by the outbound Android gateway."""

import asyncio
import hashlib
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

from app.adapters.base import ErrorCode, PlatformAdapter, PlatformResponse
from app.api.device_gateway_schemas import DeviceTaskCreate
from app.config import settings
from app.database import async_session
from app.models.device_gateway import DeviceGateway, DeviceTask
from app.services.device_gateway_service import create_task


_GATEWAY_PLATFORM_NAMES = {
    "hunyuan": "yuanbao",
}


def mobile_capture_enabled(platform: str, config: dict | None = None) -> bool:
    """Return whether audit collection for this platform uses the mobile app."""
    if not settings.mobile_app_capture_enabled:
        return False
    enabled_platforms = {
        item.strip()
        for item in settings.mobile_app_capture_platforms.split(",")
        if item.strip()
    }
    if platform not in enabled_platforms:
        return False
    mobile_config = (config or {}).get("mobile_gateway", {})
    return not isinstance(mobile_config, dict) or mobile_config.get("enabled") is not False


class MobileGatewayAdapter(PlatformAdapter):
    """Normalize Android app collection results into ``PlatformResponse``."""

    def __init__(self, platform_name: str):
        super().__init__()
        self.platform_name = platform_name

    async def query(self, prompts: list[str]) -> list[PlatformResponse]:
        # A gateway owns one physical device, so preserve prompt order and avoid
        # filling the queue with an entire platform batch at once.
        responses = []
        for prompt in prompts:
            responses.append(await self._query_single(prompt))
        return responses

    async def health_check(self) -> bool:
        gateway_id = self._target_gateway_id()
        if not gateway_id:
            return False
        async with async_session() as db:
            gateway = await db.get(DeviceGateway, gateway_id)
            if gateway is None or gateway.status not in {"online", "degraded"}:
                return False
            if gateway.last_seen_at is None:
                return False
            return gateway.last_seen_at >= datetime.utcnow() - timedelta(seconds=90)

    async def _query_single(self, prompt: str) -> PlatformResponse:
        started = time.monotonic()
        try:
            task = await self._enqueue_task(prompt)
            completed = await self._wait_for_task(task.id)
        except TimeoutError as exc:
            return self._error_response(
                prompt,
                ErrorCode.TIMEOUT,
                str(exc),
                started,
            )
        except Exception as exc:
            return self._error_response(
                prompt,
                ErrorCode.UNKNOWN,
                str(exc),
                started,
            )

        latency_ms = int((time.monotonic() - started) * 1000)
        if completed.status != "completed":
            return PlatformResponse(
                platform=self.platform_name,
                prompt=prompt,
                response_text="",
                error_code=self._map_error(completed.error_message),
                error_message=completed.error_message or completed.error_code or "Mobile capture failed",
                latency_ms=latency_ms,
                raw_response={
                    "task_id": completed.id,
                    "status": completed.status,
                    "error_code": completed.error_code,
                    "error_message": completed.error_message,
                    "attempt_count": completed.attempt_count,
                },
                request_params=self._request_metadata(prompt, completed.id),
            )

        result = completed.result
        if not isinstance(result, dict) or not isinstance(result.get("answer"), str):
            return PlatformResponse(
                platform=self.platform_name,
                prompt=prompt,
                response_text="",
                error_code=ErrorCode.FORMAT_ERROR,
                error_message="Mobile gateway returned an invalid result",
                latency_ms=latency_ms,
                raw_response=result if isinstance(result, dict) else {"result": result},
                request_params=self._request_metadata(prompt, completed.id),
            )

        citations = self._normalize_citations(result)
        app_version = str(result.get("app_version") or "")
        return PlatformResponse(
            platform=self.platform_name,
            prompt=prompt,
            response_text=result["answer"],
            latency_ms=int(result.get("duration_ms") or latency_ms),
            citations=citations,
            response_model=f"app:{app_version}" if app_version else "app",
            finish_reason="stop",
            search_enabled=bool(
                result.get("reference_count")
                or result.get("source_count")
                or citations
            ),
            raw_response=result,
            raw_response_text=result["answer"],
            search_metadata={
                "capture_mode": "mobile_app",
                "surface": result.get("surface", "app"),
                "package_name": result.get("package_name"),
                "app_version": result.get("app_version"),
                "device_serial": result.get("device_serial"),
                "reference_count": result.get("reference_count", 0),
                "source_count": result.get("source_count", 0),
                "sources": result.get("sources", []),
                "answer_urls": result.get("answer_urls", []),
                "source_collection_duration_ms": result.get(
                    "source_collection_duration_ms"
                ),
            },
            request_params=self._request_metadata(prompt, completed.id),
        )

    async def _enqueue_task(self, prompt: str) -> DeviceTask:
        runtime = self.get_runtime_context()
        project_id = str(runtime.get("project_id") or "mobile-capture")[:50]
        analysis_run_id = str(runtime.get("analysis_run_id") or "standalone")
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:20]
        idempotency_key = (
            f"mobile:{analysis_run_id}:{self.platform_name}:{prompt_hash}"
        )[:100]
        payload = {
            "prompt": prompt,
            "timeout_seconds": settings.mobile_app_capture_task_timeout_seconds,
            "new_conversation": True,
        }
        if settings.mobile_app_capture_device_serial:
            payload["device_serial"] = settings.mobile_app_capture_device_serial

        data = DeviceTaskCreate(
            project_id=project_id,
            task_type="appium.prompt",
            target_gateway_id=self._target_gateway_id(),
            platform=_GATEWAY_PLATFORM_NAMES.get(
                self.platform_name,
                self.platform_name,
            ),
            surface="app",
            payload=payload,
            priority=settings.mobile_app_capture_priority,
            max_attempts=settings.mobile_app_capture_max_attempts,
            idempotency_key=idempotency_key,
        )
        async with async_session() as db:
            return await create_task(db, data)

    async def _wait_for_task(self, task_id: str) -> DeviceTask:
        deadline = time.monotonic() + settings.mobile_app_capture_wait_timeout_seconds
        while time.monotonic() < deadline:
            async with async_session() as db:
                task = await db.get(DeviceTask, task_id)
                if task is None:
                    raise RuntimeError(f"Mobile gateway task disappeared: {task_id}")
                if task.status in {"completed", "failed"}:
                    return task
            await asyncio.sleep(settings.mobile_app_capture_poll_interval_seconds)
        raise TimeoutError(
            f"Timed out waiting for mobile gateway task {task_id} after "
            f"{settings.mobile_app_capture_wait_timeout_seconds}s"
        )

    def _target_gateway_id(self) -> str | None:
        if settings.mobile_app_capture_gateway_id:
            return settings.mobile_app_capture_gateway_id
        gateway_ids = [
            item.strip()
            for item in settings.device_gateway_ids.split(",")
            if item.strip()
        ]
        return gateway_ids[0] if gateway_ids else None

    def _request_metadata(self, prompt: str, task_id: str) -> dict:
        return {
            "capture_mode": "mobile_app",
            "task_id": task_id,
            "gateway_id": self._target_gateway_id(),
            "platform": _GATEWAY_PLATFORM_NAMES.get(
                self.platform_name,
                self.platform_name,
            ),
            "surface": "app",
            "prompt": prompt,
            "timeout_seconds": settings.mobile_app_capture_task_timeout_seconds,
        }

    def _normalize_citations(self, result: dict) -> list[dict]:
        citations = []
        for position, source in enumerate(result.get("sources") or [], start=1):
            if not isinstance(source, dict):
                continue
            url = str(source.get("url") or "").strip()
            domain = str(source.get("domain") or "").strip().lower()
            if not domain and url:
                domain = urlparse(url).hostname or ""
            citations.append(
                {
                    "url": url,
                    "title": str(
                        source.get("title")
                        or source.get("page_title")
                        or source.get("site_name")
                        or ""
                    ),
                    "domain": domain,
                    "site_name": str(source.get("site_name") or ""),
                    "index": source.get("index", position),
                    "url_resolution": source.get("url_resolution", "unavailable"),
                    "status": source.get("status", "collected"),
                    "error_message": source.get("error_message"),
                    "provider": "mobile_gateway",
                    "citation_mode": "mobile_app_reference",
                }
            )
        return citations

    def _error_response(
        self,
        prompt: str,
        code: ErrorCode,
        message: str,
        started: float,
    ) -> PlatformResponse:
        return PlatformResponse(
            platform=self.platform_name,
            prompt=prompt,
            response_text="",
            error_code=code,
            error_message=message,
            latency_ms=int((time.monotonic() - started) * 1000),
            request_params=self._request_metadata(prompt, ""),
        )

    @staticmethod
    def _map_error(message: str | None) -> ErrorCode:
        normalized = (message or "").lower()
        if "timeout" in normalized or "timed out" in normalized:
            return ErrorCode.TIMEOUT
        if (
            "peak demand" in normalized
            or "rate limit" in normalized
            or "算力不足" in normalized
        ):
            return ErrorCode.RATE_LIMITED
        if (
            "handler not installed" in normalized
            or "temporarily unavailable" in normalized
            or "platform down" in normalized
        ):
            return ErrorCode.PLATFORM_DOWN
        return ErrorCode.UNKNOWN
