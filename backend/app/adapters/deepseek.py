"""DeepSeek platform adapter."""

from app.adapters.openai_compat import OpenAICompatAdapter
from app.config import settings


class DeepSeekAdapter(OpenAICompatAdapter):
    platform_name = "deepseek"
    base_url = "https://api.deepseek.com/v1"
    model = "deepseek-chat"

    def __init__(self):
        self.api_key = settings.deepseek_api_key
        super().__init__()
