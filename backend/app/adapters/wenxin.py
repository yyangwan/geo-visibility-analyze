"""Wenxin (文心一言) platform adapter by Baidu.

Uses the Qianfan ModelBuilder API (OpenAI-compatible).
"""

from app.adapters.openai_compat import OpenAICompatAdapter
from app.config import settings


class WenxinAdapter(OpenAICompatAdapter):
    platform_name = "wenxin"

    def __init__(self):
        self.api_key = settings.wenxin_api_key
        self.base_url = settings.wenxin_base_url
        self.model = settings.wenxin_model
        super().__init__()
