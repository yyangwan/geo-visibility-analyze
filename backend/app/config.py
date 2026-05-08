from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Database
    database_url: str = "postgresql+asyncpg://aiscope:aiscope@localhost:5432/aiscope"

    # DeepSeek API
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-chat"

    # Qwen (Tongyi) API
    qwen_api_key: str = ""
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen-plus"

    # Query settings
    query_timeout_seconds: int = 30
    max_concurrent_per_platform: int = 5

    model_config = {"env_file": ".env", "env_prefix": "AISCOPE_"}


settings = Settings()
