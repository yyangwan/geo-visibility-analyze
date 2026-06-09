"""Wenxin (文心一言) platform adapter by Baidu.

Uses the Qianfan ModelBuilder API (OpenAI-compatible).
Supports web search via enable_search parameter.
"""

from app.adapters.openai_compat import OpenAICompatAdapter
from app.config import settings


class WenxinAdapter(OpenAICompatAdapter):
    platform_name = "wenxin"
    search_enabled = True

    def __init__(self):
        self.api_key = settings.wenxin_api_key
        self.base_url = settings.wenxin_base_url
        self.model = settings.wenxin_model
        super().__init__()

    def _build_request_body(self, prompt: str) -> dict:
        body = super()._build_request_body(prompt)
        body["enable_search"] = True
        return body
