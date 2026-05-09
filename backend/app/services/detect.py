"""Brand mention detection with context-window filtering.

Detects brand mentions in AI responses by:
1. Finding exact keyword matches (brand name or aliases)
2. Checking surrounding context for industry keywords to disambiguate
3. Scoring confidence based on position, context, and recommendation signals
"""

import re

from app.adapters.base import Mention

# Recommendation keywords indicating the AI is recommending a brand
_RECOMMEND_KEYWORDS = [
    "推荐", "建议", "首选", "最好", "最佳", "值得", "优选",
    "不错", "理想", "可以考虑", "首选", "首选品牌",
    "top", "recommend", "best", "suggest",
]

# Sentiment keywords for positive context
_POSITIVE_KEYWORDS = [
    "优秀", "出色", "领先", "专业", "可靠", "信赖", "口碑好",
    "服务好", "实力强", "知名度高", "覆盖广",
]

# Window size for context extraction (chars before/after match)
_CONTEXT_WINDOW = 60

# Characters that indicate a word boundary
_WORD_BOUNDARY = re.compile(r"[\s,，。.!！?？、；;：:""\n（）()【】\[\]{}]")


def detect_mentions(
    text: str,
    brand_name: str,
    aliases: list[str] | None = None,
    industry: str = "",
) -> list[Mention]:
    """Detect all mentions of a brand (and its aliases) in the given text.

    Args:
        text: The AI platform response text.
        brand_name: Primary brand name to search for.
        aliases: Alternative names/abbreviations for the brand.
        industry: Industry context for disambiguation.

    Returns:
        List of Mention objects sorted by position.
    """
    keywords = [brand_name] + (aliases or [])
    # Deduplicate and sort by length (longest first to avoid partial matches)
    keywords = sorted(set(keywords), key=len, reverse=True)

    mentions: list[Mention] = []
    seen_positions: set[int] = set()

    for keyword in keywords:
        if not keyword:
            continue
        for pos in _find_all_occurrences(text, keyword):
            if pos in seen_positions:
                continue
            seen_positions.add(pos)

            context = _extract_context(text, pos, len(keyword))
            confidence = _compute_confidence(
                text, pos, len(keyword), context, industry
            )
            is_recommended = _check_recommendation(context)

            mentions.append(
                Mention(
                    brand=brand_name,
                    position=pos,
                    context=context,
                    confidence=confidence,
                    is_recommended=is_recommended,
                )
            )

    mentions.sort(key=lambda m: m.position)
    return mentions


def _find_all_occurrences(text: str, keyword: str) -> list[int]:
    """Find all positions of keyword in text, preferring whole-word matches."""
    positions = []
    start = 0
    while True:
        pos = text.find(keyword, start)
        if pos == -1:
            break
        positions.append(pos)
        start = pos + 1
    return positions


def _extract_context(text: str, pos: int, length: int) -> str:
    """Extract surrounding context around a mention."""
    start = max(0, pos - _CONTEXT_WINDOW)
    end = min(len(text), pos + length + _CONTEXT_WINDOW)
    return text[start:end]


def _compute_confidence(
    text: str,
    pos: int,
    length: int,
    context: str,
    industry: str,
) -> float:
    """Compute confidence score for a mention.

    Factors:
    - Is it a standalone mention (word boundaries)?
    - Is it in a recommendation context?
    - Is it near industry-related terms?
    - Position in response (earlier = higher confidence for visibility).
    """
    score = 0.5  # base score for exact match

    # Word boundary check - standalone mentions are more confident
    before_ok = pos == 0 or _WORD_BOUNDARY.match(text[pos - 1])
    after_ok = (
        pos + length >= len(text)
        or _WORD_BOUNDARY.match(text[pos + length])
    )
    if before_ok and after_ok:
        score += 0.2

    # Recommendation context boost
    if _check_recommendation(context):
        score += 0.15

    # Positive sentiment boost
    context_lower = context.lower()
    if any(kw in context_lower for kw in _POSITIVE_KEYWORDS):
        score += 0.1

    # Position factor - earlier mentions matter more for visibility
    text_len = len(text)
    if text_len > 0:
        relative_pos = pos / text_len
        if relative_pos < 0.3:
            score += 0.05

    return min(round(score, 2), 1.0)


def _check_recommendation(context: str) -> bool:
    """Check if the context suggests the AI is recommending the brand."""
    context_lower = context.lower()
    return any(kw in context_lower for kw in _RECOMMEND_KEYWORDS)
