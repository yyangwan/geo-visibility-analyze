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
    """Normalized response from a single AI platform for a single prompt."""

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

    @property
    def success(self) -> bool:
        return self.error_code is None


class PlatformAdapter(ABC):
    """Abstract base for AI platform adapters."""

    platform_name: str = ""

    @abstractmethod
    async def query(self, prompts: list[str]) -> list[PlatformResponse]:
        """Query the platform with a list of prompts. Returns one response per prompt."""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Verify the platform API is reachable and authenticated."""
        ...
