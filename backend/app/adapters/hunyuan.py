"""Hunyuan (腾讯混元/元宝) platform adapter by Tencent.

Uses the Hunyuan API (OpenAI-compatible).
"""

from app.adapters.openai_compat import OpenAICompatAdapter
from app.config import settings


class HunyuanAdapter(OpenAICompatAdapter):
    platform_name = "hunyuan"
    base_url = "https://api.hunyuan.cloud.tencent.com/v1"
    model = "hunyuan-lite"

    def __init__(self):
        self.api_key = settings.hunyuan_api_key
        super().__init__()
