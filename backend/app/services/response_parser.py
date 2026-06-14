"""Response parsing utilities for extracting citations and search metadata.

This module provides platform-agnostic parsing functions that can extract
structured data from various AI platform API responses.

Related issues:
- Issue 3.1: Citation extraction (numeric, source fields, markdown links)
- Issue 3.2: Search status extraction (enabled, triggered, query)
- Issue 3.3: Parse error handling (non-blocking degradation)
"""

import re
from typing import Any

from app.logging_config import get_logger

logger = get_logger("response_parser")

# Citation format types
CITATION_FORMAT_SEARCH_RESULTS = "search_results"
CITATION_FORMAT_TOOL_CALLS = "tool_calls"
CITATION_FORMAT_MARKDOWN = "markdown"
CITATION_FORMAT_DEEPSEEK_WEB = "deepseek_web"
CITATION_FORMAT_NONE = "none"


def _get_nested_value(data: dict, path: str, default: Any = None) -> Any:
    """Get a nested value from a dict using dot-notation path.

    Args:
        data: Source dictionary
        path: Dot-notation path (e.g., "choices.0.message.content")
        default: Default value if path not found

    Returns:
        Value at path, or default if not found
    """
    if not data or not path:
        return default

    keys = path.split(".")
    current = data

    for key in keys:
        if isinstance(current, dict):
            if key in current:
                current = current[key]
            else:
                return default
        elif isinstance(current, list):
            # Handle array indices
            try:
                index = int(key)
                current = current[index]
            except (ValueError, IndexError, TypeError):
                return default
        else:
            return default

    return current


def extract_citations(
    data: dict,
    citation_format: str,
    citation_path: str | None = None,
) -> list[dict]:
    """Extract citations from platform API response.

    Args:
        data: Full API response dictionary
        citation_format: Type of citation format ("search_results", "tool_calls", "markdown", "none")
        citation_path: Optional JSONPath to citation source (for "search_results" format)

    Returns:
        List of citation dicts with keys: url, title, domain (all optional)
    """
    if citation_format == CITATION_FORMAT_NONE or not data:
        return []

    citations = []

    if citation_format == CITATION_FORMAT_SEARCH_RESULTS:
        # Extract from search_results array
        search_results = _get_nested_value(data, citation_path or "choices.0.message.search_results", [])

        if isinstance(search_results, list):
            for result in search_results:
                if not isinstance(result, dict):
                    continue

                citation = {
                    "url": result.get("url") or result.get("link") or result.get("href"),
                    "title": result.get("title") or result.get("name"),
                    "domain": result.get("domain") or _extract_domain(result.get("url", "")),
                }
                # Only include if we have at least a URL
                if citation["url"]:
                    citations.append(citation)

    elif citation_format == CITATION_FORMAT_DEEPSEEK_WEB:
        citations = parse_deepseek_web_results(data)

    elif citation_format == CITATION_FORMAT_TOOL_CALLS:
        # Extract from tool_calls (Kimi-style)
        # Citations may be embedded in tool_call arguments
        choices = data.get("choices", [])
        for choice in choices:
            message = choice.get("message", {})
            tool_calls = message.get("tool_calls", [])

            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue

                function = tool_call.get("function", {})
                if function.get("name") == "$web_search":
                    # Parse arguments to extract search results
                    arguments = function.get("arguments", "")
                    if isinstance(arguments, str):
                        try:
                            import json
                            arguments = json.loads(arguments)
                        except json.JSONDecodeError:
                            logger.warning("tool_call_arguments_parse_failed", args=arguments[:100])
                            continue

                    if isinstance(arguments, dict):
                        results = arguments.get("search_results") or arguments.get("results", [])
                        for result in results:
                            if isinstance(result, dict):
                                citations.append({
                                    "url": result.get("url"),
                                    "title": result.get("title"),
                                    "domain": result.get("domain") or _extract_domain(result.get("url", "")),
                                })

    elif citation_format == CITATION_FORMAT_MARKDOWN:
        # Extract markdown-style links from response text
        content = _get_nested_value(data, "choices.0.message.content", "")
        if isinstance(content, str):
            citations = _extract_markdown_citations(content)

    logger.debug(
        "citations_extracted",
        format=citation_format,
        count=len(citations),
    )

    return citations


