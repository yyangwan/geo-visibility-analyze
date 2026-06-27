"""Tests for native Qwen sources and Hunyuan enhancement parameters."""

from unittest.mock import AsyncMock

import pytest

from app.adapters.hunyuan import HunyuanAdapter
from app.adapters.qwen import QwenAdapter


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    def __init__(self, response: _FakeResponse):
        self.response = response
        self.calls = []

    async def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers or {}, "json": json or {}})
        return self.response


@pytest.mark.asyncio
async def test_qwen_native_adapter_extracts_search_info_sources(monkeypatch):
    adapter = QwenAdapter()
    payload = {
        "output": {
            "choices": [
                {
                    "message": {"content": "建议选择细口壶。[1]"},
                    "finish_reason": "stop",
                }
            ],
            "search_info": {
                "search_results": [
                    {
                        "index": 1,
                        "title": "手冲壶选购指南",
                        "site_name": "Example",
                        "url": "https://www.example.com/kettle",
                    }
                ]
            },
        },
        "usage": {"input_tokens": 10, "output_tokens": 20},
        "request_id": "req-1",
    }
    client = _FakeClient(_FakeResponse(payload))
    monkeypatch.setattr(adapter, "_get_client", AsyncMock(return_value=client))

    result = await adapter._query_single_native("手冲咖啡壶怎么选")

    assert result.success is True
    assert result.response_text == "建议选择细口壶。[1]"
    assert result.citations == [
        {
            "url": "https://www.example.com/kettle",
            "title": "手冲壶选购指南",
            "domain": "example.com",
            "site_name": "Example",
            "cite_index": 1,
            "provider": "qwen_native",
            "citation_mode": "dashscope_search_info",
        }
    ]
    assert result.search_metadata["search_triggered"] is True
    assert result.search_metadata["search_results_count"] == 1
    assert result.search_metadata["search_provider"] == "qwen_native"
    assert result.search_metadata["citation_mode"] == "dashscope_search_info"
    assert result.citations[0]["provider"] == "qwen_native"
    assert result.citations[0]["citation_mode"] == "dashscope_search_info"
    body = client.calls[0]["json"]
    assert body["parameters"]["enable_search"] is True
    assert body["parameters"]["search_options"]["forced_search"] is True
    assert body["parameters"]["search_options"]["enable_source"] is True
    assert body["parameters"]["search_options"]["enable_citation"] is True


def test_hunyuan_uses_native_enhancement_parameter():
    adapter = HunyuanAdapter()
    adapter.set_platform_config(
        {
            "request": {
                "model": "hunyuan-turbos-latest",
                "temperature": 0.3,
            }
        }
    )

    body = adapter._build_request_body("手冲咖啡壶怎么选")

    assert body["model"] == "hunyuan-turbos-latest"
    assert body["enable_enhancement"] is True
    assert "enable_search" not in body
    assert "search_options" not in body


def test_hunyuan_detects_400_rate_limit_error():
    adapter = HunyuanAdapter()
    response = _FakeResponse(
        {
            "error": {
                "message": "请求频繁，请稍后再试",
                "type": "rate_limit_error",
                "code": "rate_limit_exceeded",
            }
        },
        status_code=400,
    )

    assert adapter._is_rate_limited_response(response) is True
