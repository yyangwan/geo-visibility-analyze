"""Owner-free information candidates and immutable, evidence-bound projections."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

VERSION = "information-units-v1"


class InformationUnit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal[
        "feature",
        "parameter",
        "price",
        "audience",
        "use_case",
        "implementation",
        "case_study",
        "test_method",
        "risk",
        "faq",
        "comparison",
        "selection_reason",
    ]
    subject: str | None = Field(default=None, min_length=1, max_length=200)
    value: str | None = Field(default=None, min_length=1, max_length=500)
    unit: str | None = Field(default=None, min_length=1, max_length=100)
    conditions: list[str] | None = Field(default=None, max_length=15)
    effective_time: str | None = Field(default=None, min_length=1, max_length=100)
    quote: str = Field(min_length=1, max_length=2000)
    start: StrictInt
    end: StrictInt


def grounded_fields(item):
    values = [item.get(key) for key in ("subject", "value", "unit", "effective_time")]
    values += item.get("conditions") or []
    return all(
        isinstance(value, str) and value and value in item["quote"]
        for value in values
        if value is not None
    )


def local_units(result):
    from app.intelligence.semantics import stable_hash

    rows = [
        {
            **item,
            "id": stable_hash([VERSION, item]),
            "review_state": "unreviewed",
            "conditions_status": "unknown"
            if item.get("conditions") is None
            else "observed_text",
            "assertion_scope": "observed_text_only",
        }
        for item in result.get("information_units") or []
    ]
    return list({item["id"]: item for item in rows}.values())


def project_units(applications):
    from app.intelligence.semantics import stable_hash

    rows = []
    expected, processed, rejected_units = 0, 0, 0
    for application, segment, cache in applications:
        expected += 1
        result = cache.result if cache else {}
        if isinstance(result.get("information_units"), list):
            processed += application.status in ("completed", "partial")
        rejected_units += sum(item.get("path", [None])[0] == "information_units"
                              for item in result.get("rejected_items", []))
        for item in local_units(result):
            rows.append(
                {
                    **item,
                    "id": stable_hash([VERSION, segment.id, item["id"]]),
                    "local_id": item["id"],
                    "segment_id": segment.id,
                    "owner_kind": segment.owner_kind,
                    "owner_id": segment.owner_id,
                    "start": segment.start_offset + item["start"],
                    "end": segment.start_offset + item["end"],
                    "analysis_status": application.status,
                }
            )
    return {
        "version": VERSION,
        "segments": expected,
        "processed_segments": processed,
        "rejected_units": rejected_units,
        "unknown_condition_units": sum(item["conditions"] is None for item in rows),
        "status": "processed" if expected and processed == expected and not rejected_units else "partial",
        "observations": rows,
    }
