"""Tests for source extraction service."""

from app.services.source_extraction import (
    ExtractedSource,
    _extract_domain,
    _extract_tier15,
    _extract_tier2,
    _merge_by_domain,
    _normalize_domain,
    extract_sources,
)


class TestExtractSources:
    """Integration tests for the full extract_sources pipeline."""

    def test_empty_text(self):
        assert extract_sources("") == []

    def test_none_api_citations(self):
        sources = extract_sources("一些文字")
        assert isinstance(sources, list)

    def test_tier15_only_chinese_keywords(self):
        text = "根据知乎上的讨论和百度百科的资料，平安保险不错"
        sources = extract_sources(text)
        domains = {s.domain for s in sources}
        assert "zhihu.com" in domains
        assert "baike.baidu.com" in domains

    def test_tier2_only_urls(self):
        text = "参考 https://example.com/docs 和 http://other.org/page"
        sources = extract_sources(text)
        domains = {s.domain for s in sources}
        assert "example.com" in domains
        assert "other.org" in domains

    def test_tier1_structured_citations(self):
        api_cites = [
            {"url": "https://www.zhihu.com/question/123", "title": "测试"},
            {"url": "https://csdn.net/article/456", "title": "文章"},
        ]
        sources = extract_sources("回复内容", api_citations=api_cites)
        domains = {s.domain for s in sources}
        assert "zhihu.com" in domains
        assert "csdn.net" in domains

    def test_tier1_and_tier15_merge(self):
        """Tier 1 URL and Tier 1.5 keyword for same domain should merge."""
        api_cites = [{"url": "https://zhuanlan.zhihu.com/p/123", "title": "文章标题"}]
        text = "根据知乎上的讨论"
        sources = extract_sources(text, api_citations=api_cites)
        # Should merge into one source (zhihu.com)
        zhihu = [s for s in sources if s.domain == "zhihu.com"]
        assert len(zhihu) == 1
        assert zhihu[0].title == "文章标题"
        assert "https://zhuanlan.zhihu.com/p/123" in zhihu[0].urls

    def test_tier1_and_tier2_merge(self):
        """Tier 1 structured URL and Tier 2 URL regex should merge."""
        api_cites = [{"url": "https://www.example.com/a", "title": "官方"}]
        text = "详见 https://www.example.com/b"
        sources = extract_sources(text, api_citations=api_cites)
        example = [s for s in sources if s.domain == "example.com"]
        assert len(example) == 1
        assert len(example[0].urls) == 2

    def test_no_sources_found(self):
        text = "今天天气很好，适合出门散步"
        sources = extract_sources(text)
        assert sources == []


class TestTier15:
    """Tests for Tier 1.5 keyword detection."""

    def test_common_keywords(self):
        text = "知乎、小红书、CSDN、微博、B站"
        sources = _extract_tier15(text)
        domains = {s.domain for s in sources}
        assert "zhihu.com" in domains
        assert "xiaohongshu.com" in domains
        assert "csdn.net" in domains
        assert "weibo.com" in domains
        assert "bilibili.com" in domains

    def test_no_duplicates(self):
        text = "知乎说知乎也对知乎错"
        sources = _extract_tier15(text)
        zhihu = [s for s in sources if s.domain == "zhihu.com"]
        assert len(zhihu) == 1

    def test_case_insensitive_csdn(self):
        text = "csdn上有一篇文章"
        sources = _extract_tier15(text)
        domains = {s.domain for s in sources}
        assert "csdn.net" in domains

    def test_empty_text(self):
        assert _extract_tier15("") == []

    def test_no_matches(self):
        assert _extract_tier15("没有任何来源关键词") == []


class TestTier2:
    """Tests for Tier 2 URL regex extraction."""

    def test_extract_urls(self):
        text = "参考 https://example.com/a 和 http://other.org/b?x=1"
        sources = _extract_tier2(text)
        domains = {s.domain for s in sources}
        assert "example.com" in domains
        assert "other.org" in domains

    def test_strips_www(self):
        text = "链接 https://www.example.com/page"
        sources = _extract_tier2(text)
        assert sources[0].domain == "example.com"

    def test_no_urls(self):
        assert _extract_tier2("没有URL的文本") == []

    def test_url_with_trailing_punctuation(self):
        text = "参考 https://example.com/docs。"
        sources = _extract_tier2(text)
        assert len(sources) >= 1


class TestDomainNormalization:
    """Tests for subdomain normalization."""

    def test_known_subdomain(self):
        roots = {"zhihu.com"}
        assert _normalize_domain("zhuanlan.zhihu.com", roots) == "zhihu.com"

    def test_www_prefix(self):
        roots = {"example.com"}
        assert _normalize_domain("www.example.com", roots) == "example.com"

    def test_unknown_subdomain_kept(self):
        roots = {"zhihu.com"}
        assert _normalize_domain("sub.example.com", roots) == "sub.example.com"

    def test_exact_match(self):
        roots = {"zhihu.com"}
        assert _normalize_domain("zhihu.com", roots) == "zhihu.com"

    def test_deep_subdomain(self):
        roots = {"baidu.com"}
        assert _normalize_domain("a.b.c.baidu.com", roots) == "baidu.com"


class TestExtractDomain:
    """Tests for _extract_domain utility."""

    def test_simple_url(self):
        assert _extract_domain("https://example.com/path") == "example.com"

    def test_with_www(self):
        assert _extract_domain("https://www.example.com/path") == "example.com"

    def test_with_port(self):
        assert _extract_domain("http://localhost:8080/path") == "localhost"

    def test_no_protocol(self):
        assert _extract_domain("example.com/path") == "example.com"

    def test_empty(self):
        assert _extract_domain("") == ""


class TestMergeByDomain:
    """Tests for domain merge logic."""

    def test_dedup_same_domain(self):
        sources = [
            ExtractedSource(domain="zhihu.com", urls=["https://zhihu.com/a"]),
            ExtractedSource(domain="zhihu.com"),
        ]
        merged = _merge_by_domain(sources)
        assert len(merged) == 1
        assert merged[0].domain == "zhihu.com"

    def test_merge_urls(self):
        sources = [
            ExtractedSource(domain="example.com", urls=["https://example.com/a"]),
            ExtractedSource(domain="example.com", urls=["https://example.com/b"]),
        ]
        merged = _merge_by_domain(sources)
        assert len(merged[0].urls) == 2

    def test_keep_richer_title(self):
        sources = [
            ExtractedSource(domain="example.com", title="短标题"),
            ExtractedSource(domain="example.com", title="这是一个更长的标题"),
        ]
        merged = _merge_by_domain(sources)
        assert merged[0].title == "这是一个更长的标题"

    def test_empty_list(self):
        assert _merge_by_domain([]) == []