def parse_deepseek_citation_markers(text: str) -> list[int]:
    """Extract [citation:n] markers from DeepSeek web response text."""
    if not text or not isinstance(text, str):
        return []

    marker_pattern = re.compile(r"\[citation:(\d+)\]")
    markers = [int(match.group(1)) for match in marker_pattern.finditer(text)]

    logger.debug(
        "deepseek_citation_markers_extracted",
        count=len(markers),
    )
    return markers


def _normalize_deepseek_web_result(result: dict) -> dict:
    """Normalize a DeepSeek web result into the shared citation shape."""
    url = result.get("url") or result.get("link") or result.get("href")
    title = result.get("title") or result.get("name") or result.get("site_name") or ""
    domain = result.get("domain") or _extract_domain(url or "")

    citation = {
        "url": url,
        "title": title,
        "domain": domain,
    }

    # Preserve DeepSeek-specific metadata when present.
    for key in (
        "snippet",
        "site_name",
        "site_icon",
        "cite_index",
        "query_indexes",
        "published_at",
    ):
        value = result.get(key)
        if value is not None:
            citation[key] = value

    return citation


def parse_deepseek_web_results(data: dict | list | None) -> list[dict]:
    """Extract DeepSeek web citations from a response payload.

    The official web endpoint keeps its reference list under
    ``response.fragments[-1].results``. We also accept fallback shapes so the
    parser survives payload drift in tests and archived snapshots.
    """
    if not data:
        return []

    if isinstance(data, list):
        for item in reversed(data):
            citations = parse_deepseek_web_results(item)
            if citations:
                return citations
        return []

    candidates: list[list[dict]] = []
    search_paths = (
        "response.fragments.-1.results",
        "response.results",
        "results",
        "choices.0.message.search_results",
    )
    for path in search_paths:
        results = _get_nested_value(data, path, None)
        if isinstance(results, list):
            candidates.append(results)

    if not candidates:
        return []

    normalized: list[dict] = []
    for result in candidates[0]:
        if isinstance(result, dict):
            citation = _normalize_deepseek_web_result(result)
            if citation.get("url"):
                normalized.append(citation)

    logger.debug(
        "deepseek_web_results_extracted",
        count=len(normalized),
    )
    return normalized


def align_deepseek_web_citations(results: list[dict], response_text: str) -> list[dict]:
    """Order DeepSeek web citations by citation markers when available."""
    if not results:
        return []

    markers = parse_deepseek_citation_markers(response_text)
    if not markers:
        return results

    by_index = {
        cite.get("cite_index"): cite
        for cite in results
        if isinstance(cite, dict) and cite.get("cite_index") is not None
    }

    aligned: list[dict] = []
    seen: set[int] = set()

    for index in markers:
        citation = by_index.get(index)
        if citation is None or index in seen:
            continue
        aligned.append(citation)
        seen.add(index)

    for citation in results:
        cite_index = citation.get("cite_index")
        if cite_index in seen:
            continue
        aligned.append(citation)

    return aligned


def extract_deepseek_web_text(data: dict | list | None) -> str:
    """Extract final DeepSeek web answer text from a payload."""
    if not data:
        return ""

    if isinstance(data, list):
        for item in reversed(data):
            text = extract_deepseek_web_text(item)
            if text:
                return text
        return ""

    for path in (
        "response.fragments.-1.content",
        "response.fragments.-1.text",
        "response.content",
        "content",
        "choices.0.message.content",
    ):
        text = _get_nested_value(data, path, "")
        if isinstance(text, str) and text.strip():
            return text

    return ""


def _extract_domain(url: str) -> str | None:
    """Extract domain from URL.

    Args:
        url: URL string

    Returns:
        Domain name or None if invalid URL
    """
    if not url or not isinstance(url, str):
        return None

    # Remove protocol
    url = url.split("://")[-1]
    # Remove path and query
    url = url.split("/")[0]
    # Remove port
    url = url.split(":")[0]

    return url if url else None


