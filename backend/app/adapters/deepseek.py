"""DeepSeek platform adapter."""

from app.adapters.openai_compat import OpenAICompatAdapter
from app.config import settings


class DeepSeekAdapter(OpenAICompatAdapter):
    platform_name = "deepseek"

    def __init__(self):
        self.api_key = settings.deepseek_api_key
        self.base_url = settings.deepseek_base_url
        self.model = settings.deepseek_model
        super().__init__()
