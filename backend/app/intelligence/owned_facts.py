"""Verbatim owned-material facts require independent, named approval."""

from datetime import UTC

from fastapi import HTTPException
from pydantic import AwareDatetime, Field, StrictInt, TypeAdapter, model_validator
from sqlalchemy import select

from app.intelligence.models import BrandFact, SourceSnapshot
from app.intelligence.owned_assets import (
    Request,
    get_asset,
    latest_asset,
    lock_project,
    material_hash,
    notify_project,
    own_brand,
    read_text,
)
from app.intelligence.semantics import stable_hash
from app.intelligence.value_contracts import FactSource
from app.utils.timezone import utc_isoformat, utcnow


class ApproveOwnedFact(Request):
    quote: str = Field(min_length=1, max_length=2000)
    start: StrictInt = Field(ge=0)
    end: StrictInt = Field(ge=1)
    expected_version: StrictInt = Field(alias="expectedVersion", ge=0)
    expected_verification_version: StrictInt = Field(
        alias="expectedVerificationVersion", ge=1
    )
    material_hash: str = Field(alias="materialHash", pattern=r"^[a-f0-9]{64}$")
    scope: str = Field(min_length=5, max_length=1000)
    reason: str = Field(min_length=5, max_length=2000)
    valid_from: AwareDatetime = Field(alias="validFrom")
    valid_until: AwareDatetime = Field(alias="validUntil")

    @model_validator(mode="after")
    def coherent(self):
        if self.valid_until <= self.valid_from or self.end - self.start != len(
            self.quote
        ):
            raise ValueError("Positive validity and exact Unicode evidence required")
        return self


async def owned_material_fresh(db, asset, *, lock=False):
    try:
        await own_brand(db, asset.project_id, asset.registry_brand_id, lock=lock)
    except HTTPException:
        return False
    latest = await latest_asset(db, asset.project_id, asset.asset_key, lock=lock)
    current = asset.verification
    if (
        latest.id != asset.id
        or asset.status != "captured"
        or current.get("decision") != "verified"
        or current.get("material_hash") != material_hash(asset)
        or not current.get("actor_id")
        or current.get("quality", {}).get("evidence_usable") is False
    ):
        return False
    if asset.source_kind == "url":
        snapshot = await db.scalar(
            select(SourceSnapshot).where(
                SourceSnapshot.project_id == asset.project_id,
                SourceSnapshot.id == asset.snapshot_id,
            )
        )
        if (
            not snapshot
            or snapshot.integrity_status != "verified"
            or snapshot.text_key != asset.text_key
            or snapshot.text_sha256 != asset.text_sha256
        ):
            return False
    return True


