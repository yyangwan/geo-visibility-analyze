"""Tests for DecisionIntentFilter."""

import pytest

from app.services.decision_intent_filter import (
    DecisionIntentFilter,
    IntentAnalysis,
    create_decision_intent_filter,
)


class TestIntentAnalysis:
    """Test IntentAnalysis dataclass."""

    def test_to_dict(self):
        """Test conversion to dictionary."""
        analysis = IntentAnalysis(
            query="重疾险怎么选",
            intent_strength="MEDIUM",
            confidence=0.75,
            reason="Selection advice",
            suggested_action="KEEP",
        )
        result = analysis.to_dict()
        assert result["query"] == "重疾险怎么选"
        assert result["intent_strength"] == "MEDIUM"
        assert result["confidence"] == 0.75
        assert result["reason"] == "Selection advice"
        assert result["suggested_action"] == "KEEP"


class TestDecisionIntentFilter:
    """Test DecisionIntentFilter."""

    @pytest.fixture
    def filter(self):
        """Create a DecisionIntentFilter instance for testing."""
        return DecisionIntentFilter()

    # HIGH intent tests (5 tests)
    def test_analyze_direct_comparison_high_intent(self, filter):
        """Test direct comparison queries are HIGH intent."""
        analysis = filter.analyze("平安福和太保金佑怎么选")
        assert analysis.intent_strength == "HIGH"
        assert analysis.suggested_action == "KEEP"
        assert "comparison" in analysis.reason.lower()

    def test_analyze_recommendation_request_high_intent(self, filter):
        """Test recommendation requests are HIGH intent."""
        analysis = filter.analyze("推荐一款重疾险")
        assert analysis.intent_strength == "HIGH"
        assert analysis.suggested_action == "KEEP"

    def test_analyze_which_better_high_intent(self, filter):
        """Test 'which is better' queries are HIGH intent."""
        analysis = filter.analyze("平安福和金佑人生哪个好")
        assert analysis.intent_strength == "HIGH"
        assert analysis.suggested_action == "KEEP"

    def test_analyze_which_more_high_intent(self, filter):
        """Test 'which is more' queries are HIGH intent."""
        analysis = filter.analyze("平安和太保哪个更划算")
        assert analysis.intent_strength == "HIGH"
        assert analysis.suggested_action == "KEEP"

    def test_analyze_explicit_compare_high_intent(self, filter):
        """Test explicit comparison requests are HIGH intent."""
        analysis = filter.analyze("对比一下平安福和金佑人生")
        assert analysis.intent_strength == "HIGH"
        assert analysis.suggested_action == "KEEP"

    # MEDIUM intent tests (5 tests)
    def test_analyze_how_to_choose_medium_intent(self, filter):
        """Test 'how to choose' queries are MEDIUM intent."""
        analysis = filter.analyze("重疾险怎么选")
        assert analysis.intent_strength == "MEDIUM"
        assert analysis.suggested_action == "KEEP"

    def test_analyze_suitable_question_medium_intent(self, filter):
        """Test suitability questions are MEDIUM intent."""
        analysis = filter.analyze("重疾险适合上班族吗")
        assert analysis.intent_strength == "MEDIUM"
        assert analysis.suggested_action == "KEEP"

    def test_analyze_how_is_it_medium_intent(self, filter):
        """Test quality evaluation questions are MEDIUM intent."""
        analysis = filter.analyze("平安福这款保险怎么样")
        assert analysis.intent_strength == "MEDIUM"
        assert analysis.suggested_action == "KEEP"

    def test_analyze_worth_it_medium_intent(self, filter):
        """Test 'worth it' evaluation questions are MEDIUM intent."""
        analysis = filter.analyze("平安福值得买吗")
        assert analysis.intent_strength == "MEDIUM"
        assert analysis.suggested_action == "KEEP"

    def test_analyze_advice_question_medium_intent(self, filter):
        """Test advice-seeking questions are MEDIUM intent."""
        analysis = filter.analyze("买重疾险有什么建议")
        assert analysis.intent_strength == "MEDIUM"
        assert analysis.suggested_action == "KEEP"

    # LOW intent tests (5 tests)
    def test_analyze_what_is_low_intent(self, filter):
        """Test 'what is' questions are LOW intent."""
        analysis = filter.analyze("什么是重疾险")
        assert analysis.intent_strength == "LOW"
        assert analysis.suggested_action == "FILTER"

    def test_analyze_introduction_low_intent(self, filter):
        """Test introduction requests are LOW intent."""
        analysis = filter.analyze("重疾险简介")
        assert analysis.intent_strength == "LOW"
        assert analysis.suggested_action == "FILTER"

    def test_analyze_definition_low_intent(self, filter):
        """Test definition requests are LOW intent."""
        analysis = filter.analyze("重疾险的定义")
        assert analysis.intent_strength == "LOW"
        assert analysis.suggested_action == "FILTER"

    def test_analyze_history_low_intent(self, filter):
        """Test historical information questions are LOW intent."""
        analysis = filter.analyze("重疾险发展历史")
        assert analysis.intent_strength == "LOW"
        assert analysis.suggested_action == "FILTER"

    def test_analyze_application_process_low_intent(self, filter):
        """Test application process questions are LOW intent."""
        analysis = filter.analyze("重疾险如何申请")
        assert analysis.intent_strength == "LOW"
        assert analysis.suggested_action == "FILTER"


