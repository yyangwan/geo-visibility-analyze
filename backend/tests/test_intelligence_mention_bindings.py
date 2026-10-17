from copy import deepcopy

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.intelligence.mention_bindings import (
    MentionBindingRequest,
    attributed_entity,
    binding_key,
    binding_snapshot,
    decide_binding,
)
from app.intelligence.models import EntityMention
from tests.test_intelligence_aliases import alias_fixture


@pytest.mark.asyncio
async def test_occurrence_override_is_scoped_replayable_and_resettable(db_session):
    run, target, source, segment, _ = await alias_fixture(db_session)
    mention = await db_session.scalar(
        select(EntityMention).where(
            EntityMention.run_id == run.id, EntityMention.entity_id == source.id
        )
    )
    original = deepcopy(run.manifest)
    data = MentionBindingRequest(
        mentionId=mention.id,
        targetEntityId=target.id,
        decision="assign",
        reason="根据本片段上下文确认目标主体",
        expectedVersion=0,
        idempotencyKey="mention-bind-123",
    )
    event = await decide_binding(db_session, run, data, "reviewer")
    await db_session.commit()
    assert (await decide_binding(db_session, run, data, "reviewer")).id == event.id
    assert run.manifest == original
    assert mention.entity_id == source.id
    manifest = {
        "mention_bindings": await binding_snapshot(db_session, "p1"),
        "entity_resolutions": {target.id: {"decision": "confirmed"}},
    }
    assert attributed_entity(mention, segment, manifest, source.id) == target.id
    another = EntityMention(
        entity_id=source.id, start_offset=0, end_offset=2, quote="云衡"
    )
    assert binding_key(another, segment) != binding_key(mention, segment)
    assert attributed_entity(another, segment, manifest, source.id) == source.id
    assert await binding_snapshot(db_session, "p2") == {}
    reset = MentionBindingRequest(
        mentionId=mention.id,
        decision="reset",
        reason="重新核对后恢复自动归属规则",
        expectedVersion=1,
        idempotencyKey="mention-reset-123",
    )
    await decide_binding(db_session, run, reset, "reviewer")
    await db_session.commit()
    manifest["mention_bindings"] = await binding_snapshot(db_session, "p1")
    assert attributed_entity(mention, segment, manifest, source.id) == source.id


