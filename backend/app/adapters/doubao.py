"""Doubao (豆包) platform adapter by ByteDance.

Uses the Volcengine Ark API (OpenAI-compatible).
Model parameter uses endpoint ID (ep-xxxxx) or model name.
"""

from app.adapters.openai_compat import OpenAICompatAdapter
from app.config import settings


class DoubaoAdapter(OpenAICompatAdapter):
    platform_name = "doubao"
    base_url = "https://ark.cn-beijing.volces.com/api/v3"
    model = "doubao-pro-32k"

    def __init__(self):
        self.api_key = settings.doubao_api_key
        super().__init__()
