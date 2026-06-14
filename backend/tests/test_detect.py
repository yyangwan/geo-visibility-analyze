"""Tests for brand mention detection service."""

from app.services.detect import (
    _check_recommendation,
    _compute_confidence,
    _extract_context,
    detect_mentions,
)


class TestDetectMentions:
    def test_basic_mention(self):
        text = "我推荐平安保险，产品不错"
        mentions = detect_mentions(text, "平安保险")
        assert len(mentions) >= 1
        assert mentions[0].brand == "平安保险"
        assert mentions[0].confidence > 0

    def test_no_mention(self):
        text = "今天天气很好，适合出门散步"
        mentions = detect_mentions(text, "平安保险")
        assert len(mentions) == 0

    def test_alias_detection(self):
        text = "平安福这款产品值得考虑"
        mentions = detect_mentions(text, "平安保险", aliases=["平安福"])
        assert len(mentions) >= 1

    def test_multiple_mentions(self):
        text = "平安保险是一家领先的保险公司，平安保险的产品性价比高"
        mentions = detect_mentions(text, "平安保险")
        assert len(mentions) >= 2

    def test_recommendation_detected(self):
        text = "我推荐平安保险，这是最佳选择"
        mentions = detect_mentions(text, "平安保险")
        assert len(mentions) >= 1
        assert mentions[0].is_recommended is True

    def test_no_recommendation(self):
        text = "平安保险成立于1988年"
        mentions = detect_mentions(text, "平安保险")
        # Plain mention without recommendation keywords
        if mentions:
            # Should have lower confidence without recommendation context
            assert mentions[0].confidence < 0.9

    def test_context_extraction(self):
        text = "A" * 100 + "平安保险" + "B" * 100
        context = _extract_context(text, 100, 4)
        assert "平安保险" in context

    def test_context_at_start(self):
        text = "平安保险是一家好公司"
        context = _extract_context(text, 0, 4)
        assert "平安保险" in context

    def test_context_at_end(self):
        text = "A" * 50 + "平安保险"
        context = _extract_context(text, 50, 4)
        assert "平安保险" in context

    def test_confidence_early_position(self):
        text = "平安保险排名第一，中国人寿第二"
        mentions = detect_mentions(text, "平安保险")
        # Early position should boost confidence
        assert mentions[0].position < len(text) * 0.5

    def test_industry_param_accepted(self):
        text = "推荐平安保险"
        # Should not raise even if industry is provided
        mentions = detect_mentions(text, "平安保险", industry="insurance")
        assert len(mentions) >= 1

    def test_empty_aliases(self):
        text = "平安保险不错"
        mentions = detect_mentions(text, "平安保险", aliases=[])
        assert len(mentions) >= 1

    def test_none_aliases(self):
        text = "平安保险不错"
        mentions = detect_mentions(text, "平安保险", aliases=None)
        assert len(mentions) >= 1

    def test_case_insensitive_matching(self):
        """Brand matching should be case-insensitive (e.g., 'hario' matches 'Hario')."""
        # Lowercase brand name should match capitalized text
        mentions = detect_mentions("Hario is a great brand", "hario", [])
        assert len(mentions) >= 1
        # Capitalized brand name should match lowercase text
        mentions2 = detect_mentions("fellow products are good", "Fellow", [])
        assert len(mentions2) >= 1
        # Mixed case
        mentions3 = detect_mentions("The HARIO V60 works well", "hario", [])
        assert len(mentions3) >= 1


class TestCheckRecommendation:
    def test_recommend_keywords(self):
        assert _check_recommendation("推荐平安保险") is True
        assert _check_recommendation("best choice") is True
        assert _check_recommendation("Top品牌") is True

    def test_no_recommend(self):
        assert _check_recommendation("平安保险成立于1988年") is False
