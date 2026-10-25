"""Append-only attribution of a single immutable occurrence, never a fact approval."""

from typing import Literal

from fastapi import HTTPException
from pydantic import Field, model_validator
from sqlalchemy import select

from app.intelligence.aliases import alias_root, alias_snapshot
from app.intelligence.capture_contract import StrictFact
from app.intelligence.identity import identity_snapshot
from app.intelligence.models import (
    ContentSegment,
    EntityMention,
    EntityResolutionEvent,
    IntelligenceEntity,
    MentionBindingEvent,
)
from app.intelligence.refresh import request_refresh, state_for_audit
from app.intelligence.semantics import stable_hash


class MentionBindingRequest(StrictFact):
    mention_id: str = Field(alias="mentionId", pattern=r"^[a-f0-9]{32}$")
    target_entity_id: str | None = Field(
        default=None, alias="targetEntityId", pattern=r"^[a-f0-9]{32}$"
    )
    new_entity_name: str | None = Field(
        default=None, alias="newEntityName", min_length=2, max_length=200
    )
    decision: Literal["assign", "reset", "split"]
    reason: str = Field(min_length=5, max_length=2000)
    expected_version: int = Field(alias="expectedVersion", ge=0)
    idempotency_key: str = Field(
        alias="idempotencyKey", pattern=r"^[a-zA-Z0-9_-]{8,100}$"
    )

    @model_validator(mode="after")
    def valid_target(self):
        if (
            (self.decision == "assign") != bool(self.target_entity_id)
            or (self.decision == "split") != bool(self.new_entity_name)
            or self.new_entity_name is not None
            and len(self.new_entity_name.strip()) < 2
        ):
            raise ValueError(
                "Choose existing target for assign, a distinct name for split, neither for reset"
            )
        if len(self.reason.strip()) < 5:
            raise ValueError(
                "Assign requires a target; reset has no target; reason required"
            )
        return self


def binding_key(mention, segment):
    return stable_hash(
        [
            segment.id,
            segment.text,
            segment.start_offset,
            mention.entity_id,
            mention.start_offset,
            mention.end_offset,
            mention.quote,
        ]
    )


async def binding_snapshot(db, project_id, *, lock=False):
    query = (select(MentionBindingEvent)
            .where(MentionBindingEvent.project_id == project_id)
            .order_by(MentionBindingEvent.binding_key, MentionBindingEvent.version))
    rows = (await db.scalars(query.with_for_update().execution_options(populate_existing=True) if lock else query)).all()
    return {
        row.binding_key: {
            "event_id": row.id,
            "version": row.version,
            "decision": row.decision,
            "target_entity_id": row.target_entity_id,
            "reason": row.reason,
        }
        for row in rows
    }


def attributed_entity(mention, segment, manifest, fallback):
    item = manifest.get("mention_bindings", {}).get(binding_key(mention, segment), {})
    target = item.get("target_entity_id")
    if (
        item.get("decision") in ("assign", "split")
        and manifest.get("entity_resolutions", {}).get(target, {}).get("decision")
        == "confirmed"
    ):
        return target
    return fallback


