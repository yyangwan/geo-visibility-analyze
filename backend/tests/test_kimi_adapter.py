"""Tests for Kimi adapter rate-limit handling and Issue 2.3 multi-round search."""

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

    def raise_for_status(self):
        return None


class _FakeClient:
    def __init__(self, response: _FakeResponse):
        self.response = response
        self.calls = 0

    async def post(self, *args, **kwargs):
        self.calls += 1
        return self.response


class _SequencedClient:
    def __init__(self, responses: list[_FakeResponse]):
        self.responses = responses
        self.calls = 0

    async def post(self, *args, **kwargs):
        response = self.responses[self.calls]
        self.calls += 1
        return response


@pytest.mark.asyncio
async def test_kimi_includes_trace_headers(monkeypatch):
    adapter = KimiAdapter()
    adapter.set_runtime_context(
        {
            "analysis_run_id": "run-789",
            "audit_id": 99,
            "project_id": "project-kimi",
        }
    )

    response = _FakeResponse(
        {
            "error": {
                "message": "Invalid API key",
                "type": "auth_error",
            }
        },
        status_code=401,
    )
    client = _FakeClient(response)
    monkeypatch.setattr(adapter, "_get_client", AsyncMock(return_value=client))

    try:
        result = await adapter._query_single("What is Alpha?")
    finally:
        if getattr(adapter, "_client", None) is not None:
            await adapter._client.aclose()

    assert result.error_code.value == "auth_failed"
    assert client.calls == 1
    assert client.response.status_code == 401
    assert client.response.text


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


class TestExtractSearchQuery:
    """Tests for _extract_search_query_from_tool_call method (Issue 2.3)."""

    def test_extract_search_query_from_string_args(self):
        """Extract search query from JSON string arguments."""
        adapter = KimiAdapter()
        tool_call = {
            "id": "call_123",
            "type": "function",
            "function": {
                "name": "$web_search",
                "arguments": '{"query": "Python async await tutorial"}',
            },
        }
        result = adapter._extract_search_query_from_tool_call(tool_call)
        assert result == "Python async await tutorial"

    def test_extract_search_query_from_dict_args(self):
        """Extract search query from dict arguments (parsed JSON)."""
        adapter = KimiAdapter()
        tool_call = {
            "id": "call_123",
            "type": "function",
            "function": {
                "name": "$web_search",
                "arguments": {"query": "FastAPI vs Flask"},
            },
        }
        result = adapter._extract_search_query_from_tool_call(tool_call)
        assert result == "FastAPI vs Flask"

    def test_extract_search_query_missing_query(self):
        """Return None when query field is missing."""
        adapter = KimiAdapter()
        tool_call = {
            "id": "call_123",
            "type": "function",
            "function": {
                "name": "$web_search",
                "arguments": '{"other_field": "value"}',
            },
        }
        result = adapter._extract_search_query_from_tool_call(tool_call)
        assert result is None

    def test_extract_search_query_invalid_json(self):
        """Return None for invalid JSON string arguments."""
        adapter = KimiAdapter()
        tool_call = {
            "id": "call_123",
            "type": "function",
            "function": {
                "name": "$web_search",
                "arguments": "not valid json {",
            },
        }
        result = adapter._extract_search_query_from_tool_call(tool_call)
        assert result is None

    def test_extract_search_query_wrong_tool(self):
        """Return None for non-$web_search tools."""
        adapter = KimiAdapter()
        tool_call = {
            "id": "call_123",
            "type": "function",
            "function": {
                "name": "other_tool",
                "arguments": '{"query": "test"}',
            },
        }
        result = adapter._extract_search_query_from_tool_call(tool_call)
        assert result is None

    def test_extract_search_query_invalid_input(self):
        """Return None for non-dict input."""
        adapter = KimiAdapter()
        assert adapter._extract_search_query_from_tool_call(None) is None
        assert adapter._extract_search_query_from_tool_call("string") is None
        assert adapter._extract_search_query_from_tool_call([]) is None


class TestKimiAdapterStructure:
    """Tests for Kimi adapter structure and configuration."""

    def test_platform_name(self):
        """Adapter has correct platform name."""
        assert KimiAdapter.platform_name == "kimi"

    def test_search_enabled(self):
        """Search is enabled by default for Kimi."""
        assert KimiAdapter.search_enabled is True

    def test_inherits_from_openai_compat(self):
        """KimiAdapter inherits from OpenAICompatAdapter."""
        from app.adapters.openai_compat import OpenAICompatAdapter
        assert issubclass(KimiAdapter, OpenAICompatAdapter)

    def test_has_extract_method(self):
        """Adapter has the extract method defined."""
        adapter = KimiAdapter()
        assert hasattr(adapter, "_extract_search_query_from_tool_call")
        assert callable(getattr(adapter, "_extract_search_query_from_tool_call"))


class TestSearchQueryEdgeCases:
    """Edge case tests for search query extraction."""

    def test_extract_empty_query(self):
        """Handle empty string query."""
        adapter = KimiAdapter()
        tool_call = {
            "id": "call_123",
            "type": "function",
            "function": {
                "name": "$web_search",
                "arguments": '{"query": ""}',
            },
        }
        result = adapter._extract_search_query_from_tool_call(tool_call)
        assert result == ""

    def test_extract_unicode_query(self):
        """Handle Unicode characters in query (Chinese, emoji)."""
        adapter = KimiAdapter()
        tool_call = {
            "id": "call_123",
            "type": "function",
            "function": {
                "name": "$web_search",
                "arguments": '{"query": "Python 异步编程 🚀"}',
            },
        }
        result = adapter._extract_search_query_from_tool_call(tool_call)
        assert result == "Python 异步编程 🚀"

    def test_extract_nested_query_structure(self):
        """Handle query in nested structure within arguments."""
        adapter = KimiAdapter()
        tool_call = {
            "id": "call_123",
            "type": "function",
            "function": {
                "name": "$web_search",
                "arguments": '{"search": {"query": "nested query"}}',
            },
        }
        result = adapter._extract_search_query_from_tool_call(tool_call)
        # Our implementation only looks at top-level "query"
        assert result is None


@pytest.mark.asyncio
async def test_kimi_parse_error_is_preserved_without_failing_query(monkeypatch):
    adapter = KimiAdapter()
    first_payload = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "type": "function",
                            "function": {
                                "name": "$web_search",
                                "arguments": '{"query": "Alpha V60"}',
                            },
                        }
                    ]
                },
            }
        ]
    }
    final_payload = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": "Alpha is the best option.",
                },
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 4,
        },
        "model": "kimi-test",
    }
    client = _SequencedClient([
        _FakeResponse(first_payload, status_code=200),
        _FakeResponse(final_payload, status_code=200),
    ])

    monkeypatch.setattr(adapter, "_get_client", AsyncMock(return_value=client))

    def broken_extract_citations(*args, **kwargs):
        raise ValueError("bad citation format")

    monkeypatch.setattr("app.adapters.openai_compat.extract_citations", broken_extract_citations)

    try:
        result = await adapter._query_single("What is Alpha V60?")
    finally:
        if getattr(adapter, "_client", None) is not None:
            await adapter._client.aclose()

    assert result.success is True
    assert result.response_text == "Alpha is the best option."
    assert result.citations == []
    assert result.parse_error is not None
    assert "ValueError" in result.parse_error
    assert "bad citation format" in result.parse_error
