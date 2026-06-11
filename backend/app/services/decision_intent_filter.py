"""Decision Intent Filter — Filter queries by decision intent strength.

Only保留 queries likely to trigger brand recommendations in AI responses.
V1: Rule-based filtering
V2: Rule + LLM refinement for uncertain cases
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)


@dataclass
class IntentAnalysis:
    """Result of intent analysis on a single query."""
    query: str
    intent_strength: Literal["HIGH", "MEDIUM", "LOW", "UNKNOWN"]
    confidence: float  # 0-1
    reason: str  # Human-readable explanation
    suggested_action: Literal["KEEP", "FILTER", "REFINE"]

    def to_dict(self) -> dict:
        """Convert to dictionary for logging."""
        return {
            "query": self.query,
            "intent_strength": self.intent_strength,
            "confidence": self.confidence,
            "reason": self.reason,
            "suggested_action": self.suggested_action,
        }


class DecisionIntentFilter:
    """Filter user queries by decision intent strength.

    Only保留 queries likely to trigger brand recommendations in AI responses.

    Intent tiers:
    - HIGH: Direct comparison (X和Y怎么选), explicit recommendation requests (推荐)
    - MEDIUM: Evaluation (值得吗), selection advice (怎么选), scenario-based (适合...吗)
    - LOW: Pure information seeking (什么是, 定义, 历史) — FILTERED

    V1: Rule-based filtering
    V2: Rule + LLM refinement for uncertain cases
    """

    # Intent strength patterns
    HIGH_INTENT_PATTERNS = [
        (r"和.*怎么选", "Direct comparison between brands"),
        (r"推荐", "Explicit recommendation request"),
        (r"哪个好", "Which is better comparison"),
        (r"哪个更", "Which is more comparison"),
        (r"对比", "Explicit comparison request"),
        (r"区别", "Difference inquiry"),
        (r"差异", "Difference inquiry"),
    ]

    MEDIUM_INTENT_PATTERNS = [
        (r"怎么选", "Selection advice"),
        (r"适合.*吗", "Suitability question"),
        (r"怎么样", "Quality evaluation"),
        (r"好用吗", "Usability question"),
        (r"值得.*吗", "Worth it evaluation"),
        (r"建议", "Advice seeking"),
        (r"选择", "Selection inquiry"),
    ]

    LOW_INTENT_PATTERNS = [
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
        """Initialize the filter with compiled regex patterns."""
        self._high_patterns = [(re.compile(p), r) for p, r in self.HIGH_INTENT_PATTERNS]
        self._medium_patterns = [(re.compile(p), r) for p, r in self.MEDIUM_INTENT_PATTERNS]
        self._low_patterns = [(re.compile(p), r) for p, r in self.LOW_INTENT_PATTERNS]

    def analyze(self, query: str) -> IntentAnalysis:
        """Analyze decision intent of a single query.

        Args:
            query: User query string

        Returns:
            IntentAnalysis with strength, confidence, and suggested action
        """
        if not query or not query.strip():
            return IntentAnalysis(
                query=query,
                intent_strength="LOW",
                confidence=1.0,
                reason="Empty query",
                suggested_action="FILTER",
            )

        query = query.strip()

        # Check LOW intent first (most specific patterns)
        for pattern, reason in self._low_patterns:
            if pattern.search(query):
                return IntentAnalysis(
                    query=query,
                    intent_strength="LOW",
                    confidence=0.9,
                    reason=reason,
                    suggested_action="FILTER",
                )

        # Check HIGH intent
        for pattern, reason in self._high_patterns:
            if pattern.search(query):
                return IntentAnalysis(
                    query=query,
                    intent_strength="HIGH",
                    confidence=0.85,
                    reason=reason,
                    suggested_action="KEEP",
                )

        # Check MEDIUM intent
        for pattern, reason in self._medium_patterns:
            if pattern.search(query):
                return IntentAnalysis(
                    query=query,
                    intent_strength="MEDIUM",
                    confidence=0.75,
                    reason=reason,
                    suggested_action="KEEP",
                )

        # If no pattern matches, assume LOW (conservative)
        return IntentAnalysis(
            query=query,
            intent_strength="LOW",
            confidence=0.5,
            reason="No decision intent pattern detected",
            suggested_action="FILTER",
        )

    def filter_batch(self, queries: list[str]) -> list[str]:
        """Filter a batch of queries, returning only medium+ intent.

        Args:
            queries: List of candidate queries

        Returns:
            Filtered list with medium+ decision intent
        """
        if not queries:
            return []

        results = []
        stats = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "UNKNOWN": 0}

        for query in queries:
            analysis = self.analyze(query)
            stats[analysis.intent_strength] += 1

            if analysis.suggested_action == "KEEP":
                results.append(query)

        # Log filtering statistics
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

        # Ensure we return at least 3 queries (for template fallback)
        if len(results) < 3 and len(queries) >= 3:
            logger.warning(
                "filter_result_too_small",
                extra={
                    "kept": len(results),
                    "adding": min(3, len(queries)) - len(results),
                    "reason": "template_backup",
                },
            )
            # Add back some queries (first ones that were filtered)
            for query in queries:
                if query not in results:
                    analysis = self.analyze(query)
                    if analysis.intent_strength in ["HIGH", "MEDIUM", "LOW"]:
                        results.append(query)
                        if len(results) >= 3:
                            break

        return results

    def explain_batch(self, queries: list[str]) -> list[IntentAnalysis]:
        """Return detailed analysis for each query (debug mode).

        Args:
            queries: List of queries to analyze

        Returns:
            List of IntentAnalysis objects with full details
        """
        return [self.analyze(query) for query in queries]


def create_decision_intent_filter() -> DecisionIntentFilter:
    """Factory function to create a DecisionIntentFilter instance.

    This allows for easy mocking in tests.
    """
    return DecisionIntentFilter()