async def approve_owned_fact(db, asset, data, actor, blobs=None):
    from app.intelligence.reviews import add_event

    await lock_project(db, asset.project_id)
    asset = await get_asset(db, asset.project_id, asset.id, lock=True)
    request_hash = stable_hash([asset.id, data.model_dump(mode="json"), actor])
    previous = await db.scalar(
        select(BrandFact)
        .where(
            BrandFact.project_id == asset.project_id,
            BrandFact.idempotency_key == data.idempotency_key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if previous:
        if previous.request_hash != request_hash:
            raise HTTPException(409, "Fact request key conflict")
        return previous
    if (
        not await owned_material_fresh(db, asset, lock=True)
        or data.material_hash != material_hash(asset)
        or asset.verification["version"] != data.expected_verification_version
    ):
        raise HTTPException(409, "Current named material verification required")
    text = await read_text(asset, blobs)
    if (
        not data.quote.strip()
        or not (data.start < data.end <= len(text))
        or text[data.start : data.end] != data.quote
    ):
        raise HTTPException(
            422, "Fact quote must match this material and Unicode offsets"
        )
    if (
        min(len(data.scope.strip()), len(data.reason.strip())) < 5
        or data.valid_until.astimezone(UTC).replace(tzinfo=None) <= utcnow()
    ):
        raise HTTPException(
            422, "Fact scope, rationale and unexpired validity required"
        )
    # Same logical assertion can be re-approved against a new material revision.
    key = stable_hash(
        ["owned-fact-v1", asset.asset_key, data.quote, data.scope.strip()]
    )
    latest = await db.scalar(
        select(BrandFact.version)
        .where(BrandFact.project_id == asset.project_id, BrandFact.fact_key == key)
        .order_by(BrandFact.version.desc())
        .limit(1)
        .with_for_update()
    )
    if (latest or 0) != data.expected_version:
        raise HTTPException(409, "Fact version changed")
    source = (
        TypeAdapter(FactSource)
        .validate_python(
            {
                "kind": "owned_asset_revision",
                "asset_id": asset.id,
                "asset_revision": asset.version,
                "verification_id": asset.verification["id"],
                "evidence": {
                    "project_id": asset.project_id,
                    "owner_kind": "owned_asset",
                    "owner_id": asset.id,
                    "text_hash": asset.verification["body_hash"],
                    "quote": data.quote,
                    "start": data.start,
                    "end": data.end,
                },
            }
        )
        .model_dump()
    )
    fact = BrandFact(
        project_id=asset.project_id,
        fact_key=key,
        version=data.expected_version + 1,
        entity_id=None,
        identity_event_id=None,
        review_id=None,
        source_kind="owned_asset_revision",
        target_id=asset.target_id,
        registry_brand_id=asset.registry_brand_id,
        owned_asset_revision_id=asset.id,
        owned_asset_version=asset.version,
        source_ref=source,
        statement=data.quote,
        scope=data.scope.strip(),
        valid_from=data.valid_from.astimezone(UTC).replace(tzinfo=None),
        valid_until=data.valid_until.astimezone(UTC).replace(tzinfo=None),
        state="approved",
        reason=data.reason.strip(),
        actor_id=actor,
        idempotency_key=data.idempotency_key,
        request_hash=request_hash,
    )
    await add_event(db, fact)
    await notify_project(db, fact.project_id, "owned_fact_changed", fact.id)
    return fact


async def owned_fact_fresh(db, fact, *, read=False, blobs=None, lock=False):
    asset = await get_asset(db, fact.project_id, fact.owned_asset_revision_id, lock=lock)
    if not await owned_material_fresh(db, asset, lock=lock):
        return False
    try:
        source = TypeAdapter(FactSource).validate_python(fact.source_ref)
    except ValueError:
        return False
    if (
        source.kind != "owned_asset_revision"
        or source.asset_id != asset.id
        or source.asset_revision != asset.version
        or source.verification_id != asset.verification.get("id")
        or fact.owned_asset_version != asset.version
        or fact.target_id != asset.target_id
        or fact.registry_brand_id != asset.registry_brand_id
        or source.evidence.project_id != fact.project_id
        or source.evidence.text_hash != asset.verification.get("body_hash")
        or source.evidence.quote != fact.statement
        or source.evidence.end > asset.verification.get("text_length", 0)
    ):
        return False
    if read:
        text = await read_text(asset, blobs)
        if text[source.evidence.start : source.evidence.end] != fact.statement:
            return False
    return True


async def withdraw_owned_fact(db, fact, data, actor):
    from app.intelligence.reviews import add_event

    await lock_project(db, fact.project_id)
    request_hash = stable_hash([fact.id, data.model_dump(), actor])
    old = await db.scalar(
        select(BrandFact)
        .where(
            BrandFact.project_id == fact.project_id,
            BrandFact.idempotency_key == data.idempotency_key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if old:
        if old.request_hash != request_hash:
            raise HTTPException(409, "Withdrawal key conflict")
        return old
    latest = await db.scalar(
        select(BrandFact)
        .where(
            BrandFact.project_id == fact.project_id, BrandFact.fact_key == fact.fact_key
        )
        .order_by(BrandFact.version.desc())
        .limit(1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        latest.id != fact.id
        or latest.version != data.expected_version
        or len(data.reason.strip()) < 5
    ):
        raise HTTPException(409, "Fact version changed or rationale missing")
    fields = {
        key: getattr(fact, key)
        for key in (
            "project_id",
            "fact_key",
            "entity_id",
            "identity_event_id",
            "review_id",
            "source_kind",
            "target_id",
            "registry_brand_id",
            "owned_asset_revision_id",
            "owned_asset_version",
            "source_ref",
            "statement",
            "scope",
            "valid_from",
            "valid_until",
        )
    }
    new = BrandFact(
        **fields,
        version=fact.version + 1,
        state="withdrawn",
        reason=data.reason.strip(),
        actor_id=actor,
        idempotency_key=data.idempotency_key,
        request_hash=request_hash,
    )
    await add_event(db, new)
    await notify_project(db, new.project_id, "owned_fact_changed", new.id)
    return new


async def fact_status(db, fact, fresh, *, now=None, lock=False):
    now = now or utcnow()
    query = (select(BrandFact.id)
        .where(
            BrandFact.project_id == fact.project_id, BrandFact.fact_key == fact.fact_key
        )
        .order_by(BrandFact.version.desc())
        .limit(1))
    latest = await db.scalar(query.with_for_update() if lock else query)
    status = "superseded" if latest != fact.id else fact.state
    if status == "approved":
        status = (
            "needs_review"
            if not fresh
            else "expired"
            if fact.valid_until <= now
            else "scheduled"
            if fact.valid_from > now
            else "valid"
        )
    return {
        "id": fact.id,
        "fact_key": fact.fact_key,
        "version": fact.version,
        "entity_id": fact.entity_id,
        "target_id": fact.target_id,
        "registry_brand_id": fact.registry_brand_id,
        "source_kind": fact.source_kind,
        "source_ref": fact.source_ref,
        "statement": fact.statement,
        "scope": fact.scope,
        "status": status,
        "review_id": fact.review_id,
        "valid_from": utc_isoformat(fact.valid_from),
        "valid_until": utc_isoformat(fact.valid_until),
    }
