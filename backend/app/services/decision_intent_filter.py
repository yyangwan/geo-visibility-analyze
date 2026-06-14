"""Decision intent filtering for harvested queries.

The filter keeps only queries that are likely to lead to a real decision,
comparison, or purchase recommendation. It intentionally excludes queries
that are informational, glossary-style, or contaminated by industry labels
that leak into prompt generation.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)


@dataclass
class IntentAnalysis:
    """Result of intent analysis for a single query."""

    query: str
    intent_strength: Literal["HIGH", "MEDIUM", "LOW", "UNKNOWN"]
    confidence: float
    reason: str
    suggested_action: Literal["KEEP", "FILTER", "REFINE"]

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "intent_strength": self.intent_strength,
            "confidence": self.confidence,
            "reason": self.reason,
            "suggested_action": self.suggested_action,
        }


class DecisionIntentFilter:
    """Filter user queries by decision intent strength."""

    HIGH_INTENT_PATTERNS = [
        (r"和.*(怎么选|哪个好|哪个更|对比|比较|区别|差异)", "Direct comparison between options"),
        (r"推荐", "Explicit recommendation request"),
        (r"对比", "Explicit comparison request"),
        (r"哪个更", "Which is more comparison"),
        (r"哪个好", "Which is better comparison"),
    ]

    MEDIUM_INTENT_PATTERNS = [
        (r"怎么选", "Selection advice"),
        (r"适合.*吗", "Suitability question"),
        (r"合适.*吗", "Suitability question"),
        (r"怎么样", "Quality evaluation"),
        (r"好用吗", "Usability question"),
        (r"值得.*吗", "Worth-it evaluation"),
        (r"建议", "Advice seeking"),
        (r"选择", "Selection inquiry"),
    ]

    LOW_INTENT_PATTERNS = [
        (r"(电商|企业服务|B2B|B2C|ToB|ToC|行业|领域|垂直)", "Industry or classification jargon"),
        (r"^什么是", "Definition request (what is)"),
        (r"^.*简介", "Introduction request"),
        (r"^.*定义", "Definition request"),
        (r"^.*历史", "Historical information"),
        (r"^.*发展史", "Historical information"),
        (r"如何申请", "Application process"),
        (r"办理流程", "Application process"),
        (r"理赔流程", "Claims process"),
        (r"^.*多少钱", "Price inquiry only"),
        (r"^.*费用", "Cost inquiry only"),
    ]

    def __init__(self) -> None:
        self._high_patterns = [(re.compile(pattern), reason) for pattern, reason in self.HIGH_INTENT_PATTERNS]
        self._medium_patterns = [(re.compile(pattern), reason) for pattern, reason in self.MEDIUM_INTENT_PATTERNS]
        self._low_patterns = [(re.compile(pattern), reason) for pattern, reason in self.LOW_INTENT_PATTERNS]

    def analyze(self, query: str) -> IntentAnalysis:
        """Analyze decision intent of a single query."""

        if not query or not query.strip():
            return IntentAnalysis(
                query=query,
                intent_strength="LOW",
                confidence=1.0,
                reason="Empty query",
                suggested_action="FILTER",
            )

        query = query.strip()

        for pattern, reason in self._low_patterns:
            if pattern.search(query):
                return IntentAnalysis(
                    query=query,
                    intent_strength="LOW",
                    confidence=0.9,
                    reason=reason,
                    suggested_action="FILTER",
                )

        for pattern, reason in self._high_patterns:
            if pattern.search(query):
                return IntentAnalysis(
                    query=query,
                    intent_strength="HIGH",
                    confidence=0.85,
                    reason=reason,
                    suggested_action="KEEP",
                )

        for pattern, reason in self._medium_patterns:
            if pattern.search(query):
                return IntentAnalysis(
                    query=query,
                    intent_strength="MEDIUM",
                    confidence=0.75,
                    reason=reason,
                    suggested_action="KEEP",
                )

        return IntentAnalysis(
            query=query,
            intent_strength="LOW",
            confidence=0.5,
            reason="No decision intent pattern detected",
            suggested_action="FILTER",
        )

    def filter_batch(self, queries: list[str]) -> list[str]:
        """Return only medium/high intent queries."""

        if not queries:
            return []

        results: list[str] = []
        stats = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "UNKNOWN": 0}

        for query in queries:
            analysis = self.analyze(query)
            stats[analysis.intent_strength] += 1
            if analysis.suggested_action == "KEEP":
                results.append(query)

        logger.info(
            "decision_intent_filter_applied",
            extra={
                "total_input": len(queries),
                "high_intent": stats["HIGH"],
                "medium_intent": stats["MEDIUM"],
                "low_filtered": stats["LOW"],
                "unknown_filtered": stats["UNKNOWN"],
                "final_kept": len(results),
                "filter_rate": 1 - (len(results) / len(queries)) if queries else 0,
            },
        )

        return results

    def explain_batch(self, queries: list[str]) -> list[IntentAnalysis]:
        return [self.analyze(query) for query in queries]


def create_decision_intent_filter() -> DecisionIntentFilter:
    """Factory function to create a DecisionIntentFilter instance."""

    return DecisionIntentFilter()
