"""DeepSeek platform adapter.

Uses DashScope (Alibaba Cloud Bailian) to host DeepSeek models with
web search support via enable_search parameter. This gives the same
DeepSeek model quality but with real-time internet access.

Config (in .env):
  AISCOPE_DEEPSEEK_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
  AISCOPE_DEEPSEEK_API_KEY=<dashscope-api-key>
  AISCOPE_DEEPSEEK_MODEL=deepseek-v3   (or deepseek-v3.1, deepseek-r1, etc.)

Note: DeepSeek official API (api.deepseek.com) does NOT support web search.
"""

from app.adapters.openai_compat import OpenAICompatAdapter
from app.config import settings


class DeepSeekAdapter(OpenAICompatAdapter):
    platform_name = "deepseek"
    search_enabled = True

    def __init__(self):
        self.api_key = settings.deepseek_api_key
        self.base_url = settings.deepseek_base_url
        self.model = settings.deepseek_model
        super().__init__()

    def _build_request_body(self, prompt: str) -> dict:
        body = super()._build_request_body(prompt)
        body["enable_search"] = True
        body["search_options"] = {"forced_search": True}
        return body
