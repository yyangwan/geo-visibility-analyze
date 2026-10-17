from copy import deepcopy

import pytest
from fastapi import HTTPException

from app.intelligence.aliases import (
    AliasRequest,
    alias_snapshot,
    decide_alias,
    resolved_entity,
)
from app.intelligence.ingestion import prepare_run
from app.intelligence.models import (
    ContentSegment,
    EntityMention,
    EntityResolutionEvent,
    IntelligenceEntity,
)
from app.intelligence.pipeline import build_summary
from app.intelligence.semantics import stable_hash
from tests.test_intelligence_foundation import fixture_audit


async def alias_fixture(db):
    audit = await fixture_audit(db, text="云衡与Yunheng")
    run = await prepare_run(db, "p1", audit.id)
    run.status = "partial"
    target = IntelligenceEntity(
        project_id="p1", name="云衡", kind="brand", identity_hash="a" * 64
    )
    source = IntelligenceEntity(
        project_id="p1", name="Yunheng", kind="brand", identity_hash="b" * 64
    )
    segment = ContentSegment(
        project_id="p1",
        owner_kind="answer",
        owner_id=run.manifest["responses"][0]["id"],
        ordinal=0,
        start_offset=0,
        end_offset=10,
        text="云衡与Yunheng",
        segment_hash="c" * 64,
        extractor_version="test",
    )
    db.add_all([target, source, segment])
    await db.flush()
    for entity, start, end in ((target, 0, 2), (source, 3, 10)):
        db.add(
            EntityMention(
                project_id="p1",
                run_id=run.id,
                entity_id=entity.id,
                segment_id=segment.id,
                start_offset=start,
                end_offset=end,
                quote=segment.text[start:end],
            )
        )
    db.add(
        EntityResolutionEvent(
            project_id="p1",
            entity_id=target.id,
            run_id=run.id,
            version=1,
            decision="confirmed",
            canonical_name="云衡",
            reason="核对主体与原文后确认",
            actor_id="reviewer",
            idempotency_key="confirm-target",
            request_hash="d" * 64,
        )
    )
    await db.commit()
    data = AliasRequest(
        sourceEntityId=source.id,
        targetEntityId=target.id,
        decision="merge",
        reason="原文与企业资料确认中英文为同一主体",
        expectedVersion=0,
        idempotencyKey="alias-key-123",
    )
    return run, target, source, segment, data


@pytest.mark.asyncio
async def test_alias_events_replay_freeze_and_unlink_without_rewriting_raw_mentions(
    db_session,
):
    run, target, source, _, data = await alias_fixture(db_session)
    original = deepcopy(run.manifest)
    event = await decide_alias(db_session, run, data, "reviewer")
    await db_session.commit()
    assert (await decide_alias(db_session, run, data, "reviewer")).id == event.id
    assert run.manifest == original
    snapshot = await alias_snapshot(db_session, "p1")
    manifest = {
        "entity_aliases": snapshot,
        "entity_resolutions": {target.id: {"decision": "confirmed"}},
    }
    assert resolved_entity(source.id, manifest) == target.id
    event2 = await decide_alias(
        db_session,
        run,
        AliasRequest(
            sourceEntityId=source.id,
            decision="unlink",
            reason="重新核对名称关系后解除归并",
            expectedVersion=1,
            idempotencyKey="unlink-key-123",
        ),
        "reviewer",
    )
    await db_session.commit()
    assert event2.version == 2
    manifest["entity_aliases"] = await alias_snapshot(db_session, "p1")
    assert resolved_entity(source.id, manifest) == source.id
    assert await alias_snapshot(db_session, "p2") == {}


@pytest.mark.asyncio
async def test_merged_aliases_count_distinct_answers_and_preserve_original_trace(
    db_session,
):
    run, target, source, segment, data = await alias_fixture(db_session)
    await decide_alias(db_session, run, data, "reviewer")
    await db_session.commit()
    refreshed = await prepare_run(db_session, "p1", run.audit_id)
    refreshed.status = "partial"
    for entity, start, end in ((target, 0, 2), (source, 3, 10)):
        db_session.add(
            EntityMention(
                project_id="p1",
                run_id=refreshed.id,
                entity_id=entity.id,
                segment_id=segment.id,
                start_offset=start,
                end_offset=end,
                quote=segment.text[start:end],
            )
        )
    await db_session.flush()
    _, summary = await build_summary(db_session, refreshed)
    assert len(summary["entities"]) == 1
    entity = summary["entities"][0]
    assert (
        entity["id"] == target.id
        and entity["response_count"] == 1
        and entity["platform_response_counts"] == {"qwen": 1}
    )
    assert set(entity["observed_names"]) == {"云衡", "Yunheng"}
    assert {item["observed_entity_id"] for item in summary["mentions"]} == {
        target.id,
        source.id,
    }
    assert {item["entity_id"] for item in summary["mentions"]} == {target.id}
    assert refreshed.manifest["entity_aliases"][source.id]["version"] == 1
    refreshed.manifest = {
        **refreshed.manifest,
        "brands": [{"name": "云衡"}, {"name": "Yunheng", "is_competitor": True}],
    }
    _, conflict = await build_summary(db_session, refreshed)
    assert (
        conflict["entities"][0]["role_conflict"]
        and conflict["entities"][0]["role"] == "unregistered"
    )


