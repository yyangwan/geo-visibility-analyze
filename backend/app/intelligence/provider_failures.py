"""Safe provider status classification; response bodies and URLs stay private."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderFailure:
    code: str
    retryable: bool


def classify_provider_status(status: int) -> ProviderFailure:
    permanent = {
        400: "analysis_provider_request_invalid",
        401: "analysis_provider_credentials_invalid",
        402: "analysis_provider_insufficient_balance",
        403: "analysis_provider_access_denied",
        404: "analysis_provider_not_found",
    }
    if status in permanent:
        return ProviderFailure(permanent[status], False)
    if status == 429:
        return ProviderFailure("analysis_provider_rate_limited", True)
    if status == 408 or status >= 500:
        return ProviderFailure("analysis_provider_unavailable", True)
    return ProviderFailure("analysis_provider_request_rejected", False)
