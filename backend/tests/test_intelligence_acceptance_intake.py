import json
from copy import deepcopy

import pytest
from sqlalchemy import select

from app.intelligence.acceptance_intake import (
    IntakeInvalid,
    collect,
    fenced,
    project_response,
    publish,
    verify,
)
from app.intelligence.blob import digest
from app.intelligence.models import AnalysisJob
from app.models.models import PlatformResponseRecord, Prompt
from tests.conftest import test_engine
from tests.test_intelligence_foundation import fixture_audit


def record(**updates):
    return {
        "id": 10,
        "audit_id": 37,
        "prompt_id": 5,
        "platform": "qwen",
        "response_text": "青禾与未登记的远岚。",
        "raw_response": {
            "sources": [
                {
                    "title": "来源",
                    "url": "https://example.com/article",
                    "snippet": "原始摘要",
                    "kind": "answer_reference",
                    "authorization": "must-not-copy",
                }
            ],
            "authorization": "private-api-key",
        },
        "error": None,
        **updates,
    }


def sample(**updates):
    return project_response(
        record(**updates), {"id": 37, "project_id": "p1"}, "有哪些方案？"
    )


def manifest():
    return {
        "schema": "citation-acceptance-intake-1",
        "dataset_id": "dataset",
        "project_id": "p1",
        "audit_ids": [37],
        "schema_at_expected_head": False,
        "analysis_tables_present": 0,
        "analysis_tables_expected": 40,
        "runtime_configuration": {},
        "new_analysis_executed": False,
        "independent_reviews_completed": False,
        "business_closed_loop_verified": False,
        "release_eligible": False,
    }


def v2():
    answer = "青禾与未登记的远岚。"
    return {
        "contractVersion": 2,
        "taskId": "task-1",
        "platform": "qwen",
        "surface": "app",
        "observedAt": "2026-10-06T10:24:24Z",
        "answer": {
            "text": answer,
            "status": "complete",
            "contentHash": digest(answer.encode()),
        },
        "conditions": {"searchMode": "unknown"},
        "sources": [
            {
                "ordinal": 1,
                "kind": "answer_reference",
                "title": "标题",
                "url": "https://example.com/article",
                "snippet": "摘要",
                "urlResolution": "exact",
            }
        ],
        "sourcePanel": {"status": "complete", "expectedCount": 1, "capturedCount": 1},
        "adapter": {"name": "fixture", "version": "2"},
    }


def test_legacy_facts_stay_unknown_and_sensitive_payload_fields_are_not_copied():
    row = sample()
    assert row["answer_status"] == "unknown" and not row["capture_v2_verified"]
    assert row["cards"][0]["declared_kind"] == "answer_reference"
    assert not row["cards"][0]["v2_answer_reference_observation"]
    assert row["conditions"]["searchMode"] == "unknown"
    assert "private-api-key" not in json.dumps(
        row
    ) and "must-not-copy" not in json.dumps(row)
    assert row["answer_text"] == "青禾与未登记的远岚。"


@pytest.mark.parametrize(
    "damage", [None, "platform", "hash", "count", "missing_timezone"]
)
def test_only_matching_valid_v2_retains_declared_complete_status(damage):
    capture = v2()
    if damage == "platform":
        capture["platform"] = "deepseek"
    elif damage == "hash":
        capture["answer"]["contentHash"] = "a" * 64
    elif damage == "count":
        capture["sourcePanel"]["capturedCount"] = 0
    elif damage == "missing_timezone":
        capture["observedAt"] = "2026-10-06T10:24:24"
    row = sample(raw_response={"captureV2": capture})
    assert row["capture_v2_verified"] == (damage is None)
    assert row["answer_status"] == ("complete" if damage is None else "unknown")
    if damage is None:
        assert row["cards"][0]["v2_answer_reference_observation"]
        assert sample(raw_response=capture)["capture_v2_verified"]


def test_bad_card_values_and_empty_answers_are_visible_not_negative_brand_results():
    row = sample(
        response_text=None,
        raw_response={"sources": ["unknown-card", {"kind": {}, "urlResolution": []}]},
    )
    assert row["answer_status"] == "missing" and len(row["cards"]) == 2
    assert row["cards"][0]["parse_status"] == "unreadable"
    assert row["cards"][1]["declared_kind"] == "unknown"
    assert (
        sample(raw_response="invalid-json-a")["raw_payload_hash"]
        != sample(raw_response="invalid-json-b")["raw_payload_hash"]
    )


