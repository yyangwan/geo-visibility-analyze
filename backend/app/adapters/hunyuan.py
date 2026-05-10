"""Hunyuan (腾讯混元/元宝) platform adapter by Tencent.

Uses the Hunyuan API (OpenAI-compatible).
"""

from app.adapters.openai_compat import OpenAICompatAdapter
from app.config import settings


class HunyuanAdapter(OpenAICompatAdapter):
    platform_name = "hunyuan"

    def __init__(self):
        self.api_key = settings.hunyuan_api_key
        self.base_url = settings.hunyuan_base_url
        self.model = settings.hunyuan_model
        super().__init__()
