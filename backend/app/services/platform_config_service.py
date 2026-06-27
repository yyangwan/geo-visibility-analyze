"""Platform configuration management service.

Provides:
- Loading platform-specific search parameters
- Storing and retrieving platform configurations
- Versioning and rollback support for config changes

Related issues:
- Issue 1.1: Platform config table
- Issue 2.1: Adapter reads platform config
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging_config import get_logger
from app.models.models import PlatformConfig

logger = get_logger("platform_config")

# Default configurations for each platform
# These can be overridden by database configs
_DEFAULT_PLATFORM_CONFIGS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "capture_mode": "api_compat",
        "search": {
            "enable_search": False,
            "search_options": {},
        },
        "gateway": {
            "base_url": None,
            "api_key": None,
            "model": None,
            "search_engine": "bocha",
        },
        "grounding": {
            "provider": "bocha",
            "mode": "search_then_synthesize",
            "search_count": 10,
            "top_k": 8,
            "max_per_domain": 2,
            "citation_marker_format": "[S{index}]",
            "bocha_request": {},
        },
        "request": {
            "temperature": 0.3,
            "max_tokens": None,
        },
        "web": {
            "endpoint": "https://chat.deepseek.com/api/v0/chat/completion",
            "chat_session_id": None,
            "parent_message_id": None,
            "thinking_enabled": False,
            "search_enabled": True,
            "ref_file_ids": [],
            "preempt": False,
            "headers": {
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Origin": "https://chat.deepseek.com",
                "Referer": "https://chat.deepseek.com/",
            },
        },
        "parsing": {
            # Issue 3.1: Citation extraction from search_results array
            "citation_format": "search_results",
            "citation_path": "choices.0.message.search_results",
            # Issue 3.2: Search metadata extraction paths
            "search_status_path": "choices.0.message.search_status",
            "search_query_path": "choices.0.message.search_query",
            "search_reasoning_path": "choices.0.message.search_reasoning",
            "search_results_path": "choices.0.message.search_results",
        },
    },
    "kimi": {
        "capture_mode": "native_search",
        "search": {
            "enable_search": True,
            "tools": ["web_search"],
            "tool_choice": "auto",
        },
        "request": {
            "model": "kimi-k2.6",
            "temperature": 0.6,
            "top_p": 0.95,
            "max_tokens": 8192,
            "system_prompt": (
                "你是 Kimi，一个由月之暗面（Moonshot AI）开发的人工智能助手。"
                "你擅长中英文对话，能够基于上下文提供有帮助、无害且诚实的回答。"
                "当需要获取实时信息时，你会主动调用搜索工具。"
                "回答时先给出结论，再给出关键依据。"
                "如使用搜索，请在回答末尾列出引用来源名称和 URL。"
            ),
        },
        "parsing": {
            # Issue 2.3: Kimi uses tool_calls for search
            "citation_format": "tool_calls",
            "supports_multi_round_search": True,
            "search_tool_name": "$web_search",
            # Issue 3.2: Search metadata paths (may be in tool_call arguments)
            "search_status_path": "choices.0.message.search_status",
            "search_query_path": "choices.0.message.search_query",
            "search_results_path": "choices.0.message.search_results",
        },
    },
    "qwen": {
        "search": {
            "enable_search": True,
            "search_options": {"forced_search": True},
        },
        "request": {
            "temperature": 0.3,
            "max_tokens": None,
        },
        "parsing": {
            # Qwen may return citations in search_results
            "citation_format": "search_results",
            "citation_path": "choices.0.message.search_results",
            "search_status_path": "choices.0.message.search_status",
            "search_query_path": "choices.0.message.search_query",
            "search_results_path": "choices.0.message.search_results",
        },
    },
    "doubao": {
        "search": {
            "enable_search": False,
            "search_options": {},
        },
        "request": {
            "temperature": 0.3,
            "max_tokens": None,
        },
        "parsing": {
            "citation_format": "none",
        },
    },
    "wenxin": {
        "search": {
            "enable_search": False,
            "search_options": {},
        },
        "request": {
            "temperature": 0.3,
            "max_tokens": None,
        },
        "parsing": {
            "citation_format": "none",
        },
    },
    "hunyuan": {
        "search": {
            "enable_search": False,
            "search_options": {},
        },
        "request": {
            "model": "hunyuan-turbos-latest",
            "temperature": 0.3,
            "max_tokens": None,
        },
        "parsing": {
            "citation_format": "none",
        },
    },
}


async def get_platform_config(
    db: AsyncSession,
    platform: str,
) -> dict[str, Any]:
    """Load configuration for a platform.

    Returns database config if exists and active, otherwise returns default.

    Args:
        db: Database session
        platform: Platform name (e.g., "deepseek", "kimi")

    Returns:
        Configuration dictionary with search, request, and parsing settings
    """
    result = await db.execute(
        select(PlatformConfig).where(
            PlatformConfig.platform == platform,
            PlatformConfig.is_active == True,
        )
    )
    config = result.scalar_one_or_none()

    if config is not None:
        logger.debug(
            "platform_config_loaded",
            platform=platform,
            version=config.config_version,
        )
        return config.config_json

    # Return default config
    default = _DEFAULT_PLATFORM_CONFIGS.get(platform, {})
    logger.debug(
        "platform_config_default",
        platform=platform,
        has_default=bool(default),
    )
    return default


async def set_platform_config(
    db: AsyncSession,
    platform: str,
    config_json: dict[str, Any],
) -> PlatformConfig:
    """Create or update platform configuration.

    Args:
        db: Database session
        platform: Platform name
        config_json: Configuration dictionary

    Returns:
        Created or updated PlatformConfig instance
    """
    result = await db.execute(
        select(PlatformConfig).where(PlatformConfig.platform == platform)
    )
    existing = result.scalar_one_or_none()

    if existing is not None:
        # Update existing config, increment version
        existing.config_json = config_json
        existing.config_version += 1
        existing.is_active = True
        await db.flush()
        logger.info(
            "platform_config_updated",
            platform=platform,
            version=existing.config_version,
        )
        return existing

    # Create new config
    config = PlatformConfig(
        platform=platform,
        config_json=config_json,
        config_version=1,
        is_active=True,
    )
    db.add(config)
    await db.flush()
    logger.info(
        "platform_config_created",
        platform=platform,
        version=1,
    )
    return config


async def get_all_platform_configs(
    db: AsyncSession,
) -> dict[str, dict[str, Any]]:
    """Load all platform configurations.

    Returns merged config (db overrides defaults) for all platforms.

    Args:
        db: Database session

    Returns:
        Dictionary mapping platform name to config
    """
    result = await db.execute(
        select(PlatformConfig).where(PlatformConfig.is_active == True)
    )
    db_configs = result.scalars().all()

    merged = {}
    for platform, default in _DEFAULT_PLATFORM_CONFIGS.items():
        merged[platform] = default.copy()

    for config in db_configs:
        merged[config.platform] = config.config_json

    return merged


def get_default_config(platform: str) -> dict[str, Any]:
    """Get default configuration for a platform (no DB access).

    Args:
        platform: Platform name

    Returns:
        Default configuration dictionary
    """
    return _DEFAULT_PLATFORM_CONFIGS.get(platform, {}).copy()
