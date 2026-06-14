"""Tests for platform configuration service.

Related issues:
- Issue 1.1: Platform config table
- Issue 2.1: Adapter reads platform config
"""

import pytest

from app.services.platform_config_service import (
    get_default_config,
    get_platform_config,
    set_platform_config,
)


@pytest.mark.asyncio
async def test_get_default_config_deepseek():
    """Test loading default DeepSeek configuration."""
    config = get_default_config("deepseek")

    assert config is not None
    assert "search" in config
    assert "web" in config
    assert "request" in config
    assert "parsing" in config
    assert config["search"]["enable_search"] is False
    assert config["search"]["search_options"] == {}
    assert config["web"]["headers"]["Origin"] == "https://chat.deepseek.com"
    assert config["web"]["headers"]["Referer"] == "https://chat.deepseek.com/"


@pytest.mark.asyncio
async def test_get_default_config_unknown_platform():
    """Test loading default config for unknown platform returns empty dict."""
    config = get_default_config("unknown_platform")
    assert config == {}


@pytest.mark.asyncio
async def test_get_platform_config_default(db_session):
    """Test loading platform config returns default when DB is empty."""
    config = await get_platform_config(db_session, "deepseek")

    assert config is not None
    assert "search" in config
    assert "web" in config
    assert config["search"]["enable_search"] is False


@pytest.mark.asyncio
async def test_set_and_get_platform_config(db_session):
    """Test setting and retrieving platform configuration."""
    custom_config = {
        "search": {
            "enable_search": True,
            "search_options": {"forced_search": False},
        },
        "request": {
            "temperature": 0.5,
            "max_tokens": 2000,
        },
        "parsing": {
            "citation_format": "custom",
        },
    }

    # Set the config
    saved_config = await set_platform_config(db_session, "test_platform", custom_config)
    await db_session.commit()

    assert saved_config.platform == "test_platform"
    assert saved_config.config_version == 1
    assert saved_config.config_json == custom_config
    assert saved_config.is_active is True

    # Retrieve the config
    loaded_config = await get_platform_config(db_session, "test_platform")

    assert loaded_config == custom_config


@pytest.mark.asyncio
async def test_set_platform_config_updates_version(db_session):
    """Test that updating a config increments the version."""
    config_v1 = {"search": {"enable_search": True}}

    # First save
    saved = await set_platform_config(db_session, "version_test", config_v1)
    await db_session.commit()
    assert saved.config_version == 1

    # Second save (update)
    config_v2 = {"search": {"enable_search": False}}
    updated = await set_platform_config(db_session, "version_test", config_v2)
    await db_session.commit()
    assert updated.config_version == 2
    assert updated.config_json == config_v2


@pytest.mark.asyncio
async def test_get_platform_config_all(db_session):
    """Test loading all platform configurations."""
    from app.services.platform_config_service import get_all_platform_configs

    # Set a custom config for one platform
    custom_config = {"search": {"enable_search": False}}
    await set_platform_config(db_session, "qwen", custom_config)
    await db_session.commit()

    # Get all configs
    all_configs = await get_all_platform_configs(db_session)

    # Should have all default platforms
    expected_platforms = ["deepseek", "kimi", "qwen", "doubao", "wenxin", "hunyuan"]
    for platform in expected_platforms:
        assert platform in all_configs
        assert "search" in all_configs[platform]

    # Custom config should override default
    assert all_configs["qwen"]["search"]["enable_search"] is False


def test_default_platform_configs_reflect_calibration_baseline():
    """Test that default configs encode the intended platform calibration baseline."""
    platforms = {
        "deepseek": {
            "capture_mode": "api_compat",
            "search_enabled": False,
            "citation_format": "search_results",
        },
        "qwen": {
            "search_enabled": True,
            "forced_search": True,
            "citation_format": "search_results",
        },
        "kimi": {
            "search_enabled": True,
            "tools": ["web_search"],
            "citation_format": "tool_calls",
            "supports_multi_round_search": True,
            "search_tool_name": "$web_search",
        },
        "doubao": {
            "search_enabled": False,
            "citation_format": "none",
        },
        "wenxin": {
            "search_enabled": False,
            "citation_format": "none",
        },
        "hunyuan": {
            "search_enabled": False,
            "citation_format": "none",
        },
    }

    for platform, expected in platforms.items():
        config = get_default_config(platform)

        if "capture_mode" in expected:
            assert config["capture_mode"] == expected["capture_mode"]

        assert config["request"]["temperature"] == 0.3
        assert config["request"]["max_tokens"] is None
        assert config["search"]["enable_search"] is expected["search_enabled"]

        parsing = config["parsing"]
        assert parsing["citation_format"] == expected["citation_format"]

        if "tools" in expected:
            assert config["search"]["tools"] == expected["tools"]

        if "supports_multi_round_search" in expected:
            assert parsing["supports_multi_round_search"] is expected["supports_multi_round_search"]

        if "search_tool_name" in expected:
            assert parsing["search_tool_name"] == expected["search_tool_name"]
