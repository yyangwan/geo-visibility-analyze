"""Tests for adapter platform config injection (Issue 2.1)."""

import pytest

from app.adapters.deepseek import DeepSeekAdapter
from app.adapters.base import PlatformAdapter


def test_adapter_has_config_methods():
    """Test that adapter has config getter/setter methods."""
    adapter = DeepSeekAdapter()

    # Should have set_platform_config method
    assert hasattr(adapter, "set_platform_config")
    assert callable(getattr(adapter, "set_platform_config"))

    # Should have get_platform_config method
    assert hasattr(adapter, "get_platform_config")
    assert callable(getattr(adapter, "get_platform_config"))


def test_adapter_config_initially_empty():
    """Test that adapter config is initially empty."""
    adapter = DeepSeekAdapter()
    config = adapter.get_platform_config()
    assert config == {}


def test_adapter_set_and_get_config():
    """Test setting and getting adapter config."""
    adapter = DeepSeekAdapter()

    test_config = {
        "search": {
            "enable_search": True,
            "search_options": {"forced_search": False},
        },
        "request": {
            "temperature": 0.7,
            "max_tokens": 3000,
        },
    }

    adapter.set_platform_config(test_config)
    retrieved_config = adapter.get_platform_config()

    assert retrieved_config == test_config


def test_adapter_request_body_uses_config():
    """Test that _build_request_body uses platform config."""
    adapter = DeepSeekAdapter()

    # Set custom config
    custom_config = {
        "search": {
            "enable_search": True,
            "search_options": {"forced_search": False},
        },
        "request": {
            "temperature": 0.8,
            "max_tokens": 2000,
        },
    }
    adapter.set_platform_config(custom_config)

    # Build request body
    body = adapter._build_request_body("test prompt")

    # Should use config temperature
    assert body["temperature"] == 0.8
    # Should use config max_tokens
    assert body["max_tokens"] == 2000
    # Public DeepSeek API does not accept the web-search envelope
    assert "enable_search" not in body
    assert "search_options" not in body


def test_adapter_request_body_defaults_without_config():
    """Test that _build_request_body uses defaults without config."""
    adapter = DeepSeekAdapter()

    assert adapter.base_url == "https://api.deepseek.com"
    assert adapter.model == "deepseek-v4-flash"

    # No config set
    body = adapter._build_request_body("test prompt")

    # Should use default temperature
    assert body["temperature"] == 0.3
    assert body["model"] == "deepseek-v4-flash"
    # Public DeepSeek API requests stay on the standard chat/completions path
    assert "enable_search" not in body
    assert "search_options" not in body


def test_adapter_request_body_partial_config():
    """Test that partial config is merged with defaults."""
    adapter = DeepSeekAdapter()

    # Set only temperature, leave search config empty
    partial_config = {
        "request": {
            "temperature": 0.9,
        },
        "search": {},
    }
    adapter.set_platform_config(partial_config)

    body = adapter._build_request_body("test prompt")

    # Should use config temperature
    assert body["temperature"] == 0.9
    # Search envelope should still be stripped for the public API path
    assert "enable_search" not in body


def test_adapter_runtime_context_builds_trace_headers():
    """Test that runtime trace context becomes gateway headers."""
    adapter = DeepSeekAdapter()
    adapter.set_runtime_context(
        {
            "analysis_run_id": "run-123",
            "audit_id": 42,
            "project_id": "project-abc",
        }
    )

    headers = adapter.build_trace_headers()

    assert headers["X-Analysis-Run-Id"] == "run-123"
    assert headers["X-Audit-Id"] == "42"
    assert headers["X-Project-Id"] == "project-abc"
    assert adapter._web_adapter.get_runtime_context()["analysis_run_id"] == "run-123"
