"""Tests for DeepSeek web capture and adapter routing."""

import json
from unittest.mock import AsyncMock

import pytest

from app.adapters.base import PlatformResponse
from app.adapters.deepseek import DeepSeekAdapter
from app.adapters.deepseek_web import DeepSeekWebAdapter
from app.adapters.openai_compat import OpenAICompatAdapter
from app.services.response_parser import (
    align_deepseek_web_citations,
    extract_deepseek_web_text,
    parse_deepseek_citation_markers,
    parse_deepseek_web_results,
)


def test_parse_deepseek_web_results_extracts_rich_metadata():
    payload = {
        "response": {
            "fragments": [
                {
                    "index": 0,
                    "content": "Draft",
                },
                {
                    "index": -1,
                    "content": "Alpha is the best option. [citation:7]",
                    "results": [
                        {
                            "url": "https://example.com/article",
                            "title": "Example Article",
                            "snippet": "Short summary",
                            "site_name": "Example",
                            "site_icon": "https://example.com/icon.png",
                            "cite_index": 7,
                            "query_indexes": [0, 2],
                            "published_at": 1780848000,
                        }
                    ],
                },
            ]
        }
    }

    citations = parse_deepseek_web_results(payload)
    assert len(citations) == 1
    assert citations[0]["url"] == "https://example.com/article"
    assert citations[0]["domain"] == "example.com"
    assert citations[0]["cite_index"] == 7
    assert citations[0]["query_indexes"] == [0, 2]
    assert extract_deepseek_web_text(payload) == "Alpha is the best option. [citation:7]"


def test_align_deepseek_web_citations_uses_marker_order():
    citations = [
        {"url": "https://one.test", "title": "One", "domain": "one.test", "cite_index": 2},
        {"url": "https://two.test", "title": "Two", "domain": "two.test", "cite_index": 7},
    ]
    aligned = align_deepseek_web_citations(citations, "answer [citation:7] then [citation:2]")
    assert [cite["cite_index"] for cite in aligned] == [7, 2]
    assert parse_deepseek_citation_markers("x [citation:7] y [citation:2]") == [7, 2]


class _FakeStreamResponse:
    def __init__(self, lines: list[str]):
        self._lines = lines
        self.status_code = 200
        self.headers = {"content-type": "text/event-stream"}

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return "\n".join(self._lines).encode("utf-8")


class _FakeStreamContext:
    def __init__(self, response: _FakeStreamResponse):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeClient:
    def __init__(self, response: _FakeStreamResponse):
        self.response = response
        self.calls = []

    def stream(self, method, endpoint, headers=None, json=None):
        self.calls.append({
            "method": method,
            "endpoint": endpoint,
            "headers": headers,
            "json": json,
        })
        return _FakeStreamContext(self.response)


@pytest.mark.asyncio
async def test_deepseek_web_adapter_parses_stream_payload(monkeypatch):
    adapter = DeepSeekWebAdapter()
    adapter.set_platform_config(
        {
            "web": {
                "endpoint": "https://chat.deepseek.com/api/v0/chat/completion",
                "chat_session_id": "c515607d-e3ba-4586-9473-9fc32951d407",
                "parent_message_id": None,
                "model_type": "default",
                "thinking_enabled": False,
                "search_enabled": True,
                "ref_file_ids": [],
                "preempt": False,
            }
        }
    )

    payload = {
        "response": {
            "status": "FINISHED",
            "fragments": [
                {
                    "index": -1,
                    "content": "Alpha is the best option. [citation:7]",
                    "results": [
                        {
                            "url": "https://example.com/article",
                            "title": "Example Article",
                            "snippet": "Short summary",
                            "site_name": "Example",
                            "site_icon": "https://example.com/icon.png",
                            "cite_index": 7,
                            "query_indexes": [0, 2],
                            "published_at": 1780848000,
                        }
                    ],
                }
            ],
        },
        "usage": {"prompt_tokens": 11, "completion_tokens": 4},
        "model": "deepseek-web-test",
    }

    response = _FakeStreamResponse(
        [
            "event: message",
            f"data: {json.dumps(payload, ensure_ascii=False)}",
            "",
        ]
    )
    client = _FakeClient(response)
    monkeypatch.setattr(adapter, "_get_client", AsyncMock(return_value=client))

    result = await adapter._query_single("What is Alpha?")

    assert result.success is True
    assert result.response_text == "Alpha is the best option. [citation:7]"
    assert result.citations[0]["cite_index"] == 7
    assert result.search_metadata["search_results_count"] == 1
    assert result.search_metadata["citation_markers"] == [7]
    assert result.raw_response["events"]
    assert result.request_params["prompt"] == "What is Alpha?"
    assert result.request_params["chat_session_id"] == "c515607d-e3ba-4586-9473-9fc32951d407"
    assert "parent_message_id" in result.request_params
    assert result.request_params["parent_message_id"] == 2
    assert result.request_params["model_type"] == "default"
    assert result.request_params["thinking_enabled"] is False
    assert result.request_params["search_enabled"] is True
    assert result.request_params["ref_file_ids"] == []
    assert client.calls[0]["headers"]["Origin"] == "https://chat.deepseek.com"
    assert client.calls[0]["headers"]["Referer"] == "https://chat.deepseek.com/"
    assert client.calls[0]["headers"]["Accept-Language"] == "zh-CN,zh;q=0.9,en;q=0.8"
    assert client.calls[0]["method"] == "POST"


