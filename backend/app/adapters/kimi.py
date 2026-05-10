"""Kimi platform adapter by Moonshot AI.

Uses the Moonshot API (OpenAI-compatible).
"""

from app.adapters.openai_compat import OpenAICompatAdapter
from app.config import settings


class KimiAdapter(OpenAICompatAdapter):
    platform_name = "kimi"

    def __init__(self):
        self.api_key = settings.kimi_api_key
        self.base_url = settings.kimi_base_url
        self.model = settings.kimi_model
        super().__init__()
