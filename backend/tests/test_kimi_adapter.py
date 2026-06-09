"""Tests for Kimi adapter rate-limit handling."""

from unittest.mock import AsyncMock

import pytest

from app.adapters.kimi import KimiAdapter


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 429):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, response: _FakeResponse):
        self.response = response
        self.calls = 0

    async def post(self, *args, **kwargs):
        self.calls += 1
        return self.response


@pytest.mark.asyncio
async def test_kimi_quota_exhaustion_short_circuits_remaining_prompts(monkeypatch):
    adapter = KimiAdapter()
    payload = {
        "error": {
            "message": (
                "Your account org-123 <ak-abc> is suspended due to insufficient "
                "balance, please recharge your account or check your plan and billing details"
            ),
            "type": "exceeded_current_quota_error",
        }
    }
    client = _FakeClient(_FakeResponse(payload))

    monkeypatch.setattr(adapter, "_get_client", AsyncMock(return_value=client))
    sleep_mock = AsyncMock()
    monkeypatch.setattr("app.adapters.kimi.asyncio.sleep", sleep_mock)

    try:
        results = await adapter.query(
            ["通用产品有哪些类型？", "通用和专用产品有什么区别？", "通用产品怎么样？"]
        )
    finally:
        if getattr(adapter, "_client", None) is not None:
            await adapter._client.aclose()

    assert client.calls == 1
    assert sleep_mock.await_count == 0
    assert all(not result.success for result in results)
    assert all(result.error_code.value == "rate_limited" for result in results)
    assert "insufficient balance" in results[0].error_message.lower()
