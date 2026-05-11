import os
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Database
    database_url: str = "mysql+aiomysql://aiscope:aiscope@localhost:3306/aiscope"

    # DeepSeek API
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-chat"

    # Qwen (Tongyi) API
    qwen_api_key: str = ""
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen-plus"

    # Doubao (Volcengine/ByteDance) API
    doubao_api_key: str = ""
    doubao_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    doubao_model: str = "doubao-1-5-pro-32k-250115"

    # Kimi (Moonshot) API
    kimi_api_key: str = ""
    kimi_base_url: str = "https://api.moonshot.cn/v1"
    kimi_model: str = "moonshot-v1-8k"

    # Wenxin (Baidu Qianfan) API
    wenxin_api_key: str = ""
    wenxin_base_url: str = "https://qianfan.baidubce.com/v2"
    wenxin_model: str = "ernie-4.0-8k"

    # Hunyuan (Tencent) API
    hunyuan_api_key: str = ""
    hunyuan_base_url: str = "https://api.hunyuan.cloud.tencent.com/v1"
    hunyuan_model: str = "hunyuan-lite"

    # Query settings
    query_timeout_seconds: int = 60
    max_concurrent_per_platform: int = 5

    # Analysis settings
    analysis_timeout_seconds: int = 120

    # LLM for internal tasks (prompt gen, suggestions). Defaults to DeepSeek.
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""

    # Auth
    secret_key: str = ""
    access_token_expire_minutes: int = 1440  # 24h

    # Timezone for scheduler (default: China Standard Time)
    tz: str = "Asia/Shanghai"

    model_config = {
        "env_file": str(Path(__file__).resolve().parents[2] / ".env"),
        "env_prefix": "AISCOPE_",
    }

    @field_validator("secret_key")
    @classmethod
    def _validate_secret_key(cls, v: str) -> str:
        if not v:
            debug = os.getenv("AISCOPE_DEBUG", "0") == "1"
            if debug:
                return "aiscope-dev-secret-do-not-use-in-production"
            raise ValueError(
                "AISCOPE_SECRET_KEY must be set. "
                "Set it in .env or as an environment variable."
            )
        return v

    def get_llm_config(self) -> tuple[str, str, str]:
        """Return (api_key, base_url, model) for internal LLM tasks."""
        return (
            self.llm_api_key or self.deepseek_api_key,
            self.llm_base_url or self.deepseek_base_url,
            self.llm_model or self.deepseek_model,
        )


settings = Settings()
