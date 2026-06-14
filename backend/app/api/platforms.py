"""Platform configuration API — list platforms and their status."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.registry import PLATFORM_LABELS, available_platforms
from app.api.access import require_workspace_scope
from app.api.auth import get_current_user
from app.config import settings
from app.database import async_session
from app.services.platform_config_service import (
    get_all_platform_configs,
    get_platform_config,
    set_platform_config,
)

router = APIRouter()


class PlatformInfo(BaseModel):
    key: str
    label: str
    configured: bool


class PlatformConfigDetail(BaseModel):
    platform: str
    config_version: int
    config_json: dict
    is_active: bool


class PlatformConfigUpdate(BaseModel):
    config_json: dict


@router.get("", response_model=list[PlatformInfo])
async def list_platforms(
    current_user: dict = Depends(get_current_user),
):
    """Return all platforms with their label and whether an API key is configured."""
    require_workspace_scope(current_user)
    api_key_map = {
        "deepseek": settings.deepseek_api_key,
        "qwen": settings.qwen_api_key,
        "doubao": settings.doubao_api_key,
        "kimi": settings.kimi_api_key,
        "hunyuan": settings.hunyuan_api_key,
    }
    return [
        PlatformInfo(
            key=key,
            label=PLATFORM_LABELS.get(key, key),
            configured=bool(api_key_map.get(key)),
        )
        for key in available_platforms()
    ]


@router.get("/configs", response_model=dict)
async def list_all_platform_configs(
    current_user: dict = Depends(get_current_user),
):
    """Return all platform configurations (merged with defaults)."""
    require_workspace_scope(current_user)
    async with async_session() as db:
        return await get_all_platform_configs(db)


@router.get("/configs/{platform}", response_model=dict)
async def get_platform_config_endpoint(
    platform: str,
    current_user: dict = Depends(get_current_user),
):
    """Return configuration for a specific platform."""
    require_workspace_scope(current_user)
    async with async_session() as db:
        return await get_platform_config(db, platform)


@router.post("/configs/{platform}", response_model=PlatformConfigDetail)
async def set_platform_config_endpoint(
    platform: str,
    update: PlatformConfigUpdate,
    current_user: dict = Depends(get_current_user),
):
    """Create or update platform configuration.

    This allows fine-tuning platform behavior:
    - Search parameters (enable_search, search_options, tools)
    - Request defaults (temperature, max_tokens)
    - Parsing rules (citation_format, extraction paths)

    Requires workspace scope.
    """
    require_workspace_scope(current_user)
    async with async_session() as db:
        config = await set_platform_config(db, platform, update.config_json)
        await db.commit()
        await db.refresh(config)
        return PlatformConfigDetail(
            platform=config.platform,
            config_version=config.config_version,
            config_json=config.config_json,
            is_active=config.is_active,
        )