@pytest.mark.asyncio
async def test_alias_cycle_unconfirmed_cross_kind_and_stale_version_are_rejected(
    db_session,
):
    run, target, source, _, data = await alias_fixture(db_session)
    with pytest.raises(HTTPException) as error:
        await decide_alias(
            db_session,
            run,
            data.model_copy(update={"target_entity_id": source.id}),
            "reviewer",
        )
    assert error.value.status_code == 422
    source.kind = "product"
    with pytest.raises(HTTPException) as error:
        await decide_alias(db_session, run, data, "reviewer")
    assert error.value.status_code == 422
    source.kind = "brand"
    await decide_alias(db_session, run, data, "reviewer")
    await db_session.commit()
    with pytest.raises(HTTPException) as error:
        await decide_alias(
            db_session,
            run,
            data.model_copy(update={"idempotency_key": "stale-key-123"}),
            "reviewer",
        )
    assert error.value.status_code == 409
    # A target cannot redirect back into its own alias subtree.
    db_session.add(
        EntityResolutionEvent(
            project_id="p1",
            entity_id=source.id,
            run_id=run.id,
            version=1,
            decision="confirmed",
            canonical_name=source.name,
            reason="单独核对候选主体身份",
            actor_id="reviewer",
            idempotency_key="confirm-source",
            request_hash=stable_hash("source"),
        )
    )
    await db_session.flush()
    with pytest.raises(HTTPException):
        await decide_alias(
            db_session,
            run,
            AliasRequest(
                sourceEntityId=target.id,
                targetEntityId=source.id,
                decision="merge",
                reason="测试禁止循环主体归并",
                expectedVersion=0,
                idempotencyKey="cycle-key-123",
            ),
            "reviewer",
        )


def test_excluded_canonical_identity_does_not_silently_accept_aliases():
    manifest = {
        "entity_aliases": {
            "source": {"decision": "merge", "target_entity_id": "target"}
        },
        "entity_resolutions": {"target": {"decision": "excluded"}},
    }
    assert resolved_entity("source", manifest) == "source"


@pytest.mark.asyncio
async def test_mapping_changes_invalidate_existing_facts(db_session):
    from app.intelligence.reviews import approve_fact, fact_view, review_claim
    from tests.test_intelligence_reviews import (
        fact_request,
        fixture_evidence,
        review_request,
    )

    run, evidence, source, segment = await fixture_evidence(db_session)
    review = await review_claim(
        db_session, run, evidence.id, review_request(segment.text), "reviewer"
    )
    fact = await approve_fact(db_session, run, fact_request(review, source), "reviewer")
    target = IntelligenceEntity(
        project_id="p1", name="云衡正式名", kind="brand", identity_hash="f" * 64
    )
    db_session.add(target)
    await db_session.flush()
    db_session.add(
        EntityResolutionEvent(
            project_id="p1",
            entity_id=target.id,
            run_id=run.id,
            version=1,
            decision="confirmed",
            canonical_name=target.name,
            reason="核对正式主体后确认",
            actor_id="reviewer",
            idempotency_key="fact-alias-target",
            request_hash="a" * 64,
        )
    )
    db_session.add(
        EntityMention(
            project_id="p1",
            run_id=run.id,
            entity_id=source.id,
            segment_id=segment.id,
            start_offset=0,
            end_offset=2,
            quote="云衡",
        )
    )
    await db_session.flush()
    await decide_alias(
        db_session,
        run,
        AliasRequest(
            sourceEntityId=source.id,
            targetEntityId=target.id,
            decision="merge",
            reason="正式名称与原简称对应",
            expectedVersion=0,
            idempotencyKey="fact-alias-key",
        ),
        "reviewer",
    )
    assert (await fact_view(db_session, fact))["status"] == "needs_review"


@pytest.mark.asyncio
async def test_alias_api_project_and_viewer_boundaries(client, db_session, monkeypatch):
    from app.api.auth import get_current_user
    from app.config import settings
    from app.main import app

    run, _, _, _, data = await alias_fixture(db_session)
    monkeypatch.setattr(settings, "intelligence_enabled", True)
    app.dependency_overrides[get_current_user] = lambda: {
        "scope": "project",
        "pid": "p1",
        "role": "viewer",
        "sub": "viewer",
    }
    try:
        assert (
            await client.post(
                f"/api/intelligence/runs/{run.id}/aliases",
                json=data.model_dump(by_alias=True),
            )
        ).status_code == 403
        app.dependency_overrides[get_current_user] = lambda: {
            "scope": "project",
            "pid": "p2",
            "role": "owner",
            "sub": "other",
        }
        assert (
            await client.post(
                f"/api/intelligence/runs/{run.id}/aliases",
                json=data.model_dump(by_alias=True),
            )
        ).status_code == 404
        assert (
            await client.get("/api/intelligence/projects/p1/aliases")
        ).status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)