@pytest.mark.asyncio
async def test_deepseek_web_adapter_includes_trace_headers(monkeypatch):
    adapter = DeepSeekWebAdapter()
    adapter.set_runtime_context(
        {
            "analysis_run_id": "run-456",
            "audit_id": 7,
            "project_id": "project-xyz",
        }
    )
    adapter.set_platform_config(
        {
            "web": {
                "endpoint": "https://chat.deepseek.com/api/v0/chat/completion",
                "chat_session_id": "session-1",
                "parent_message_id": None,
                "model_type": "default",
            }
        }
    )

    payload = {
        "response": {
            "status": "FINISHED",
            "fragments": [{"index": -1, "content": "Answer", "results": []}],
        },
        "usage": {},
        "model": "deepseek-web-test",
    }
    response = _FakeStreamResponse(["data: {}".format(json.dumps(payload, ensure_ascii=False)), ""])
    client = _FakeClient(response)
    monkeypatch.setattr(adapter, "_get_client", AsyncMock(return_value=client))

    await adapter._query_single("What is Alpha?")

    assert client.calls[0]["headers"]["X-Analysis-Run-Id"] == "run-456"
    assert client.calls[0]["headers"]["X-Audit-Id"] == "7"
    assert client.calls[0]["headers"]["X-Project-Id"] == "project-xyz"
    assert client.calls[0]["json"]["prompt"] == "What is Alpha?"


@pytest.mark.asyncio
async def test_deepseek_web_adapter_uses_env_user_agent(monkeypatch):
    from app.adapters import deepseek_web as deepseek_web_module

    adapter = DeepSeekWebAdapter()
    monkeypatch.setattr(deepseek_web_module.settings, "deepseek_web_user_agent", "Mozilla/5.0 Test UA")
    monkeypatch.setattr(
        deepseek_web_module.settings,
        "deepseek_web_headers_json",
        "{\"x-app-version\":\"2.0.0\",\"x-client-platform\":\"web\"}",
    )

    headers = adapter._build_headers()

    assert headers["User-Agent"] == "Mozilla/5.0 Test UA"
    assert headers["x-app-version"] == "2.0.0"
    assert headers["x-client-platform"] == "web"


def test_deepseek_web_request_body_keeps_required_nullable_fields():
    adapter = DeepSeekWebAdapter()
    adapter.set_platform_config({"web": {"parent_message_id": "2"}})

    body = adapter._build_request_body("test prompt")

    assert "chat_session_id" in body
    assert "parent_message_id" in body
    assert "model_type" in body
    assert "action" in body
    assert body["parent_message_id"] == 2


@pytest.mark.asyncio
async def test_deepseek_adapter_switches_to_web_capture(monkeypatch):
    adapter = DeepSeekAdapter()
    adapter.set_platform_config({"capture_mode": "official_web"})

    web_results = [
        PlatformResponse(
            platform="deepseek",
            prompt="What is Alpha?",
            response_text="Alpha is the best option.",
        )
    ]
    web_query = AsyncMock(return_value=web_results)
    monkeypatch.setattr(adapter._web_adapter, "query", web_query)

    api_query = AsyncMock(return_value=[])
    monkeypatch.setattr(OpenAICompatAdapter, "query", api_query)

    result = await adapter.query(["What is Alpha?"])

    assert result == web_results
    web_query.assert_awaited_once()
    api_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_deepseek_adapter_falls_back_to_api_mode(monkeypatch):
    adapter = DeepSeekAdapter()
    adapter.set_platform_config({"capture_mode": "api_compat"})

    api_results = [
        PlatformResponse(
            platform="deepseek",
            prompt="What is Alpha?",
            response_text="Alpha is the best option.",
        )
    ]
    api_query = AsyncMock(return_value=api_results)
    monkeypatch.setattr(OpenAICompatAdapter, "query", api_query)

    web_query = AsyncMock(return_value=[])
    monkeypatch.setattr(adapter._web_adapter, "query", web_query)

    result = await adapter.query(["What is Alpha?"])

    assert result == api_results
    api_query.assert_awaited_once()
    web_query.assert_not_awaited()
