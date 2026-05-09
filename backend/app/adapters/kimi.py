"""Kimi platform adapter by Moonshot AI.

Uses the Moonshot API (OpenAI-compatible).
"""

from app.adapters.openai_compat import OpenAICompatAdapter
from app.config import settings


class KimiAdapter(OpenAICompatAdapter):
    platform_name = "kimi"
    base_url = "https://api.moonshot.cn/v1"
    model = "moonshot-v1-8k"

    def __init__(self):
        self.api_key = settings.kimi_api_key
        super().__init__()