@pytest.mark.asyncio
async def test_recent_project_scope_is_read_only_and_oldest_audit_is_refused(
    db_session,
):
    audits = [await fixture_audit(db_session) for _ in range(3)]
    before = (await db_session.scalars(select(AnalysisJob.id))).all()
    async with test_engine.connect() as connection:
        metadata, rows = await collect(
            connection, [audits[-1].id, audits[-2].id], project_id="p1"
        )
        assert len(rows) == 2 and metadata["release_eligible"] is False
        assert all(
            row["model_analysis_status"] == "not_executed_by_intake" for row in rows
        )
        with pytest.raises(IntakeInvalid, match="latest two"):
            await collect(connection, [audits[0].id])
        with pytest.raises(IntakeInvalid, match="Project scope"):
            await collect(connection, [audits[-1].id], project_id="other")
    assert before == (await db_session.scalars(select(AnalysisJob.id))).all()
    assert not db_session.dirty and not db_session.new


@pytest.mark.asyncio
async def test_cross_project_audits_and_prompt_joins_are_not_followed(db_session):
    first = await fixture_audit(db_session)
    second = await fixture_audit(db_session, project="p2")
    foreign_prompt = await db_session.scalar(
        select(Prompt.id).where(Prompt.project_id == "p2")
    )
    response = await db_session.scalar(
        select(PlatformResponseRecord).where(
            PlatformResponseRecord.audit_id == first.id
        )
    )
    response.prompt_id = foreign_prompt
    await db_session.commit()
    async with test_engine.connect() as conn:
        with pytest.raises(IntakeInvalid, match="crosses projects"):
            await collect(conn, [first.id, second.id])
        _, rows = await collect(conn, [first.id])
        assert rows[0]["prompt_text"] is None and rows[0]["prompt_hash"] is None


@pytest.mark.parametrize("ids", [[], [1, 1], [1, 2, 3], [True], [-1]])
@pytest.mark.asyncio
async def test_selection_requires_one_or_two_distinct_positive_ids(ids):
    async with test_engine.connect() as conn:
        with pytest.raises(IntakeInvalid):
            await collect(conn, ids)


def test_private_packet_has_blinded_preparation_templates_and_safe_literal_content(
    tmp_path,
):
    row = sample(
        response_text="```\n<script>do not execute</script>\n````\n![remote](https://example.com/image)"
    )
    target = tmp_path / "packet"
    result = publish(manifest(), [row], target)
    assert (
        result["status"] == "prepared_only"
        and verify(target)["release_eligible"] is False
    )
    template = json.loads((target / "review-a.template.jsonl").read_text())
    assert (
        template["status"] == "pending"
        and template["entities"] is None
        and template["preparation_only"]
    )
    rendered = (target / "samples" / f"{row['sample_id']}.md").read_text(
        encoding="utf-8"
    )
    assert fenced(row["answer_text"]) in rendered and "未读取或核验文章全文" in rendered
    assert fenced(row["answer_text"]).startswith("`````text")
    with pytest.raises(FileExistsError):
        publish(manifest(), [row], target)


@pytest.mark.parametrize(
    "damage", ["inputs", "markdown", "scope", "execution", "incomplete"]
)
def test_tampered_inputs_review_pages_and_false_completion_flags_fail(tmp_path, damage):
    row = sample()
    target = tmp_path / "packet"
    publish(manifest(), [row], target)
    path = target / "manifest.json"
    metadata = json.loads(path.read_text())
    if damage == "inputs":
        (target / "captured-inputs.jsonl").write_text("{}")
    elif damage == "markdown":
        (target / "samples" / f"{row['sample_id']}.md").write_text(
            "wrong original text"
        )
    elif damage == "scope":
        metadata["project_id"] = "other"
    elif damage == "execution":
        metadata["new_analysis_executed"] = True
    elif damage == "incomplete":
        (target / "INCOMPLETE").write_text("not finished")
    path.write_text(json.dumps(metadata))
    with pytest.raises(IntakeInvalid):
        verify(target)


def test_invalid_sample_paths_are_rejected_before_any_file_is_written(tmp_path):
    row = deepcopy(sample())
    row["sample_id"] = "../../escaped"
    target = tmp_path / "packet"
    with pytest.raises(IntakeInvalid):
        publish(manifest(), [row], target)
    assert not target.exists()
