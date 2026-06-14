"""Tests for response parsing utilities.

Related issues:
- Issue 3.1: Citation extraction (numeric, source fields, markdown links)
- Issue 3.2: Search status extraction (enabled, triggered, query)
- Issue 3.3: Parse error handling (non-blocking degradation)
"""

import pytest

from app.services.response_parser import (
    _extract_domain,
    _extract_markdown_citations,
    _get_nested_value,
    extract_citations,
    extract_search_metadata,
    extract_with_parse_fallback,
    CITATION_FORMAT_SEARCH_RESULTS,
    CITATION_FORMAT_TOOL_CALLS,
    CITATION_FORMAT_DEEPSEEK_WEB,
    CITATION_FORMAT_MARKDOWN,
    CITATION_FORMAT_NONE,
)


class TestNestedValueExtraction:
    """Test JSONPath-style nested value extraction."""

    def test_get_nested_value_simple_path(self):
        """Test extracting simple nested values."""
        data = {"a": {"b": {"c": "value"}}}
        assert _get_nested_value(data, "a.b.c") == "value"

    def test_get_nested_value_with_array_index(self):
        """Test extracting array elements by index."""
        data = {"choices": [{"message": {"content": "hello"}}]}
        assert _get_nested_value(data, "choices.0.message.content") == "hello"

    def test_get_nested_value_missing_key(self):
        """Test missing key returns default."""
        data = {"a": {"b": "value"}}
        assert _get_nested_value(data, "a.x.y", "default") == "default"

    def test_get_nested_value_invalid_index(self):
        """Test invalid array index returns default."""
        data = {"choices": [{"message": "hello"}]}
        assert _get_nested_value(data, "choices.5.message", "default") == "default"

    def test_get_nested_value_empty_path(self):
        """Test empty path returns default."""
        data = {"a": "value"}
        assert _get_nested_value(data, "", "default") == "default"

    def test_get_nested_value_none_data(self):
        """Test None data returns default."""
        assert _get_nested_value(None, "a.b.c", "default") == "default"


class TestCitationExtraction:
    """Test citation extraction from various formats."""

    def test_extract_citations_none_format(self):
        """Test 'none' format returns empty list."""
        data = {"choices": [{"message": {"content": "some text"}}]}
        citations = extract_citations(data, CITATION_FORMAT_NONE)
        assert citations == []

    def test_extract_citations_empty_data(self):
        """Test empty data returns empty list."""
        citations = extract_citations(None, CITATION_FORMAT_SEARCH_RESULTS)
        assert citations == []

    def test_extract_citations_search_results_format(self):
        """Test extracting citations from search_results array."""
        data = {
            "choices": [{
                "message": {
                    "search_results": [
                        {"url": "https://example.com/1", "title": "Example 1", "domain": "example.com"},
                        {"url": "https://test.org/2", "title": "Test 2"},
                    ]
                }
            }]
        }
        citations = extract_citations(data, CITATION_FORMAT_SEARCH_RESULTS, "choices.0.message.search_results")

        assert len(citations) == 2
        assert citations[0]["url"] == "https://example.com/1"
        assert citations[0]["title"] == "Example 1"
        assert citations[0]["domain"] == "example.com"
        assert citations[1]["url"] == "https://test.org/2"
        assert citations[1]["domain"] == "test.org"

    def test_extract_citations_search_results_missing_url(self):
        """Test citations without URL are skipped."""
        data = {
            "choices": [{
                "message": {
                    "search_results": [
                        {"title": "No URL"},
                        {"url": "https://example.com/1", "title": "Has URL"},
                    ]
                }
            }]
        }
        citations = extract_citations(data, CITATION_FORMAT_SEARCH_RESULTS)

        assert len(citations) == 1
        assert citations[0]["url"] == "https://example.com/1"

    def test_extract_citations_search_results_alternate_fields(self):
        """Test extracting from link/href fields."""
        data = {
            "choices": [{
                "message": {
                    "search_results": [
                        {"link": "https://example.com/1"},
                        {"href": "https://test.org/2"},
                    ]
                }
            }]
        }
        citations = extract_citations(data, CITATION_FORMAT_SEARCH_RESULTS)

        assert len(citations) == 2
        assert citations[0]["url"] == "https://example.com/1"
        assert citations[1]["url"] == "https://test.org/2"

    def test_extract_citations_tool_calls_format(self):
        """Test extracting citations from tool_calls (Kimi-style)."""
        data = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "function": {
                            "name": "$web_search",
                            "arguments": '{"search_results": [{"url": "https://kimi.com/result", "title": "Kimi Result"}]}',
                        }
                    }]
                }
            }]
        }
        citations = extract_citations(data, CITATION_FORMAT_TOOL_CALLS)

        assert len(citations) == 1
        assert citations[0]["url"] == "https://kimi.com/result"
        assert citations[0]["title"] == "Kimi Result"

    def test_extract_citations_tool_calls_dict_arguments(self):
        """Test tool_calls with dict arguments (parsed JSON)."""
        data = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "function": {
                            "name": "$web_search",
                            "arguments": {
                                "results": [
                                    {"url": "https://example.com/1", "title": "Result 1"},
                                ]
                            },
                        }
                    }]
                }
            }]
        }
        citations = extract_citations(data, CITATION_FORMAT_TOOL_CALLS)

        assert len(citations) == 1
        assert citations[0]["url"] == "https://example.com/1"

    def test_extract_citations_tool_calls_invalid_json(self):
        """Test tool_calls with invalid JSON arguments logs warning."""
        data = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "function": {
                            "name": "$web_search",
                            "arguments": "invalid json{{{",
                        }
                    }]
                }
            }]
        }
        citations = extract_citations(data, CITATION_FORMAT_TOOL_CALLS)
        # Should return empty list, not crash
        assert citations == []

    def test_extract_citations_markdown_format(self):
        """Test extracting markdown-style citations."""
        data = {
            "choices": [{
                "message": {
                    "content": "Check out [Example Site](https://example.com) and [Another](https://test.org \"Test Title\")"
                }
            }]
        }
        citations = extract_citations(data, CITATION_FORMAT_MARKDOWN)

        assert len(citations) == 2
        assert citations[0]["url"] == "https://example.com"
        assert citations[0]["title"] == "Example Site"
        assert citations[0]["domain"] == "example.com"
        assert citations[1]["url"] == "https://test.org"
        assert citations[1]["title"] == "Test Title"

    def test_extract_citations_deepseek_web_format(self):
        """Test extracting citations from DeepSeek web payloads."""
        data = {
            "response": {
                "fragments": [
                    {
                        "index": -1,
                        "results": [
                            {
                                "url": "https://example.com/article",
                                "title": "Example Article",
                                "snippet": "Short summary",
                                "site_name": "Example",
                                "site_icon": "https://example.com/icon.png",
                                "cite_index": 7,
                                "query_indexes": [0, 2],
                                "published_at": 1780848000,
                            }
                        ],
                    }
                ]
            }
        }
        citations = extract_citations(data, CITATION_FORMAT_DEEPSEEK_WEB)

        assert len(citations) == 1
        assert citations[0]["url"] == "https://example.com/article"
        assert citations[0]["domain"] == "example.com"
        assert citations[0]["cite_index"] == 7


