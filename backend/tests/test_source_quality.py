"""Tests for deterministic source quality scoring."""

from app.services.source_quality import clean_cited_sources, is_valid_source_domain, score_source_authority


def test_score_source_authority_high_authority_domains():
    assert score_source_authority("gov.cn") == 5.0
    assert score_source_authority("baike.baidu.com") == 5.0


def test_score_source_authority_industry_and_community_domains():
    assert score_source_authority("alibaba.com") == 4.0
    assert score_source_authority("zhihu.com") == 3.0


def test_score_source_authority_title_boost_and_mobile_penalty():
    assert score_source_authority("example.com", ["https://m.example.com/list/a"], "官方选购指南") == 3.0


def test_is_valid_source_domain_rejects_synthetic_markers():
    assert is_valid_source_domain("example.com") is True
    assert is_valid_source_domain("https://www.example.com/a") is True
    assert is_valid_source_domain("source_S1") is False
    assert is_valid_source_domain("S1") is False


def test_clean_cited_sources_normalizes_and_filters_invalid_domains():
    sources = clean_cited_sources(
        [
            {"domain": "https://www.example.com/path", "authority_score": 4},
            {"domain": "source_S1", "authority_score": 3},
            {"domain": "S2", "authority_score": 3},
            "not-a-dict",
        ]
    )

    assert sources == [{"domain": "example.com", "authority_score": 4}]
