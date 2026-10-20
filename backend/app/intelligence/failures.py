"""Bounded worker diagnostics; never inspect HTTP content or request metadata."""

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class Failure:
    code: str
    classification: str
    retry: bool
    exception_class: str
    http_status: int | None = None


def classify_failure(error: Exception) -> Failure:
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if type(status) is not int or not 100 <= status <= 599:
            return Failure("http_error", "http_unknown_status", True, "HTTPStatusError")
        if status == 402:
            classification, retry = "http_billing", False
        elif status == 429:
            classification, retry = "http_rate_limited", True
        elif status in (408, 425):
            classification, retry = "http_transient", True
        elif 500 <= status <= 599:
            classification, retry = "http_server_error", True
        elif 400 <= status <= 499:
            classification, retry = "http_permanent", False
        else:
            # Redirects are not followed by provider calls; repetition cannot fix them.
            classification, retry = "http_unexpected_status", False
        return Failure(
            f"http_{status}", classification, retry, "HTTPStatusError", status
        )
    if isinstance(error, httpx.TimeoutException):
        return Failure("http_timeout", "http_timeout", True, "TimeoutException")
    if isinstance(error, httpx.TransportError):
        return Failure(
            "http_transport_error", "http_transport_error", True, "TransportError"
        )
    if isinstance(error, TimeoutError):
        return Failure("timeout", "timeout", True, "TimeoutError")

    # Keep existing known codes, but do not trust arbitrary exception class names.
    name = type(error).__name__
    if name in {
        "ValueError",
        "TypeError",
        "KeyError",
        "LookupError",
        "RuntimeError",
        "OSError",
        "ConnectionError",
        "SourceBlocked",
        "SourceUnavailable",
        "SemanticOutputError",
        "ValidationError",
    }:
        return Failure(name, "unclassified", True, name)
    return Failure("job_error", "unclassified", True, "Exception")
