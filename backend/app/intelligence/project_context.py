"""Portal-owned versioned registered-brand context; immutable receiver receipts."""

from typing import Annotated, Literal

from fastapi import HTTPException
from pydantic import Field, StrictInt, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.intelligence.aliases import alias_root, alias_snapshot
from app.intelligence.capture_contract import BrandSnapshot, StrictFact
from app.intelligence.identity import identity_snapshot
from app.intelligence.models import (
    EntityResolutionEvent,
    IntelligenceEntity,
    ProjectContextEvent,
    ProjectContextHead,
)
from app.intelligence.question_context import SCENARIOS, Scenario
from app.intelligence.semantics import stable_hash


class ContextBrand(BrandSnapshot):
    analysis_role: Literal["registered", "comparison"] = "registered"
    entity_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    identity_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def binding_pair(self):
        if (self.entity_id is None) != (self.identity_version is None):
            raise ValueError("Binding requires entity and confirmed identity version")
        return self


class ContextRequest(StrictFact):
    version: int = Field(ge=1)
    brands: list[ContextBrand] = Field(max_length=200)
    business_preferences: dict = Field(default_factory=dict)

    @field_validator("business_preferences")
    @classmethod
    def checked_preferences(cls, value):
        if value:
            return BusinessPreferences.model_validate(value).model_dump()
        return value

    @model_validator(mode="after")
    def unique_brands(self):
        if len({row.id for row in self.brands}) != len(self.brands) or any(
            not row.name.strip() for row in self.brands
        ):
            raise ValueError("Distinct brand IDs and nonblank names required")
        return self


class BusinessPreferences(StrictFact):
    version: Literal["business-preferences-v1"]
    scenario_importance: dict[
        Scenario,
        Annotated[StrictInt, Field(ge=0, le=100)],
    ] = Field(max_length=8)
    reason: str = Field(min_length=5, max_length=2000)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value):
        if len(value.strip()) < 5:
            raise ValueError("Meaningful preference reason required")
        return value.strip()

    @model_validator(mode="after")
    def nonzero_importance(self):
        if self.scenario_importance and not any(self.scenario_importance.values()):
            raise ValueError("Keep neutral defaults or provide a positive importance")
        return self


def preference_projection(context):
    preferences = (context or {}).get("business_preferences") or {}
    values = preferences.get("scenario_importance", {})
    return {"mode": "custom" if values else "neutral", "context_version": (context or {}).get("version", 0), "priority_policy_version": f"scenario-priority-v1:{(context or {}).get('version', 0)}:{(context or {}).get('content_hash', 'neutral')}", "importance": {key: values.get(key, 50) for key in SCENARIOS}, "reason": preferences.get("reason"), "taxonomy": SCENARIOS, "score_weights_changed": False}


async def context_snapshot(db, project_id):
    event = await db.scalar(
        select(ProjectContextEvent)
        .where(ProjectContextEvent.project_id == project_id)
        .order_by(ProjectContextEvent.version.desc())
        .limit(1)
    )
    return (
        {
            "version": event.version,
            "content_hash": event.content_hash,
            "brands": event.brands,
            "business_preferences": event.business_preferences,
        }
        if event
        else None
    )


async def receive_context(db, project_id, data, actor_id):
    head = await db.scalar(
        select(ProjectContextHead)
        .where(ProjectContextHead.project_id == project_id)
        .with_for_update()
    )
    if head is None:
        try:
            async with db.begin_nested():
                db.add(ProjectContextHead(project_id=project_id, version=0))
                await db.flush()
        except IntegrityError:
            pass
        head = await db.scalar(
            select(ProjectContextHead)
            .where(ProjectContextHead.project_id == project_id)
            .with_for_update()
        )
    for brand in data.brands:
        if brand.entity_id:
            proof = await db.scalar(
                select(EntityResolutionEvent.id)
                .join(
                    IntelligenceEntity,
                    (EntityResolutionEvent.entity_id == IntelligenceEntity.id)
                    & (
                        EntityResolutionEvent.project_id
                        == IntelligenceEntity.project_id
                    ),
                )
                .where(
                    EntityResolutionEvent.project_id == project_id,
                    EntityResolutionEvent.entity_id == brand.entity_id,
                    EntityResolutionEvent.version == brand.identity_version,
                    EntityResolutionEvent.decision == "confirmed",
                    IntelligenceEntity.kind.in_(["brand", "organization", "product"]),
                )
            )
            if proof is None:
                raise HTTPException(
                    422, "Brand binding has no confirmed project identity proof"
                )
    brands = [row.model_dump() for row in sorted(data.brands, key=lambda row: row.id)]
    content_hash = stable_hash(
        {"brands": brands, "business_preferences": data.business_preferences}
    )
    previous = await db.scalar(
        select(ProjectContextEvent)
        .where(
            ProjectContextEvent.project_id == project_id,
            ProjectContextEvent.version == data.version,
        )
        .with_for_update()
    )
    if previous:
        if previous.content_hash != content_hash:
            raise HTTPException(409, "Same context version has conflicting content")
        return previous
    if data.version != head.version + 1:
        raise HTTPException(409, "Context version gap; deliver earlier snapshots first")
    event = ProjectContextEvent(
        project_id=project_id,
        version=data.version,
        content_hash=content_hash,
        brands=brands,
        business_preferences=data.business_preferences,
        received_by=actor_id,
    )
    head.version = data.version
    db.add(event)
    await db.flush()
    return event


async def binding_proof(db, project_id, entity_id):
    entity = await db.scalar(
        select(IntelligenceEntity).where(
            IntelligenceEntity.id == entity_id,
            IntelligenceEntity.project_id == project_id,
        )
    )
    identity = (await identity_snapshot(db, project_id)).get(entity_id, {})
    if entity is None:
        raise HTTPException(404, "Entity not found")
    if (
        entity.kind not in ("brand", "organization", "product")
        or identity.get("decision") != "confirmed"
        or alias_root(entity_id, await alias_snapshot(db, project_id)) != entity_id
    ):
        raise HTTPException(
            422, "Choose a currently confirmed brand, organization or product root"
        )
    return {
        "id": entity_id,
        "version": identity["version"],
        "name": identity["canonical_name"],
    }


def bound_roles(manifest, entity_id):
    identity = manifest.get("entity_resolutions", {}).get(entity_id, {})
    roles = set()
    for brand in (manifest.get("brand_context") or {}).get("brands", []):
        if (
            brand.get("entity_id") == entity_id
            and identity.get("decision") == "confirmed"
            and identity.get("version") == brand.get("identity_version")
            and alias_root(entity_id, manifest.get("entity_aliases", {})) == entity_id
        ):
            roles.add(
                "comparison"
                if brand.get("analysis_role") == "comparison"
                else "competitor"
                if brand.get("is_competitor")
                else "own"
            )
    return roles
