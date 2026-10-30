"""Bounded structured extraction; vendor options only target verified endpoints."""

from urllib.parse import urlsplit

VERSION = "structured-extraction-policy-1"


def structured_options(base_url, model, *, verification=False):
    options = {"max_tokens": 2048 if verification else 8192}
    # https://api-docs.deepseek.com/guides/thinking_mode/
    # Official V4/Flash defaults to thinking, which can consume the entire JSON
    # output budget. Never send this vendor extension to another provider.
    if urlsplit(base_url).hostname == "api.deepseek.com" and model in (
        "deepseek-flash",
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "deepseek-v4-flash-vision-exp",
    ):
        options["thinking"] = {"type": "disabled"}
    return options
