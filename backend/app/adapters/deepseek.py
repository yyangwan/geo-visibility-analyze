"""DeepSeek platform adapter.

Uses the official DeepSeek API for the default request path and keeps the
web-capture adapter as an opt-in mode for comparison/debugging.
"""

from app.adapters.deepseek_web import DeepSeekWebAdapter
from app.adapters.openai_compat import OpenAICompatAdapter
from app.config import settings
from app.logging_config import get_logger
from app.services.grounded_answer_service import GroundedAnswerService

logger = get_logger("deepseek")


class DeepSeekAdapter(OpenAICompatAdapter):
    platform_name = "deepseek"
    search_enabled = False
    api_model = "deepseek-v4-flash"

    def __init__(self):
        self.api_key = settings.deepseek_api_key
        self.base_url = "https://api.deepseek.com"
        self.model = self.api_model
        super().__init__()
        self._web_adapter = DeepSeekWebAdapter()
        self._web_adapter.set_platform_config(self.get_platform_config())

    def _gateway_search_mode(self) -> bool:
        return self._capture_mode() == "gateway_search"

    def _bocha_grounded_mode(self) -> bool:
        return self._capture_mode() in {"bocha_grounded", "bocha_search"}

    def _resolve_gateway_config(self) -> dict:
        config = self.get_platform_config()
        gateway = config.get("gateway", {}) if isinstance(config, dict) else {}
        return dict(gateway) if isinstance(gateway, dict) else {}

    def _apply_gateway_overrides(self) -> None:
        gateway_config = self._resolve_gateway_config()

        base_url = gateway_config.get("base_url")
        if isinstance(base_url, str) and base_url.strip():
            self.base_url = base_url.strip()

        api_key = gateway_config.get("api_key")
        if isinstance(api_key, str) and api_key.strip():
            self.api_key = api_key.strip()

        model = gateway_config.get("model")
        if isinstance(model, str) and model.strip():
            self.model = model.strip()

    def _resolve_gateway_search_engine(self) -> str | None:
        gateway_config = self._resolve_gateway_config()
        search_engine = gateway_config.get("search_engine") or gateway_config.get("search_provider")
        if isinstance(search_engine, str):
            search_engine = search_engine.strip()
            if search_engine:
                return search_engine
        return None

    def set_platform_config(self, config: dict) -> None:
        super().set_platform_config(config)
        self._web_adapter.set_platform_config(config)

    def set_runtime_context(self, context: dict | None) -> None:
        super().set_runtime_context(context)
        self._web_adapter.set_runtime_context(context)

    def _capture_mode(self) -> str:
        config = self.get_platform_config()
        capture_mode = config.get("capture_mode")
        if not capture_mode:
            capture_mode = config.get("capture", {}).get("mode")
        return str(capture_mode or "api_compat").lower()

    async def query(self, prompts: list[str]):
        if self._bocha_grounded_mode():
            grounded_service = GroundedAnswerService(
                platform=self.platform_name,
                api_key=self.api_key,
                base_url=self.base_url,
                model=self.model,
                platform_config=self.get_platform_config(),
                runtime_context=self.get_runtime_context(),
                trace_headers=self.build_trace_headers(),
            )
            return await grounded_service.query(prompts)

        if self._capture_mode() == "official_web":
            try:
                return await self._web_adapter.query(prompts)
            except Exception as exc:
                logger.warning(
                    "deepseek_web_capture_failed_falling_back",
                    error=str(exc),
                )
        if self._gateway_search_mode():
            self._apply_gateway_overrides()
        return await super().query(prompts)

    async def health_check(self) -> bool:
        if self._bocha_grounded_mode():
            return bool(self.api_key and settings.bocha_api_key)

        if self._capture_mode() == "official_web":
            try:
                if await self._web_adapter.health_check():
                    return True
            except Exception as exc:
                logger.warning(
                    "deepseek_web_health_check_failed_falling_back",
                    error=str(exc),
                )
        if self._gateway_search_mode():
            self._apply_gateway_overrides()
        return await super().health_check()

    def _build_request_body(self, prompt: str) -> dict:
        """Build request body for the official DeepSeek API.

        The official API follows the standard chat/completions shape and does
        not use the web-search envelope that the web capture path relies on.
        """
        body = super()._build_request_body(prompt)

        # The public API is stateless and does not expose DeepSeek web-search
        # parameters, so strip any search envelope inherited from generic
        # OpenAI-compatible defaults or platform config.
        if not self._gateway_search_mode():
            body.pop("enable_search", None)
            body.pop("search_options", None)
            body.pop("tools", None)
        else:
            search_engine = self._resolve_gateway_search_engine()
            if search_engine:
                body["search_engine"] = search_engine

        return body
