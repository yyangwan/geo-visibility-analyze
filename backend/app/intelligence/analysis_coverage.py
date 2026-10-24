"""Separate text processing from local span defects and identity uncertainty."""

from collections import defaultdict


def segment_processed(application, cache):
    if not cache or not isinstance(cache.result, dict):
        return False
    result = cache.result
    if not isinstance(result.get("brand_signals"), list):
        return False
    if application.status == "completed":
        return not result.get("rejected_spans", 0)
    defects = result.get("rejected_items", [])
    count = result.get("rejected_spans")
    return (
        application.status == "partial"
        and type(count) is int
        and count > 0
        and isinstance(defects, list)
        and len(defects) == count
        and all(
            isinstance(row, dict) and isinstance(row.get("candidate"), dict)
            for row in defects
        )
    )


def answer_coverage(manifest, applications):
    grouped = defaultdict(list)
    for application, segment, cache in applications:
        if segment.owner_kind == "answer":
            grouped[segment.owner_id].append((application, segment, cache))
    proofs = {}
    for response in manifest["responses"]:
        rows = sorted(grouped[response["id"]], key=lambda row: row[1].start_offset)
        cursor, rejected = 0, []
        processed = (
            bool(rows)
            and not response["error"]
            and response.get("answer_status", "complete") == "complete"
        )
        for application, segment, cache in rows:
            result = cache.result if cache and isinstance(cache.result, dict) else {}
            defects = result.get("rejected_items", [])
            processed = (
                processed
                and segment.start_offset == cursor
                and segment.end_offset > cursor
                and segment_processed(application, cache)
            )
            if hasattr(segment, "text"):
                processed = (
                    processed
                    and response["text"][segment.start_offset : segment.end_offset]
                    == segment.text
                )
            if isinstance(defects, list):
                rejected.extend(
                    row
                    for row in defects
                    if isinstance(row, dict) and isinstance(row.get("candidate"), dict)
                )
            cursor = segment.end_offset
        proofs[response["id"]] = {
            "input_processed": bool(processed and cursor == len(response["text"])),
            "rejected_items": rejected,
        }
    return proofs


def related_span_defects(proof, names):
    aliases = {name.casefold() for name in names if isinstance(name, str) and name}
    for item in proof.get("rejected_items", []):
        candidate = item["candidate"]
        subject = candidate.get("name") or candidate.get("subject")
        if not isinstance(subject, str) or not subject:
            return True  # An unlocalizable failure cannot prove absence.
        values = [subject, candidate.get("quote", "")]
        if any(
            alias in value.casefold()
            for alias in aliases
            for value in values
            if isinstance(value, str)
        ):
            return True
    return False
