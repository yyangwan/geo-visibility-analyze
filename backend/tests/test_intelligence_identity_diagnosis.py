from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.intelligence.diagnosis import platform_diagnoses
from app.intelligence.identity import (
    IdentityDecision,
    decide_identity,
    identity_snapshot,
)
from app.intelligence.ingestion import prepare_run
from app.intelligence.models import (
    ContentSegment,
    EntityMention,
    EntityResolutionEvent,
    IntelligenceEntity,
)
from tests.test_intelligence_foundation import fixture_audit


@pytest.mark.asyncio
async def test_identity_decisions_are_versioned_idempotent_and_do_not_rewrite_runs(
    db_session,
):
    audit = await fixture_audit(db_session)
    run = await prepare_run(db_session, "p1", audit.id)
    entity = IntelligenceEntity(
        project_id="p1", name="云衡", kind="brand", identity_hash="a" * 64
    )
    segment = ContentSegment(
        project_id="p1",
        owner_kind="answer",
        owner_id="1",
        ordinal=0,
        start_offset=0,
        end_offset=2,
        text="云衡",
        segment_hash="b" * 64,
        extractor_version="test",
    )
    db_session.add_all([entity, segment])
    await db_session.flush()
    db_session.add(
        EntityMention(
            project_id="p1",
            run_id=run.id,
            entity_id=entity.id,
            segment_id=segment.id,
            start_offset=0,
            end_offset=2,
            quote="云衡",
        )
    )
    await db_session.commit()
    original = deepcopy(run.manifest)
    data = IdentityDecision(
        entityId=entity.id,
        decision="confirmed",
        canonicalName="云衡公司",
        reason="按原文上下文确认公司名称",
        expectedVersion=0,
        idempotencyKey="identity-key-123",
    )
    first = await decide_identity(db_session, run, data, "reviewer-1")
    await db_session.commit()
    replay = await decide_identity(db_session, run, data, "reviewer-1")
    assert replay.id == first.id
    assert run.manifest == original
    snapshot = await identity_snapshot(db_session, "p1")
    assert snapshot[entity.id]["version"] == 1
    assert await identity_snapshot(db_session, "p2") == {}
    new_run = await prepare_run(db_session, "p1", audit.id)
    assert new_run.id != run.id
    assert new_run.manifest["entity_resolutions"][entity.id]["decision"] == "confirmed"
    with pytest.raises(HTTPException) as stale:
        await decide_identity(
            db_session,
            run,
            data.model_copy(
                update={"idempotency_key": "other-key-123", "decision": "excluded"}
            ),
            "reviewer-2",
        )
    assert stale.value.status_code == 409
    with pytest.raises(HTTPException) as conflict:
        await decide_identity(
            db_session,
            run,
            data.model_copy(update={"reason": "不同的确认理由内容"}),
            "reviewer-1",
        )
    assert conflict.value.status_code == 409
    assert len((await db_session.scalars(select(EntityResolutionEvent))).all()) == 1


def test_platform_diagnoses_do_not_borrow_evidence_from_other_platforms():
    run = SimpleNamespace(
        id="run1",
        manifest={
            "responses": [
                {
                    "id": "r1",
                    "platform": "qwen",
                    "prompt_hash": "p",
                    "prompt": "哪个好",
                    "conditions": {},
                    "answer_status": "complete",
                },
                {
                    "id": "r2",
                    "platform": "deepseek",
                    "prompt_hash": "p",
                    "prompt": "哪个好",
                    "conditions": {},
                    "answer_status": "complete",
                },
            ],
            "sources": [
                {
                    "id": "s1",
                    "response_id": "r1",
                    "platform": "qwen",
                    "kind": "answer_reference",
                    "fetch_status": "captured",
                    "url": "https://example.com/a",
                    "url_resolution": "exact",
                },
                {
                    "id": "s2",
                    "response_id": "r2",
                    "platform": "deepseek",
                    "kind": "answer_reference",
                    "fetch_status": "captured",
                    "url": "https://example.com/b",
                    "url_resolution": "exact",
                },
            ],
        },
    )
    summary = {
        "entities": [{"id": "e", "identity_status": "provisional"}],
        "mentions": [
            {
                "id": "m2",
                "entity_id": "e",
                "entity_name": "新品牌",
                "owner_kind": "answer",
                "owner_id": "r2",
                "sentiment": "negative",
            }
        ],
        "claim_evidence": [
            {
                "id": "c2",
                "citation_id": "s2",
                "platform": "deepseek",
                "relation": "contradicts",
            }
        ],
    }
    rows = platform_diagnoses(run, summary)
    assert rows and all(row["platform"] == "deepseek" for row in rows)
    assert all(
        row["response_ids"] == ["r2"] and row["source_ids"] == ["s2"] for row in rows
    )
    assert {row["kind"] for row in rows} == {
        "identity_review",
        "negative_observation",
        "claim_review",
    }
    assert all(row["action"]["status"] == "proposed" for row in rows)


def test_unknown_capture_only_proposes_collection_not_brand_absence():
    run = SimpleNamespace(
        id="run1",
        manifest={
            "responses": [
                {
                    "id": "r1",
                    "platform": "qwen",
                    "prompt_hash": "p",
                    "prompt": "哪个好",
                    "conditions": {},
                }
            ],
            "sources": [],
        },
    )
    rows = platform_diagnoses(
        run, {"entities": [], "mentions": [], "claim_evidence": []}
    )
    assert [row["kind"] for row in rows] == ["capture_gap"]
    assert "不能" in rows[0]["finding"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "project,role,actor,expected",
    [
        ("p1", "owner", "reviewer", 202),
        ("p1", "viewer", "reviewer", 403),
        ("p2", "owner", "reviewer", 404),
        ("p1", "owner", "", 403),
    ],
)
async def test_identity_api_scope_and_reviewer_provenance(
    client, db_session, monkeypatch, project, role, actor, expected
):
    from app.api.auth import get_current_user
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "intelligence_enabled", True)
    audit = await fixture_audit(db_session)
    run = await prepare_run(db_session, "p1", audit.id)
    run.status = "partial"
    entity = IntelligenceEntity(
        project_id="p1", name="云衡", kind="brand", identity_hash="a" * 64
    )
    segment = ContentSegment(
        project_id="p1",
        owner_kind="answer",
        owner_id="1",
        ordinal=0,
        start_offset=0,
        end_offset=2,
        text="云衡",
        segment_hash="b" * 64,
        extractor_version="test",
    )
    db_session.add_all([entity, segment])
    await db_session.flush()
    db_session.add(
        EntityMention(
            project_id="p1",
            run_id=run.id,
            entity_id=entity.id,
            segment_id=segment.id,
            start_offset=0,
            end_offset=2,
            quote="云衡",
        )
    )
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: {
        "scope": "project",
        "pid": project,
        "role": role,
        "sub": actor,
    }
    try:
        result = await client.post(
            f"/api/intelligence/runs/{run.id}/identity-decisions",
            json={
                "entityId": entity.id,
                "decision": "confirmed",
                "canonicalName": "云衡公司",
                "reason": "原文说明企业名称",
                "expectedVersion": 0,
                "idempotencyKey": "identity-key-123",
            },
        )
        assert result.status_code == expected
        event = await db_session.scalar(select(EntityResolutionEvent))
        assert (event.actor_id == actor) if expected == 202 else event is None
    finally:
        app.dependency_overrides.pop(get_current_user, None)
