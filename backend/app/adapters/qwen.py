"""Qwen (Tongyi Qianwen) platform adapter.

Uses the OpenAI-compatible API provided by Alibaba Cloud DashScope.
"""

from app.adapters.openai_compat import OpenAICompatAdapter
from app.config import settings


class QwenAdapter(OpenAICompatAdapter):
    platform_name = "qwen"

    def __init__(self):
        self.api_key = settings.qwen_api_key
        self.base_url = settings.qwen_base_url
        self.model = settings.qwen_model
        super().__init__()