def _extract_markdown_citations(text: str) -> list[dict]:
    """Extract citations from markdown-style links in text.

    Supports formats:
    - [text](url)
    - [text](url "title")

    Args:
        text: Response text containing markdown links

    Returns:
        List of citation dicts
    """
    # Pattern: [text](url "optional title") or [text](url)
    # URL is captured as non-whitespace chars, title is in quotes
    pattern = r'\[([^\]]+)\]\(([^)\s]+)(?:\s+"([^"]+)")?\)'

    citations = []
    for match in re.finditer(pattern, text):
        url = match.group(2).strip()
        title = match.group(3) or match.group(1)  # Use title attr or link text

        citations.append({
            "url": url,
            "title": title,
            "domain": _extract_domain(url),
        })

    return citations


def extract_search_metadata(
    data: dict,
    search_enabled: bool = False,
    search_status_path: str | None = None,
    search_query_path: str | None = None,
    search_reasoning_path: str | None = None,
    search_results_path: str | None = None,
) -> dict:
    """Extract search metadata from platform API response.

    Args:
        data: Full API response dictionary
        search_enabled: Whether search is enabled for this platform (config default)
        search_status_path: JSONPath to search status field
        search_query_path: JSONPath to search query field
        search_reasoning_path: JSONPath to search reasoning field
        search_results_path: JSONPath to search results array

    Returns:
        Dict with keys:
        - search_enabled: bool
        - search_triggered: bool
        - search_query: str | None
        - search_reasoning: str | None
        - search_results_count: int
    """
    if not data:
        return {
            "search_enabled": search_enabled,
            "search_triggered": False,
            "search_query": None,
            "search_reasoning": None,
            "search_results_count": 0,
        }

    message = _get_nested_value(data, "choices.0.message", {})

    # Extract search status
    search_status = _get_nested_value(
        data,
        search_status_path or "choices.0.message.search_status",
    )

    # Extract search query
    search_query = _get_nested_value(
        data,
        search_query_path or "choices.0.message.search_query",
    )

    # Extract search reasoning
    search_reasoning = _get_nested_value(
        data,
        search_reasoning_path or "choices.0.message.search_reasoning",
    )

    # Extract search results count
    search_results = _get_nested_value(
        data,
        search_results_path or "choices.0.message.search_results",
    )

    if isinstance(search_results, list):
        search_results_count = len(search_results)
    else:
        search_results_count = 0

    # Determine if search was triggered
    # Search is triggered if:
    # 1. search_status indicates active search (not "disabled")
    # 2. OR we have search results
    # 3. OR we have a search query
    search_triggered = False

    if isinstance(search_status, str):
        search_triggered = search_status.lower() not in ("disabled", "none", "false")
    elif isinstance(search_status, bool):
        search_triggered = search_status
    elif search_results_count > 0:
        search_triggered = True
    elif search_query:
        search_triggered = True

    # Also check for tool_calls (Kimi-style search)
    if not search_triggered:
        tool_calls = message.get("tool_calls", [])
        if tool_calls:
            search_triggered = any(
                tc.get("function", {}).get("name") in ("$web_search", "web_search")
                for tc in tool_calls
                if isinstance(tc, dict)
            )

    metadata = {
        "search_enabled": search_enabled,
        "search_triggered": search_triggered,
        "search_query": search_query,
        "search_reasoning": search_reasoning,
        "search_results_count": search_results_count,
    }

    logger.debug(
        "search_metadata_extracted",
        triggered=search_triggered,
        query=search_query,
        results_count=search_results_count,
    )

    return metadata


def extract_with_parse_fallback(
    data: dict,
    extract_func: callable,
    default_value: Any = None,
) -> tuple[Any, str | None]:
    """Safely extract data with parse error fallback.

    Wraps any extraction function to catch exceptions and return parse error.

    Args:
        data: Source data dictionary
        extract_func: Function that extracts data from the dict
        default_value: Value to return if extraction fails

    Returns:
        Tuple of (extracted_value, parse_error | None)
    """
    try:
        return extract_func(data), None
    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}"
        logger.warning(
            "parse_error_fallback",
            error=error_msg,
            exc_info=True,
        )
        return default_value, error_msg
