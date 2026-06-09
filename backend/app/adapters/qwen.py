"""Qwen (Tongyi Qianwen) platform adapter.

Uses the OpenAI-compatible API provided by Alibaba Cloud DashScope.
Supports web search via enable_search + search_options parameters.
"""

from app.adapters.openai_compat import OpenAICompatAdapter
from app.config import settings


class QwenAdapter(OpenAICompatAdapter):
    platform_name = "qwen"
    search_enabled = True

    def __init__(self):
        self.api_key = settings.qwen_api_key
        self.base_url = settings.qwen_base_url
        self.model = settings.qwen_model
        super().__init__()

    def _build_request_body(self, prompt: str) -> dict:
        body = super()._build_request_body(prompt)
        body["enable_search"] = True
        body["search_options"] = {"forced_search": True}
        return body
