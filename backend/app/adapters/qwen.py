"""Qwen (Tongyi Qianwen) platform adapter.

Uses the OpenAI-compatible API provided by Alibaba Cloud DashScope.
"""

from app.adapters.openai_compat import OpenAICompatAdapter
from app.config import settings


class QwenAdapter(OpenAICompatAdapter):
    platform_name = "qwen"
    base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model = "qwen-plus"

    def __init__(self):
        self.api_key = settings.qwen_api_key
        super().__init__()
