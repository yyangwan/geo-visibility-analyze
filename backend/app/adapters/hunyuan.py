"""Hunyuan (腾讯混元/元宝) platform adapter by Tencent.

Uses the Hunyuan API (OpenAI-compatible).
Supports web search via enable_search parameter.
"""

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

    def _build_request_body(self, prompt: str) -> dict:
        body = super()._build_request_body(prompt)
        body["enable_search"] = True
        return body
