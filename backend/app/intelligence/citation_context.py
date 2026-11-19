"""Frozen answer anchors select observations, never manufacture supporting text."""

import re
from collections import Counter

from app.intelligence.blob import digest

ANCHOR_VERSION = "answer-anchor-v1"
EVIDENCE_POLICY = "anchored-observation-budget-v3"
PAIRS_PER_CLAIM = 12
BRACKET_MARKER = re.compile(r"\[(\d{1,3})\](?!\()|【(\d{1,3})】")
FLATTENED_MARKER = re.compile(r"(?<=[\u3400-\u9fff）)])(\d{1,3})(?=[。！？；])")
QUANTITY_PREFIX = re.compile(
    r"(?:第|版本|型号|系列|编号|数量|等级|级别|排名|上限|下限|总计|最多|最少|"
    r"不少于|不超过|不低于|超过|仅|达|为|约|近|共|有|至|数)$"
    r"|(?:最多|最少|至少|上限|不超过|不少于)[\u3400-\u9fff]{0,5}$"
)
REFERENCE_HEADING = re.compile(
    r"(?m)^\s*(?:#{1,6}\s*)?参考(?:来源|资料|文献)\s*[:：]?\s*$"
)


def answer_reference_markers(response, references):
    """Return character ranges for citation tokens corroborated by this capture."""
    # Use the entire recorded panel, including unavailable URLs, to recognize a
    # citation marker. Missing URLs must not redirect that claim to another article.
    ordinals = {row.get("ordinal") for row in references}
    text = response["text"]
    heading = REFERENCE_HEADING.search(text)
    body = text[: heading.start()] if heading else text
    markers = [
        (match.start(), match.end(), int(match[1] or match[2]))
        for match in BRACKET_MARKER.finditer(body)
        if int(match[1] or match[2]) in ordinals
    ]
    if response["platform"] == "qwen" and response.get("surface") == "web" and heading:
        # Qwen Web flattens superscript references into trailing digits. Bare
        # digits also occur in real quantities, so require repeated use and
        # at least two distinct corroborated reference numbers in the answer.
        bare = [
            (match.start(), match.end(), int(match[1]))
            for match in FLATTENED_MARKER.finditer(body)
            if int(match[1]) in ordinals
            and not QUANTITY_PREFIX.search(
                body[max(0, match.start() - 8) : match.start()]
            )
        ]
        counts = Counter(ordinal for _, _, ordinal in bare)
        if len(counts) >= 2:
            markers.extend(item for item in bare if counts[item[2]] > 1)
    return sorted(markers)


def claim_reference_tokens(text, start, end, markers):
    remaining, cursor, ordinals = [], start, set()
    for left, right, ordinal in markers:
        if start <= left < right <= end:
            remaining.append(text[cursor:left])
            cursor = right
            ordinals.add(ordinal)
    remaining.append(text[cursor:end])
    return "".join(remaining), ordinals


def freeze_anchor(raw, text):
    result = {"version": ANCHOR_VERSION, "answer_hash": digest(text.encode())}
    if raw is None:
        return {**result, "status": "unavailable", "reason": "not_captured"}
    if not (
        isinstance(raw, (list, tuple))
        and len(raw) == 2
        and all(type(value) is int for value in raw)
        and 0 <= raw[0] < raw[1] <= len(text)
    ):
        return {**result, "status": "invalid", "reason": "invalid_unicode_range"}
    start, end = raw
    return {
        **result,
        "status": "valid",
        "start": start,
        "end": end,
        "quote": text[start:end],
        "offset_unit": "unicode_code_point",
    }


def association(source, response, start, end, reference_ordinals=()):
    anchor = source.get("answer_anchor") or {}
    if anchor.get("status") == "valid":
        expected = freeze_anchor(
            [anchor.get("start"), anchor.get("end")], response["text"]
        )
        if expected != anchor:
            return {"eligible": False, "method": "invalid_frozen_anchor", "priority": 2}
        matches = start < anchor["end"] and anchor["start"] < end
        return {
            "eligible": matches,
            "method": "answer_anchor" if matches else "anchor_outside_claim",
            "priority": 0,
            "anchor": anchor,
        }
    if reference_ordinals:
        matches = source.get("ordinal") in reference_ordinals
        return {
            "eligible": matches,
            "method": "answer_reference_marker"
            if matches
            else "different_reference_marker",
            "priority": 0,
        }
    return {
        "eligible": True,
        "method": "same_answer_fallback",
        "priority": 1,
        "reason": anchor.get("reason", "not_captured"),
        "inferred_association": True,
    }


def snapshot_version(snapshot):
    return {
        "id": snapshot.id,
        "body_hash": snapshot.body_hash,
        "representation_hash": snapshot.representation_hash,
        "extractor_version": snapshot.extractor_version,
        "integrity_status": snapshot.integrity_status,
        "article_completeness": snapshot.completeness,
    }


def snapshot_matches(source, snapshot):
    return bool(snapshot) and (
        not source.get("snapshot_version")
        or source["snapshot_version"] == snapshot_version(snapshot)
    )