@pytest.mark.asyncio
async def test_occurrence_rejects_stale_unconfirmed_and_tampered_text(db_session):
    run, target, source, _segment, _ = await alias_fixture(db_session)
    mention = await db_session.scalar(
        select(EntityMention).where(
            EntityMention.run_id == run.id, EntityMention.entity_id == source.id
        )
    )
    data = MentionBindingRequest(
        mentionId=mention.id,
        targetEntityId=target.id,
        decision="assign",
        reason="根据本片段上下文确认目标主体",
        expectedVersion=2,
        idempotencyKey="mention-bind-123",
    )
    with pytest.raises(HTTPException) as error:
        await decide_binding(db_session, run, data, "reviewer")
    assert error.value.status_code == 409
    data.expected_version = 0
    data.target_entity_id = source.id
    with pytest.raises(HTTPException) as error:
        await decide_binding(db_session, run, data, "reviewer")
    assert error.value.status_code == 422
    data.target_entity_id = target.id
    mention.quote = "changed"
    await db_session.flush()
    with pytest.raises(HTTPException) as error:
        await decide_binding(db_session, run, data, "reviewer")
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_refreshed_summary_uses_occurrence_without_assigning_other_mentions(
    db_session,
):
    from app.intelligence.ingestion import prepare_run
    from app.intelligence.pipeline import build_summary

    run, target, source, segment, _ = await alias_fixture(db_session)
    mention = await db_session.scalar(
        select(EntityMention).where(
            EntityMention.run_id == run.id, EntityMention.entity_id == source.id
        )
    )
    data = MentionBindingRequest(
        mentionId=mention.id,
        targetEntityId=target.id,
        decision="assign",
        reason="根据本片段上下文确认目标主体",
        expectedVersion=0,
        idempotencyKey="mention-summary-123",
    )
    await decide_binding(db_session, run, data, "reviewer")
    await db_session.commit()
    refreshed = await prepare_run(db_session, "p1", run.audit_id)
    copied = EntityMention(
        project_id="p1",
        run_id=refreshed.id,
        entity_id=source.id,
        segment_id=segment.id,
        start_offset=mention.start_offset,
        end_offset=mention.end_offset,
        quote=mention.quote,
    )
    db_session.add(copied)
    await db_session.flush()
    _, summary = await build_summary(db_session, refreshed)
    assert summary["entities"][0]["id"] == target.id
    assert summary["entities"][0]["response_count"] == 1
    assert summary["mentions"][0]["observed_entity_id"] == source.id
    assert summary["mentions"][0]["attribution"]["version"] == 1
    assert run.manifest.get("mention_bindings", {}) == {}
    forged = data.model_copy(
        update={"mention_id": "f" * 32, "idempotency_key": "missing-mention-123"}
    )
    with pytest.raises(HTTPException) as error:
        await decide_binding(db_session, run, forged, "reviewer")
    assert error.value.status_code == 404
    segment.owner_id = "outside-manifest"
    await db_session.flush()
    fresh_key = data.model_copy(
        update={"idempotency_key": "outside-owner-123", "expected_version": 1}
    )
    with pytest.raises(HTTPException) as error:
        await decide_binding(db_session, run, fresh_key, "reviewer")
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_occurrence_api_project_and_viewer_boundaries(
    client, db_session, monkeypatch
):
    from app.api.auth import get_current_user
    from app.config import settings
    from app.main import app

    run, target, source, _, _ = await alias_fixture(db_session)
    mention = await db_session.scalar(
        select(EntityMention).where(
            EntityMention.run_id == run.id, EntityMention.entity_id == source.id
        )
    )
    data = MentionBindingRequest(
        mentionId=mention.id,
        targetEntityId=target.id,
        decision="assign",
        reason="根据本片段上下文确认目标主体",
        expectedVersion=0,
        idempotencyKey="mention-api-123",
    )
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
                f"/api/intelligence/runs/{run.id}/mention-bindings",
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
                f"/api/intelligence/runs/{run.id}/mention-bindings",
                json=data.model_dump(by_alias=True),
            )
        ).status_code == 404
        assert (
            await client.get("/api/intelligence/projects/p1/mention-bindings")
        ).status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_split_creates_independent_identity_and_preserves_lineage(db_session):
    from app.intelligence.identity import (
        IdentityDecision,
        decide_identity,
        identity_snapshot,
    )
    from app.intelligence.ingestion import prepare_run
    from app.intelligence.mention_bindings import split_snapshot
    from app.intelligence.models import IntelligenceEntity
    from app.intelligence.pipeline import build_summary

    run, _, source, segment, _ = await alias_fixture(db_session)
    mention = await db_session.scalar(
        select(EntityMention).where(
            EntityMention.run_id == run.id, EntityMention.entity_id == source.id
        )
    )
    data = MentionBindingRequest(
        mentionId=mention.id,
        newEntityName="Yunheng（工业设备品牌）",
        decision="split",
        reason="本片段谈及工业设备并非原候选公司",
        expectedVersion=0,
        idempotencyKey="split-subject-123",
    )
    event = await decide_binding(db_session, run, data, "reviewer")
    await db_session.commit()
    replay = await decide_binding(db_session, run, data, "reviewer")
    assert replay.id == event.id and replay.target_entity_id != source.id
    target = await db_session.get(IntelligenceEntity, replay.target_entity_id)
    assert target.name == data.new_entity_name and target.kind == source.kind
    identities = await identity_snapshot(db_session, "p1")
    assert identities[target.id]["decision"] == "confirmed"
    assert (await split_snapshot(db_session, "p1"))[target.id][
        "source_entity_id"
    ] == source.id
    assert mention.entity_id == source.id
    refreshed = await prepare_run(db_session, "p1", run.audit_id)
    refreshed.status = "partial"
    copied = EntityMention(
        project_id="p1",
        run_id=refreshed.id,
        entity_id=source.id,
        segment_id=segment.id,
        start_offset=mention.start_offset,
        end_offset=mention.end_offset,
        quote=mention.quote,
    )
    db_session.add(copied)
    await db_session.flush()
    _, summary = await build_summary(db_session, refreshed)
    other = EntityMention(
        project_id="p1",
        run_id=refreshed.id,
        entity_id=source.id,
        segment_id=segment.id,
        start_offset=0,
        end_offset=2,
        quote=segment.text[:2],
    )
    db_session.add(other)
    await db_session.flush()
    _, summary = await build_summary(db_session, refreshed)
    assert {item["id"] for item in summary["entities"]} == {target.id, source.id}
    assert (
        next(item for item in summary["entities"] if item["id"] == target.id)["role"]
        == "unregistered"
    )
    assert (
        next(item for item in summary["mentions"] if item["id"] == other.id)[
            "identity_ambiguity"
        ]
        is True
    )
    assert (
        next(item for item in summary["mentions"] if item["id"] == copied.id)[
            "identity_ambiguity"
        ]
        is False
    )
    decision = IdentityDecision(
        entityId=target.id,
        decision="excluded",
        canonicalName=target.name,
        reason="进一步核对后暂时排除此主体身份",
        expectedVersion=1,
        idempotencyKey="exclude-split-123",
    )
    excluded = await decide_identity(db_session, refreshed, decision, "reviewer")
    assert excluded.version == 2
    await db_session.commit()
    reset = data.model_copy(
        update={
            "new_entity_name": None,
            "decision": "reset",
            "expected_version": 1,
            "idempotency_key": "reset-split-123",
        }
    )
    await decide_binding(db_session, run, reset, "reviewer")
    await db_session.commit()
    assert target.id in await split_snapshot(db_session, "p1")
    assert (await binding_snapshot(db_session, "p1"))[event.binding_key][
        "decision"
    ] == "reset"