async def decide_binding(db, run, data, actor_id):
    await state_for_audit(db, run.project_id, run.audit_id)
    entities = (
        await db.scalars(
            select(IntelligenceEntity)
            .where(IntelligenceEntity.project_id == run.project_id)
            .order_by(IntelligenceEntity.id)
            .with_for_update()
        )
    ).all()
    previous = await db.scalar(
        select(MentionBindingEvent)
        .where(
            MentionBindingEvent.project_id == run.project_id,
            MentionBindingEvent.idempotency_key == data.idempotency_key,
        )
        .with_for_update()
    )
    request_hash = stable_hash([run.id, data.model_dump(), actor_id])
    if previous:
        if previous.request_hash != request_hash:
            raise HTTPException(409, "Occurrence idempotency key conflict")
        return previous
    result = (
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
                EntityMention.id == data.mention_id,
            )
        )
    ).first()
    if not result:
        raise HTTPException(404, "Occurrence not found in this run")
    mention, segment = result
    allowed_owners = {
        "answer": {row["id"] for row in run.manifest.get("responses", [])},
        "citation": {row["id"] for row in run.manifest.get("sources", [])},
        "snapshot": {
            row.get("snapshot_id")
            for row in run.manifest.get("sources", [])
            if row.get("snapshot_id")
        },
    }
    if segment.owner_id not in allowed_owners.get(segment.owner_kind, set()):
        raise HTTPException(409, "Occurrence owner is outside this analysis manifest")
    start, end = (
        mention.start_offset - segment.start_offset,
        mention.end_offset - segment.start_offset,
    )
    if (
        not (0 <= start < end <= len(segment.text))
        or segment.text[start:end] != mention.quote
    ):
        raise HTTPException(409, "Occurrence no longer matches immutable text")
    key = binding_key(mention, segment)
    snapshot = await binding_snapshot(db, run.project_id)
    if snapshot.get(key, {}).get("version", 0) != data.expected_version:
        raise HTTPException(409, "Occurrence decision changed; refresh")
    target_id = data.target_entity_id
    if data.decision == "split":
        name = data.new_entity_name.strip()
        source = next(entity for entity in entities if entity.id == mention.entity_id)
        identities = await identity_snapshot(db, run.project_id)
        occupied_names = {entity.name.casefold() for entity in entities}
        occupied_names.update(
            item.get("canonical_name", "").casefold() for item in identities.values()
        )
        if name.casefold() in occupied_names:
            raise HTTPException(
                422, "Use a distinct qualified name, or assign to an existing entity"
            )
        target = IntelligenceEntity(
            project_id=run.project_id,
            name=name,
            kind=source.kind,
            identity_hash=stable_hash(
                ["human_split", run.project_id, data.idempotency_key]
            ),
        )
        db.add(target)
        await db.flush()
        target_id = target.id
        db.add(
            EntityResolutionEvent(
                project_id=run.project_id,
                entity_id=target.id,
                run_id=run.id,
                version=1,
                decision="confirmed",
                canonical_name=name,
                reason=data.reason.strip(),
                actor_id=actor_id,
                idempotency_key="split-"
                + stable_hash([run.project_id, data.idempotency_key]),
                request_hash=request_hash,
            )
        )
    if data.decision == "assign":
        target = next(
            (entity for entity in entities if entity.id == data.target_entity_id), None
        )
        identities = await identity_snapshot(db, run.project_id)
        if (
            target is None
            or identities.get(target.id, {}).get("decision") != "confirmed"
        ):
            raise HTTPException(422, "Target must be confirmed in this project")
        if alias_root(target.id, await alias_snapshot(db, run.project_id)) != target.id:
            raise HTTPException(422, "Choose the canonical root entity")
        source = next(entity for entity in entities if entity.id == mention.entity_id)
        if target.kind != source.kind:
            raise HTTPException(422, "Target kind must match the observed entity")
    event = MentionBindingEvent(
        project_id=run.project_id,
        binding_key=key,
        mention_id=mention.id,
        target_entity_id=target_id,
        version=data.expected_version + 1,
        decision=data.decision,
        reason=data.reason.strip(),
        actor_id=actor_id,
        idempotency_key=data.idempotency_key,
        request_hash=request_hash,
    )
    db.add(event)
    await db.flush()
    await request_refresh(
        db, run.project_id, run.audit_id, "mention_binding_changed", event.id
    )
    return event


async def split_snapshot(db, project_id):
    rows = (
        await db.execute(
            select(MentionBindingEvent, EntityMention.entity_id)
            .join(
                EntityMention,
                (MentionBindingEvent.mention_id == EntityMention.id)
                & (MentionBindingEvent.project_id == EntityMention.project_id),
            )
            .where(
                MentionBindingEvent.project_id == project_id,
                MentionBindingEvent.decision == "split",
            )
        )
    ).all()
    return {
        event.target_entity_id: {
            "source_entity_id": source_id,
            "mention_id": event.mention_id,
            "event_id": event.id,
        }
        for event, source_id in rows
    }