class TestDomainExtraction:
    """Test domain extraction from URLs."""

    def test_extract_domain_simple_url(self):
        """Test extracting domain from simple URL."""
        assert _extract_domain("https://example.com/path") == "example.com"
        assert _extract_domain("http://test.org") == "test.org"

    def test_extract_domain_with_port(self):
        """Test extracting domain removes port."""
        assert _extract_domain("https://example.com:8080/path") == "example.com"

    def test_extract_domain_with_subdomain(self):
        """Test extracting domain preserves subdomain."""
        assert _extract_domain("https://www.example.com/path") == "www.example.com"
        assert _extract_domain("https://api.service.io") == "api.service.io"

    def test_extract_domain_invalid(self):
        """Test invalid URLs return None."""
        assert _extract_domain("") is None
        assert _extract_domain(None) is None
        assert _extract_domain(123) is None


class TestMarkdownCitationExtraction:
    """Test markdown link parsing."""

    def test_extract_markdown_citations_simple(self):
        """Test simple markdown links."""
        text = "See [Google](https://google.com) for more."
        citations = _extract_markdown_citations(text)

        assert len(citations) == 1
        assert citations[0]["url"] == "https://google.com"
        assert citations[0]["title"] == "Google"
        assert citations[0]["domain"] == "google.com"

    def test_extract_markdown_citations_with_title_attr(self):
        """Test markdown links with title attribute."""
        text = '[Link](https://example.com "Title in quotes")'
        citations = _extract_markdown_citations(text)

        assert len(citations) == 1
        assert citations[0]["title"] == "Title in quotes"

    def test_extract_markdown_citations_multiple(self):
        """Test extracting multiple markdown links."""
        text = "[One](https://one.com) text [Two](https://two.org)"
        citations = _extract_markdown_citations(text)

        assert len(citations) == 2
        assert citations[0]["url"] == "https://one.com"
        assert citations[1]["url"] == "https://two.org"

    def test_extract_markdown_citations_none(self):
        """Test text without links returns empty list."""
        text = "No links here, just plain text."
        citations = _extract_markdown_citations(text)
        assert citations == []


