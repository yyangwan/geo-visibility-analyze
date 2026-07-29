"""Tests for adapter auth header handling when API keys are missing."""

from unittest.mock import AsyncMock

import pytest

from app.adapters.deepseek import DeepSeekAdapter
from app.adapters.doubao import DoubaoAdapter
from app.adapters.kimi import KimiAdapter


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 401):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _FakeClient:
    def __init__(self, response: _FakeResponse):
        self.response = response
        self.calls: list[dict] = []

    async def post(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return self.response


def test_openai_compat_build_headers_omits_empty_authorization():
    adapter = DeepSeekAdapter()
    adapter.api_key = ""

    headers = adapter._build_headers()

    assert "Authorization" not in headers


@pytest.mark.asyncio
async def test_kimi_omits_empty_authorization_header(monkeypatch):
    adapter = KimiAdapter()
    adapter.api_key = ""
    client = _FakeClient(_FakeResponse({"error": {"message": "missing key"}}, status_code=401))
    monkeypatch.setattr(adapter, "_get_client", AsyncMock(return_value=client))

    result = await adapter._query_single("test prompt")

    assert client.calls[0]["kwargs"]["headers"].get("Authorization") is None
    assert result.error_code is not None
    assert result.error_code.value == "auth_failed"


@pytest.mark.asyncio
async def test_doubao_omits_empty_authorization_header(monkeypatch):
    adapter = DoubaoAdapter()
    adapter.api_key = ""
    client = _FakeClient(_FakeResponse({"error": {"message": "missing key"}}, status_code=401))
    monkeypatch.setattr(adapter, "_get_client", AsyncMock(return_value=client))

    result = await adapter._query_single("test prompt")

    assert client.calls[0]["kwargs"]["headers"].get("Authorization") is None
    assert result.error_code is not None
    assert result.error_code.value == "auth_failed"
