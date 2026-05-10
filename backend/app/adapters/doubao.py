"""Doubao (豆包) platform adapter by ByteDance.

Uses the Volcengine Ark API (OpenAI-compatible).
Model parameter uses endpoint ID (ep-xxxxx) or model name.
"""

from app.adapters.openai_compat import OpenAICompatAdapter
from app.config import settings


class DoubaoAdapter(OpenAICompatAdapter):
    platform_name = "doubao"

    def __init__(self):
        self.api_key = settings.doubao_api_key
        self.base_url = settings.doubao_base_url
        self.model = settings.doubao_model
        super().__init__()
