"""Base adapter interface and shared data types for AI platform integrations."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class ErrorCode(str, Enum):
    AUTH_FAILED = "auth_failed"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    FORMAT_ERROR = "format_error"
    PLATFORM_DOWN = "platform_down"
    UNKNOWN = "unknown"


@dataclass
class Mention:
    """A brand mention found in an AI platform response."""

    brand: str
    position: int  # character offset in response
    context: str  # surrounding text (±50 chars)
    confidence: float  # 0.0-1.0
    is_recommended: bool = False


@dataclass
class PlatformResponse:
    """Normalized response from a single AI platform for a single prompt.

    Issue 2.2: Extended to support complete response archiving:
    - raw_response: Full platform API response as dict
    - raw_response_text: Text representation for search/indexing
    - search_metadata: Search query, reasoning, and triggered status
    - request_params: Request parameters sent to the platform
    """

    platform: str
    prompt: str
    response_text: str
    mentions: list[Mention] = field(default_factory=list)
    error_code: ErrorCode | None = None
    error_message: str | None = None
    latency_ms: int = 0
    # Enrichment fields (populated by adapters with search mode)
    citations: list[dict] = field(default_factory=list)  # [{url, title, domain}]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    response_model: str = ""
    finish_reason: str = ""
    search_enabled: bool = False

    # Issue 2.2: Raw response archiving fields
    raw_response: dict | None = None  # Full API response as JSON
    raw_response_text: str | None = None  # Stringified version
    search_metadata: dict | None = None  # Search query, reasoning, results
    request_params: dict | None = None  # Request parameters sent
    parse_error: str | None = None  # Non-blocking parsing error, if any

    @property
    def success(self) -> bool:
        return self.error_code is None


class PlatformAdapter(ABC):
    """Abstract base for AI platform adapters."""

    platform_name: str = ""
    trace_header_map = {
        "analysis_run_id": "X-Analysis-Run-Id",
        "audit_id": "X-Audit-Id",
        "project_id": "X-Project-Id",
    }

    def __init__(self):
        """Initialize adapter. Subclasses may override to load config."""
        self._platform_config: dict | None = None
        self._runtime_context: dict | None = None

    def set_platform_config(self, config: dict) -> None:
        """Inject platform configuration into the adapter.

        Issue 2.1: Allows adapters to use platform-specific configuration
        from the database instead of hardcoded values.

        Args:
            config: Platform configuration dict with search, request, and parsing settings
        """
        self._platform_config = config

    def get_platform_config(self) -> dict:
        """Get the current platform configuration.

        Returns empty dict if no config has been set (uses defaults).
        """
        return self._platform_config or {}

    def set_runtime_context(self, context: dict | None) -> None:
        """Inject per-request runtime context such as trace ids."""
        self._runtime_context = context or {}

    def get_runtime_context(self) -> dict:
        """Get the current runtime context."""
        return self._runtime_context or {}

    def build_trace_headers(self) -> dict[str, str]:
        """Build headers that should be forwarded for traceability."""
        context = self.get_runtime_context()
        headers: dict[str, str] = {}
        for key, header_name in self.trace_header_map.items():
            value = context.get(key)
            if value is None:
                continue
            value_str = str(value).strip()
            if value_str:
                headers[header_name] = value_str
        return headers

    @abstractmethod
    async def query(self, prompts: list[str]) -> list[PlatformResponse]:
        """Query the platform with a list of prompts. Returns one response per prompt."""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Verify the platform API is reachable and authenticated."""
        ...