class TestSearchMetadataExtraction:
    """Test search metadata extraction."""

    def test_extract_search_metadata_empty_data(self):
        """Test empty data returns default metadata."""
        metadata = extract_search_metadata(None, search_enabled=True)

        assert metadata["search_enabled"] is True
        assert metadata["search_triggered"] is False
        assert metadata["search_query"] is None
        assert metadata["search_reasoning"] is None
        assert metadata["search_results_count"] == 0

    def test_extract_search_metadata_with_search_status(self):
        """Test extracting search status."""
        data = {
            "choices": [{
                "message": {
                    "search_status": "triggered",
                    "search_query": "test query",
                }
            }]
        }
        metadata = extract_search_metadata(data, search_enabled=True)

        assert metadata["search_enabled"] is True
        assert metadata["search_triggered"] is True
        assert metadata["search_query"] == "test query"

    def test_extract_search_status_disabled(self):
        """Test 'disabled' status means search not triggered."""
        data = {
            "choices": [{
                "message": {
                    "search_status": "disabled",
                }
            }]
        }
        metadata = extract_search_metadata(data, search_enabled=True)

        assert metadata["search_triggered"] is False

    def test_extract_search_status_boolean(self):
        """Test boolean search status."""
        data = {
            "choices": [{
                "message": {
                    "search_status": True,
                }
            }]
        }
        metadata = extract_search_metadata(data, search_enabled=True)

        assert metadata["search_triggered"] is True

    def test_extract_search_results_count(self):
        """Test counting search results."""
        data = {
            "choices": [{
                "message": {
                    "search_results": [
                        {"url": "https://example.com/1"},
                        {"url": "https://example.com/2"},
                        {"url": "https://example.com/3"},
                    ]
                }
            }]
        }
        metadata = extract_search_metadata(data, search_enabled=True)

        assert metadata["search_results_count"] == 3
        assert metadata["search_triggered"] is True  # Results imply search was triggered

    def test_extract_search_with_reasoning(self):
        """Test extracting search reasoning."""
        data = {
            "choices": [{
                "message": {
                    "search_status": "triggered",
                    "search_reasoning": "Need to find current information",
                    "search_query": "latest AI models 2024",
                }
            }]
        }
        metadata = extract_search_metadata(data, search_enabled=True)

        assert metadata["search_reasoning"] == "Need to find current information"
        assert metadata["search_query"] == "latest AI models 2024"

    def test_extract_search_custom_paths(self):
        """Test using custom JSONPath expressions."""
        data = {
            "result": {
                "did_search": True,
                "search_term": "custom query",
                "results": [{"url": "https://example.com"}],
            }
        }
        metadata = extract_search_metadata(
            data,
            search_enabled=True,
            search_status_path="result.did_search",
            search_query_path="result.search_term",
            search_results_path="result.results",
        )

        assert metadata["search_triggered"] is True
        assert metadata["search_query"] == "custom query"
        assert metadata["search_results_count"] == 1

    def test_extract_search_tool_calls_triggered(self):
        """Test tool_calls with $web_search means search triggered."""
        data = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "function": {"name": "$web_search"}
                    }]
                }
            }]
        }
        metadata = extract_search_metadata(data, search_enabled=True)

        assert metadata["search_triggered"] is True

    def test_extract_search_tool_calls_web_search(self):
        """Test tool_calls with web_search (no $) means search triggered."""
        data = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "function": {"name": "web_search"}
                    }]
                }
            }]
        }
        metadata = extract_search_metadata(data, search_enabled=True)

        assert metadata["search_triggered"] is True

    def test_extract_search_other_tool_calls(self):
        """Test other tool_calls don't trigger search flag."""
        data = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "function": {"name": "code_interpreter"}
                    }]
                }
            }]
        }
        metadata = extract_search_metadata(data, search_enabled=True)

        assert metadata["search_triggered"] is False


class TestParseErrorHandling:
    """Test parse error fallback handling (Issue 3.3)."""

    def test_extract_with_parse_fallback_success(self):
        """Test successful extraction returns value and no error."""
        data = {"key": "value"}

        def extract_func(d):
            return d["key"]

        result, error = extract_with_parse_fallback(data, extract_func, default_value="fallback")

        assert result == "value"
        assert error is None

    def test_extract_with_parse_fallback_exception(self):
        """Test extraction failure returns default and error message."""
        data = {"invalid": "data"}

        def extract_func(d):
            return d["missing_key"]  # Will raise KeyError

        result, error = extract_with_parse_fallback(data, extract_func, default_value="fallback")

        assert result == "fallback"
        assert error is not None
        assert "KeyError" in error

    def test_extract_with_parse_fallback_custom_default(self):
        """Test custom default value on error."""
        data = {}

        def extract_func(d):
            return d["missing"]

        result, error = extract_with_parse_fallback(data, extract_func, default_value=42)

        assert result == 42
        assert error is not None

    def test_extract_with_parse_fallback_type_error(self):
        """Test type errors are caught."""
        data = {"value": "not a number"}

        def extract_func(d):
            return int(d["value"])  # Will raise ValueError

        result, error = extract_with_parse_fallback(data, extract_func, default_value=0)

        assert result == 0
        assert error is not None
        assert "ValueError" in error or "int" in error
