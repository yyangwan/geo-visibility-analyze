"""Wenxin (文心一言) platform adapter by Baidu.

Uses the Qianfan ModelBuilder API (OpenAI-compatible).
"""

from app.adapters.openai_compat import OpenAICompatAdapter
from app.config import settings


class WenxinAdapter(OpenAICompatAdapter):
    platform_name = "wenxin"
    base_url = "https://qianfan.baidubce.com/v2"
    model = "ernie-4.0-8k"

    def __init__(self):
        self.api_key = settings.wenxin_api_key
        super().__init__()
