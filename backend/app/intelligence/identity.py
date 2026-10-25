"""Append-only human decisions, frozen into subsequent run input manifests."""

from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.intelligence.models import (
    ContentSegment,
    EntityMention,
    EntityResolutionEvent,
    IntelligenceEntity,
)
from app.intelligence.refresh import request_refresh
from app.intelligence.semantics import stable_hash


class IdentityDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    entity_id: str = Field(alias="entityId", pattern=r"^[a-f0-9]{32}$")
    decision: Literal["confirmed", "excluded"]
    canonical_name: str = Field(alias="canonicalName", min_length=1, max_length=200)
    reason: str = Field(min_length=5, max_length=2000)
    expected_version: int = Field(alias="expectedVersion", ge=0)
    idempotency_key: str = Field(
        alias="idempotencyKey", pattern=r"^[a-zA-Z0-9_-]{8,100}$"
    )


async def identity_snapshot(db, project_id, *, lock=False):
    query = (select(EntityResolutionEvent)
            .where(EntityResolutionEvent.project_id == project_id)
            .order_by(EntityResolutionEvent.entity_id, EntityResolutionEvent.version))
    rows = (await db.scalars(query.with_for_update().execution_options(populate_existing=True) if lock else query)).all()
    return {
        row.entity_id: {
            "event_id": row.id,
            "version": row.version,
            "decision": row.decision,
            "canonical_name": row.canonical_name,
            "reason": row.reason,
        }
        for row in rows
    }


async def decide_identity(db, run, data, actor_id):
    # Lock order matches run creation / refresh: audit first, then identity.
    from app.intelligence.refresh import state_for_audit

    await state_for_audit(db, run.project_id, run.audit_id)
    entity = await db.scalar(
        select(IntelligenceEntity)
        .where(
            IntelligenceEntity.project_id == run.project_id,
            IntelligenceEntity.id == data.entity_id,
        )
        .with_for_update()
    )
    mention = await db.scalar(
        select(EntityMention.id)
        .where(
            EntityMention.project_id == run.project_id,
            EntityMention.run_id == run.id,
            EntityMention.entity_id == data.entity_id,
        )
        .limit(1)
    )
    if entity is not None and mention is None:
        from app.intelligence.mention_bindings import binding_key

        observations = (
            await db.execute(
                select(EntityMention, ContentSegment)
                .join(
                    ContentSegment,
                    (EntityMention.segment_id == ContentSegment.id)
                    & (EntityMention.project_id == ContentSegment.project_id),
                )
                .where(
                    EntityMention.project_id == run.project_id,
                    EntityMention.run_id == run.id,
                )
            )
        ).all()
        mention = next(
            (
                row.id
                for row, segment in observations
                if run.manifest.get("mention_bindings", {})
                .get(binding_key(row, segment), {})
                .get("decision")
                in ("assign", "split")
                and run.manifest["mention_bindings"][binding_key(row, segment)].get(
                    "target_entity_id"
                )
                == entity.id
            ),
            None,
        )
    if entity is None or mention is None:
        raise HTTPException(404, "Entity is not observed in this run")
    request_hash = stable_hash([run.id, data.model_dump(), actor_id])
    previous = await db.scalar(
        select(EntityResolutionEvent)
        .where(
            EntityResolutionEvent.project_id == run.project_id,
            EntityResolutionEvent.idempotency_key == data.idempotency_key,
        )
        .with_for_update()
    )
    if previous:
        if previous.request_hash != request_hash:
            raise HTTPException(409, "Identity request key conflict")
        return previous
    latest = await db.scalar(
        select(EntityResolutionEvent)
        .where(
            EntityResolutionEvent.project_id == run.project_id,
            EntityResolutionEvent.entity_id == entity.id,
        )
        .order_by(EntityResolutionEvent.version.desc())
        .with_for_update()
        .limit(1)
    )
    version = latest.version if latest else 0
    if data.expected_version != version:
        raise HTTPException(409, "Identity changed; refresh before reviewing")
    if len(data.reason.strip()) < 5 or not data.canonical_name.strip():
        raise HTTPException(422, "Identity decision requires name and reason")
    event = EntityResolutionEvent(
        project_id=run.project_id,
        entity_id=entity.id,
        run_id=run.id,
        version=version + 1,
        decision=data.decision,
        canonical_name=data.canonical_name.strip(),
        reason=data.reason.strip(),
        actor_id=actor_id,
        idempotency_key=data.idempotency_key,
        request_hash=request_hash,
    )
    try:
        async with db.begin_nested():
            db.add(event)
            await db.flush()
    except IntegrityError as error:
        raise HTTPException(
            409, "Identity decision collided; refresh before reviewing"
        ) from error
    await request_refresh(
        db, run.project_id, run.audit_id, "entity_resolution_changed", event.id
    )
    return event
