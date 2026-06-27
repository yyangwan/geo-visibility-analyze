"""Hunyuan (腾讯混元/元宝) platform adapter by Tencent.

Uses the Hunyuan API (OpenAI-compatible).
Supports native enhancement via enable_enhancement parameter. The API response
does not currently expose structured citation/source lists.
"""

import asyncio

from app.adapters.openai_compat import OpenAICompatAdapter
from app.config import settings


class HunyuanAdapter(OpenAICompatAdapter):
    platform_name = "hunyuan"
    search_enabled = True

    def __init__(self):
        self.api_key = settings.hunyuan_api_key
        self.base_url = settings.hunyuan_base_url
        self.model = settings.hunyuan_model
        super().__init__()
        self.semaphore = asyncio.Semaphore(1)
        self._rate_limit_retries = 5

    def _build_request_body(self, prompt: str) -> dict:
        body = super()._build_request_body(prompt)
        config = self.get_platform_config()
        request_config = config.get("request", {}) if isinstance(config, dict) else {}
        body["model"] = request_config.get("model") or self.model
        body.pop("enable_search", None)
        body.pop("search_options", None)
        body["enable_enhancement"] = True
        return body
