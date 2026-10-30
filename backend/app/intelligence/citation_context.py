"""Frozen answer anchors select observations, never manufacture supporting text."""

from app.intelligence.blob import digest

ANCHOR_VERSION = "answer-anchor-v1"
EVIDENCE_POLICY = "anchored-observation-budget-v2"
PAIRS_PER_CLAIM = 12


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


def association(source, response, start, end):
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
