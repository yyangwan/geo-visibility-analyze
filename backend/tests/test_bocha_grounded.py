"""Tests for backend-owned Bocha search and DeepSeek grounded synthesis."""

import pytest

from app.services.bocha_search_service import BochaSearchService, SearchResult, normalize_bocha_results
from app.services.grounded_answer_service import (
    GroundedAnswerService,
    append_missing_source_names,
    build_grounded_prompt,
    citations_for_markers,
    extract_source_names,
    parse_source_markers,
)


def test_normalize_bocha_results_accepts_common_payload_shapes(monkeypatch):
    monkeypatch.setattr("app.services.bocha_search_service.settings.bocha_top_k", 3)
    monkeypatch.setattr("app.services.bocha_search_service.settings.bocha_max_per_domain", 2)

    payload = {
        "data": {
            "webPages": {
                "value": [
                    {
                        "name": "Alpha One",
                        "url": "https://Example.com/a#fragment",
                        "summary": "First result",
                        "date": "2026-06-01",
                    },
                    {
                        "title": "Duplicate",
                        "link": "https://example.com/a",
                        "snippet": "Duplicate should be dropped",
                    },
                    {
                        "title": "Alpha Two",
                        "href": "https://www.example.com/b",
                        "content": "Second result",
                    },
                    {
                        "title": "Other",
                        "url": "https://other.test/c",
                        "description": "Third result",
                    },
                ]
            }
        }
    }

    results = normalize_bocha_results(payload, query="alpha")

    assert [result.source_id for result in results] == ["S1", "S2", "S3"]
    assert results[0].url == "https://example.com/a"
    assert results[0].domain == "example.com"
    assert results[0].title == "Alpha One"
    assert results[0].snippet == "First result"
    assert results[0].published_at == "2026-06-01"


def test_bocha_search_service_url_accepts_full_endpoint():
    service = BochaSearchService(
        api_key="test-key",
        base_url="https://ignored.test",
        endpoint="https://api.bocha.cn/v1/web-search",
    )

    assert service._url() == "https://api.bocha.cn/v1/web-search"


def test_source_marker_parsing_and_citation_mapping():
    sources = [
        SearchResult("S1", "One", "https://one.test", "one.test", "one", None, 1, "q"),
        SearchResult("S2", "Two", "https://two.test", "two.test", "two", None, 2, "q"),
    ]

    markers = parse_source_markers("answer [S2] then [S1] and again [S2]")
    citations = citations_for_markers(sources, markers)

    assert markers == [2, 1]
    assert [citation["source_id"] for citation in citations] == ["S2", "S1"]


def test_build_grounded_prompt_includes_source_contract():
    sources = [
        SearchResult("S1", "One", "https://one.test", "one.test", "Snippet", None, 1, "q")
    ]

    prompt = build_grounded_prompt("What is Alpha?", sources)

    assert "USER_QUESTION:" in prompt
    assert "[S1] title: One" in prompt
    assert "source_names:" in prompt
    assert "只使用 SOURCES 中的信息" in prompt


def test_extract_source_names_keeps_brand_like_latin_names():
    source = SearchResult(
        "S1",
        "kalita和hario哪个好？",
        "https://m.gafei.com/views-1",
        "m.gafei.com",
        "Hario V60 and Kalita Wave are common choices.",
        None,
        1,
        "q",
    )

    names = [name.lower() for name in extract_source_names(source)]
    assert "hario" in names
    assert "kalita" in names
    assert "v60" not in names


def test_append_missing_source_names_adds_source_derived_names():
    source = SearchResult(
        "S6",
        "kalita和hario哪个好？",
        "https://m.gafei.com/views-1",
        "m.gafei.com",
        "Hario V60 and Kalita Wave are common choices.",
        None,
        6,
        "q",
    )

    text = append_missing_source_names("选择时关注控水。", [source])

    assert "Hario" in text or "hario" in text
    assert "[S6]" in text


class _FakeSearchService:
    async def search(
        self,
        query,
        count=None,
        top_k=None,
        max_per_domain=None,
        trace_headers=None,
        request_overrides=None,
    ):
        return (
            [
                SearchResult(
                    "S1",
                    "Alpha",
                    "https://alpha.test/news",
                    "alpha.test",
                    "Alpha has a new release.",
                    "2026-06-01",
                    1,
                    query,
                )
            ],
            {"data": {"webPages": {"value": []}}},
            {
                "url": "https://api.bocha.cn/v1/web-search",
                "json": {"query": query, "count": count},
                "top_k": top_k,
                "max_per_domain": max_per_domain,
            },
        )


class _FakeResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "model": "deepseek-test",
            "choices": [
                {
                    "message": {"content": "Alpha 发布了新版本。[S1]"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 8},
        }


class _FakeLlmClient:
    def __init__(self):
        self.calls = []

    async def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return _FakeResponse()


@pytest.mark.asyncio
async def test_grounded_answer_service_returns_citations_and_metadata(monkeypatch):
    service = GroundedAnswerService(
        platform="deepseek",
        api_key="deepseek-key",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        platform_config={
            "grounding": {"search_count": 5, "top_k": 3, "max_per_domain": 1},
            "request": {"temperature": 0.2},
        },
        trace_headers={"X-Audit-Id": "7"},
    )
    fake_llm = _FakeLlmClient()
    async def get_fake_llm_client():
        return fake_llm

    monkeypatch.setattr(service, "_get_search_service", lambda: _FakeSearchService())
    monkeypatch.setattr(service, "_get_llm_client", get_fake_llm_client)

    result = (await service.query(["What is Alpha?"]))[0]

    assert result.success is True
    assert result.response_text == "Alpha 发布了新版本。[S1]"
    assert result.citations[0]["url"] == "https://alpha.test/news"
    assert result.search_metadata["search_provider"] == "bocha"
    assert result.search_metadata["grounding_mode"] == "bocha_grounded"
    assert result.search_metadata["citation_markers"] == [1]
    assert result.request_params["search"]["json"]["count"] == 5
    assert result.request_params["search"]["top_k"] == 3
    assert result.request_params["search"]["max_per_domain"] == 1
    assert result.request_params["llm"]["temperature"] == 0.2
    assert fake_llm.calls[0]["headers"]["X-Audit-Id"] == "7"
