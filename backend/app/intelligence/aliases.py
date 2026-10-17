"""Human alias events; raw mentions remain immutable and never become facts."""

from typing import Literal

from fastapi import HTTPException
from pydantic import Field, model_validator
from sqlalchemy import select

from app.intelligence.capture_contract import StrictFact
from app.intelligence.identity import identity_snapshot
from app.intelligence.models import EntityAliasEvent, EntityMention, IntelligenceEntity
from app.intelligence.refresh import request_refresh, state_for_audit
from app.intelligence.semantics import stable_hash


class AliasRequest(StrictFact):
    source_entity_id: str = Field(alias="sourceEntityId", pattern=r"^[a-f0-9]{32}$")
    target_entity_id: str | None = Field(
        default=None, alias="targetEntityId", pattern=r"^[a-f0-9]{32}$"
    )
    decision: Literal["merge", "unlink"]
    reason: str = Field(min_length=5, max_length=2000)
    expected_version: int = Field(alias="expectedVersion", ge=0)
    idempotency_key: str = Field(
        alias="idempotencyKey", pattern=r"^[a-zA-Z0-9_-]{8,100}$"
    )

    @model_validator(mode="after")
    def target_required(self):
        if (self.decision == "merge") != bool(self.target_entity_id) or len(
            self.reason.strip()
        ) < 5:
            raise ValueError(
                "Merge needs a target; unlink must not supply one, and a reason is required"
            )
        return self


async def alias_snapshot(db, project_id):
    rows = (
        await db.scalars(
            select(EntityAliasEvent)
            .where(EntityAliasEvent.project_id == project_id)
            .order_by(EntityAliasEvent.source_entity_id, EntityAliasEvent.version)
        )
    ).all()
    return {
        row.source_entity_id: {
            "event_id": row.id,
            "version": row.version,
            "target_entity_id": row.target_entity_id,
            "target_name": row.target_name,
            "decision": row.decision,
            "reason": row.reason,
        }
        for row in rows
    }


def alias_root(entity_id, bindings):
    visited = set()
    while bindings.get(entity_id, {}).get("decision") == "merge":
        if entity_id in visited or len(visited) >= 64:
            raise HTTPException(409, "Alias cycle or excessive chain depth")
        visited.add(entity_id)
        entity_id = bindings[entity_id]["target_entity_id"]
        if not entity_id:
            raise HTTPException(409, "Alias target is missing")
    return entity_id


def resolved_entity(entity_id, manifest):
    root = alias_root(entity_id, manifest.get("entity_aliases", {}))
    if (
        root != entity_id
        and manifest.get("entity_resolutions", {}).get(root, {}).get("decision")
        != "confirmed"
    ):
        return entity_id
    return root


async def decide_alias(db, run, data, actor_id):
    await state_for_audit(db, run.project_id, run.audit_id)
    # Serialize project identity edits in stable order; two concurrent merges cannot form a cycle.
    entities = (
        await db.scalars(
            select(IntelligenceEntity)
            .where(IntelligenceEntity.project_id == run.project_id)
            .order_by(IntelligenceEntity.id)
            .with_for_update()
        )
    ).all()
    by_id = {row.id: row for row in entities}
    previous = await db.scalar(
        select(EntityAliasEvent)
        .where(
            EntityAliasEvent.project_id == run.project_id,
            EntityAliasEvent.idempotency_key == data.idempotency_key,
        )
        .with_for_update()
    )
    request_hash = stable_hash([run.id, data.model_dump(), actor_id])
    if previous:
        if previous.request_hash != request_hash:
            raise HTTPException(409, "Alias idempotency key conflict")
        return previous
    source = by_id.get(data.source_entity_id)
    observed = await db.scalar(
        select(EntityMention.id)
        .where(
            EntityMention.project_id == run.project_id,
            EntityMention.run_id == run.id,
            EntityMention.entity_id == data.source_entity_id,
        )
        .limit(1)
    )
    if source is None or observed is None:
        raise HTTPException(404, "Source entity is not observed in this run")
    snapshot = await alias_snapshot(db, run.project_id)
    if data.expected_version != snapshot.get(source.id, {}).get("version", 0):
        raise HTTPException(409, "Alias mapping changed; refresh before reviewing")
    target = None
    if data.decision == "merge":
        target = by_id.get(data.target_entity_id)
        identities = await identity_snapshot(db, run.project_id)
        if (
            target is None
            or target.kind != source.kind
            or identities.get(target.id, {}).get("decision") != "confirmed"
        ):
            raise HTTPException(
                422,
                "Target must be a confirmed entity of the same kind in this project",
            )
        # Require an explicit root to avoid hidden redirects in an approval.
        if alias_root(target.id, snapshot) != target.id:
            raise HTTPException(422, "Choose the canonical root entity")
        proposed = {
            **snapshot,
            source.id: {"decision": "merge", "target_entity_id": target.id},
        }
        alias_root(source.id, proposed)
    event = EntityAliasEvent(
        project_id=run.project_id,
        source_entity_id=source.id,
        target_entity_id=target.id if target else None,
        target_name=target.name if target else None,
        run_id=run.id,
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
        db, run.project_id, run.audit_id, "entity_alias_changed", event.id
    )
    return event
