"""Adapter registry for managing platform adapters."""

from app.adapters.base import PlatformAdapter
from app.adapters.deepseek import DeepSeekAdapter
from app.adapters.doubao import DoubaoAdapter
from app.adapters.hunyuan import HunyuanAdapter
from app.adapters.kimi import KimiAdapter
from app.adapters.qwen import QwenAdapter
from app.adapters.wenxin import WenxinAdapter

_ADAPTERS: dict[str, type[PlatformAdapter]] = {
    "deepseek": DeepSeekAdapter,
    "qwen": QwenAdapter,
    "doubao": DoubaoAdapter,
    "kimi": KimiAdapter,
    "wenxin": WenxinAdapter,
    "hunyuan": HunyuanAdapter,
}


def get_adapter(platform: str) -> PlatformAdapter:
    """Get an adapter instance for the given platform name."""
    cls = _ADAPTERS.get(platform)
    if not cls:
        raise ValueError(f"Unknown platform: {platform}. Available: {list(_ADAPTERS.keys())}")
    return cls()


def get_adapters(platforms: list[str]) -> list[PlatformAdapter]:
    """Get adapter instances for multiple platforms."""
    return [get_adapter(p) for p in platforms]


def available_platforms() -> list[str]:
    """List all registered platform names."""
    return list(_ADAPTERS.keys())