class TestFilterBatch:
    """Test filter_batch method."""

    @pytest.fixture
    def filter(self):
        """Create a DecisionIntentFilter instance for testing."""
        return DecisionIntentFilter()

    def test_filter_batch_keeps_high_medium_intent(self, filter):
        """Test that filter_batch keeps HIGH and MEDIUM intent queries."""
        queries = [
            "什么是重疾险",  # LOW - filtered
            "重疾险怎么选",  # MEDIUM - kept
            "平安福和太保金佑怎么选",  # HIGH - kept
            "重疾险历史",  # LOW - filtered
            "平安福值得买吗",  # MEDIUM - kept
        ]
        result = filter.filter_batch(queries)

        assert len(result) == 3
        assert "重疾险怎么选" in result
        assert "平安福和太保金佑怎么选" in result
        assert "平安福值得买吗" in result
        assert "什么是重疾险" not in result
        assert "重疾险历史" not in result

    def test_filter_batch_empty_input(self, filter):
        """Test filter_batch with empty input."""
        assert filter.filter_batch([]) == []

    def test_filter_batch_all_low_intent_keeps_minimum(self, filter):
        """Test that filter_batch keeps at least 3 queries when all are LOW intent."""
        queries = [
            "什么是重疾险",
            "重疾险简介",
            "重疾险定义",
            "重疾险历史",
            "重疾险如何申请",
        ]
        result = filter.filter_batch(queries)

        # Should keep at least 3 for template fallback
        assert len(result) >= 3


class TestExplainBatch:
    """Test explain_batch method."""

    @pytest.fixture
    def filter(self):
        """Create a DecisionIntentFilter instance for testing."""
        return DecisionIntentFilter()

    def test_explain_batch_returns_analyses(self, filter):
        """Test that explain_batch returns IntentAnalysis for each query."""
        queries = ["重疾险怎么选", "什么是重疾险"]
        analyses = filter.explain_batch(queries)

        assert len(analyses) == 2
        assert all(isinstance(a, IntentAnalysis) for a in analyses)
        assert analyses[0].query == "重疾险怎么选"
        assert analyses[1].query == "什么是重疾险"


class TestFactoryFunction:
    """Test factory function."""

    def test_create_decision_intent_filter(self):
        """Test factory function creates instance."""
        filter_instance = create_decision_intent_filter()
        assert isinstance(filter_instance, DecisionIntentFilter)
