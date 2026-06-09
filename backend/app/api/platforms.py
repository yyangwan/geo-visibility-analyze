"""Platform configuration API — list platforms and their status."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.adapters.registry import PLATFORM_LABELS, available_platforms
from app.api.access import require_workspace_scope
from app.api.auth import get_current_user
from app.config import settings

router = APIRouter()


class PlatformInfo(BaseModel):
    key: str
    label: str
    configured: bool


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