@pytest.mark.asyncio
async def test_split_rejects_duplicate_names_and_changed_replay(db_session):
    from pydantic import ValidationError

    run, _, source, _, _ = await alias_fixture(db_session)
    mention = await db_session.scalar(
        select(EntityMention).where(
            EntityMention.run_id == run.id, EntityMention.entity_id == source.id
        )
    )
    data = MentionBindingRequest(
        mentionId=mention.id,
        newEntityName="Yunheng",
        decision="split",
        reason="本片段属于独立的工业设备品牌",
        expectedVersion=0,
        idempotencyKey="split-subject-123",
    )
    with pytest.raises(HTTPException) as error:
        await decide_binding(db_session, run, data, "reviewer")
    assert error.value.status_code == 422
    data.new_entity_name = "Yunheng（工业设备品牌）"
    await decide_binding(db_session, run, data, "reviewer")
    await db_session.commit()
    with pytest.raises(HTTPException) as error:
        await decide_binding(
            db_session,
            run,
            data.model_copy(update={"new_entity_name": "Yunheng（另一主体）"}),
            "reviewer",
        )
    assert error.value.status_code == 409
    with pytest.raises(ValidationError):
        MentionBindingRequest(
            mentionId=mention.id,
            newEntityName="  ",
            decision="split",
            reason="本片段属于独立的工业设备品牌",
            expectedVersion=0,
            idempotencyKey="bad-split-123",
        )
    with pytest.raises(ValidationError):
        MentionBindingRequest(
            mentionId=mention.id,
            targetEntityId=source.id,
            newEntityName="another",
            decision="split",
            reason="本片段属于独立的工业设备品牌",
            expectedVersion=0,
            idempotencyKey="bad-split-123",
        )
