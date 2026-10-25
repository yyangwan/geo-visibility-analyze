"""Human evidence decisions and scoped, expiring fact approvals remain append-only."""

from datetime import UTC
from typing import Literal

from fastapi import HTTPException
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.intelligence.aliases import alias_snapshot
from app.intelligence.identity import identity_snapshot
from app.intelligence.mention_bindings import binding_snapshot
from app.intelligence.models import (
    BrandFact,
    ClaimEvidence,
    ClaimReview,
    ContentSegment,
    IntelligenceEntity,
    IntelligenceRun,
    SourceFetch,
    SourceSnapshot,
)
from app.intelligence.project_context import context_snapshot
from app.intelligence.refresh import request_refresh, state_for_audit
from app.intelligence.semantics import stable_hash
from app.intelligence.value_contracts import FactSource
from app.utils.timezone import utc_isoformat, utcnow


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    relation: Literal["supports", "partial", "contradicts", "unverified"]
    source_quote: str = Field(alias="sourceQuote", max_length=2000)
    reason: str = Field(min_length=5, max_length=2000)
    expected_version: int = Field(alias="expectedVersion", ge=0)
    idempotency_key: str = Field(
        alias="idempotencyKey", pattern=r"^[a-zA-Z0-9_-]{8,100}$"
    )


class FactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    review_id: str = Field(alias="reviewId", pattern=r"^[a-f0-9]{32}$")
    entity_id: str = Field(alias="entityId", pattern=r"^[a-f0-9]{32}$")
    scope: str = Field(min_length=5, max_length=1000)
    valid_from: AwareDatetime = Field(alias="validFrom")
    valid_until: AwareDatetime = Field(alias="validUntil")
    reason: str = Field(min_length=5, max_length=2000)
    expected_version: int = Field(alias="expectedVersion", ge=0)
    idempotency_key: str = Field(
        alias="idempotencyKey", pattern=r"^[a-zA-Z0-9_-]{8,100}$"
    )

    @model_validator(mode="after")
    def window(self):
        if self.valid_until <= self.valid_from:
            raise ValueError("Fact validity interval must be positive")
        return self


class WithdrawRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    expected_version: int = Field(alias="expectedVersion", ge=1)
    reason: str = Field(min_length=5, max_length=2000)
    idempotency_key: str = Field(
        alias="idempotencyKey", pattern=r"^[a-zA-Z0-9_-]{8,100}$"
    )


async def add_event(db, event):
    try:
        async with db.begin_nested():
            db.add(event)
            await db.flush()
    except IntegrityError as error:
        raise HTTPException(409, "Review request collided; refresh version") from error


async def evidence_material(db, run, evidence):
    from app.intelligence.citation_context import association, snapshot_matches

    sources = {source["id"]: source for source in run.manifest["sources"]}
    citation = sources.get(evidence.citation_id)
    answer = await db.scalar(
        select(ContentSegment).where(
            ContentSegment.project_id == run.project_id,
            ContentSegment.id == evidence.answer_segment_id,
        )
    )
    source = await db.scalar(
        select(ContentSegment).where(
            ContentSegment.project_id == run.project_id,
            ContentSegment.id == evidence.source_segment_id,
        )
    )
    if (
        evidence.project_id != run.project_id
        or evidence.run_id != run.id
        or not citation
        or citation["kind"] != "answer_reference"
        or citation["url_resolution"] != "exact"
        or not source
        or not answer
        or source.owner_kind != "snapshot"
        or source.owner_id != citation["snapshot_id"]
        or answer.owner_kind != "answer"
        or answer.owner_id != citation["response_id"]
        or evidence.answer_quote not in answer.text
    ):
        raise HTTPException(409, "Evidence provenance no longer matches run")
    snapshot = await db.scalar(
        select(SourceSnapshot).where(
            SourceSnapshot.project_id == run.project_id,
            SourceSnapshot.id == source.owner_id,
        )
    )
    if (
        snapshot is None
        or snapshot.integrity_status != "verified"
        or snapshot.document_id != citation["document_id"]
        or not snapshot_matches(citation, snapshot)
    ):
        raise HTTPException(409, "Source snapshot integrity is not verified")
    responses = {response["id"]: response for response in run.manifest["responses"]}
    if (
        citation["response_id"] not in responses
        or responses[citation["response_id"]]["platform"] != citation["platform"]
    ):
        raise HTTPException(409, "Evidence platform mismatch")
    span = evidence.verification.get("answer_span")
    if span and (
        type(span.get("start")) is not int or type(span.get("end")) is not int
        or not (answer.start_offset <= span["start"] < span["end"] <= answer.end_offset)
        or responses[citation["response_id"]]["text"][span["start"]:span["end"]] != evidence.answer_quote
        or not association(citation, responses[citation["response_id"]], span["start"], span["end"])["eligible"]
    ):
        raise HTTPException(409, "Evidence does not match the frozen answer anchor")
    proof = stable_hash(
        [
            run.id,
            run.input_hash,
            run.signature,
            evidence.id,
            evidence.answer_quote,
            evidence.verification,
            citation,
            answer.text,
            source.text,
            snapshot.document_id,
            snapshot.representation_hash,
            snapshot.text_sha256,
        ]
    )
    return source, citation, proof


async def review_snapshot(db, project_id, run_id, *, lock=False):
    query = (select(ClaimReview)
            .where(ClaimReview.project_id == project_id, ClaimReview.run_id == run_id)
            .order_by(ClaimReview.version))
    rows = (await db.scalars(query.with_for_update().execution_options(populate_existing=True) if lock else query)).all()
    return {
        row.evidence_id: {
            "id": row.id,
            "version": row.version,
            "relation": row.relation,
            "source_quote": row.source_quote,
            "reason": row.reason,
            "actor_id": row.actor_id,
            "evidence_hash": row.evidence_hash,
        }
        for row in rows
    }


async def review_claim(db, run, evidence_id, data, actor):
    await state_for_audit(db, run.project_id, run.audit_id)
    evidence = await db.scalar(
        select(ClaimEvidence)
        .where(
            ClaimEvidence.project_id == run.project_id,
            ClaimEvidence.run_id == run.id,
            ClaimEvidence.id == evidence_id,
        )
        .with_for_update()
    )
    if evidence is None:
        raise HTTPException(404, "Evidence not observed in this run")
    source, _, proof = await evidence_material(db, run, evidence)
    request_hash = stable_hash([run.id, evidence_id, data.model_dump(), actor])
    previous = await db.scalar(
        select(ClaimReview)
        .where(
            ClaimReview.project_id == run.project_id,
            ClaimReview.idempotency_key == data.idempotency_key,
        )
        .with_for_update()
    )
    if previous:
        if previous.request_hash != request_hash:
            raise HTTPException(409, "Review request key conflict")
        return previous
    latest = await db.scalar(
        select(ClaimReview)
        .where(
            ClaimReview.project_id == run.project_id,
            ClaimReview.evidence_id == evidence_id,
        )
        .order_by(ClaimReview.version.desc())
        .with_for_update()
        .limit(1)
    )
    if (latest.version if latest else 0) != data.expected_version:
        raise HTTPException(409, "Evidence review version changed")
    if (
        len(data.reason.strip()) < 5
        or (data.relation != "unverified" and not data.source_quote.strip())
        or (data.source_quote and data.source_quote not in source.text)
    ):
        raise HTTPException(422, "Review requires verbatim source quote and rationale")
    review = ClaimReview(
        project_id=run.project_id,
        run_id=run.id,
        evidence_id=evidence_id,
        version=data.expected_version + 1,
        relation=data.relation,
        source_quote=data.source_quote,
        reason=data.reason.strip(),
        actor_id=actor,
        evidence_hash=proof,
        idempotency_key=data.idempotency_key,
        request_hash=request_hash,
    )
    await add_event(db, review)
    return review


async def fact_snapshot(db, project_id):
    rows = (
        await db.scalars(
            select(BrandFact)
            .where(BrandFact.project_id == project_id)
            .order_by(BrandFact.version)
        )
    ).all()
    latest = {row.fact_key: row for row in rows}
    result = {
        row.fact_key: {
            "id": row.id,
            "version": row.version,
            "state": row.state,
            "entity_id": row.entity_id,
            "review_id": row.review_id,
            "source_kind": row.source_kind,
            "target_id": row.target_id,
            "registry_brand_id": row.registry_brand_id,
            "owned_asset_revision_id": row.owned_asset_revision_id,
            "owned_asset_version": row.owned_asset_version,
            "source_ref": row.source_ref,
            "statement": row.statement,
            "scope": row.scope,
            "valid_from": utc_isoformat(row.valid_from),
            "valid_until": utc_isoformat(row.valid_until),
        }
        for row in rows
    }
    for key, row in latest.items():
        result[key]["effective_status"] = (await fact_view(db, row))["status"]
    return result


async def approve_fact(db, run, data, actor):
    from app.intelligence.actions import stamp

    state = await state_for_audit(db, run.project_id, run.audit_id)
    previous = await db.scalar(
        select(BrandFact)
        .where(
            BrandFact.project_id == run.project_id,
            BrandFact.idempotency_key == data.idempotency_key,
        )
        .with_for_update()
    )
    request_hash = stable_hash([run.id, data.model_dump(mode="json"), actor])
    if previous:
        if previous.request_hash != request_hash:
            raise HTTPException(409, "Fact request key conflict")
        return previous
    _, fresh = await stamp(db, run, state)
    if not fresh:
        raise HTTPException(409, "Approve facts from current evidence run")
    review = await db.scalar(
        select(ClaimReview).where(
            ClaimReview.project_id == run.project_id,
            ClaimReview.run_id == run.id,
            ClaimReview.id == data.review_id,
        )
    )
    if review is None:
        raise HTTPException(404, "Review not in this run")
    latest = (await review_snapshot(db, run.project_id, run.id)).get(review.evidence_id)
    if review.relation != "supports" or latest["id"] != review.id:
        raise HTTPException(409, "A current human supports decision is required")
    evidence = await db.scalar(
        select(ClaimEvidence).where(
            ClaimEvidence.project_id == run.project_id,
            ClaimEvidence.id == review.evidence_id,
            ClaimEvidence.run_id == run.id,
        )
    )
    _, _, proof = await evidence_material(db, run, evidence)
    if proof != review.evidence_hash:
        raise HTTPException(409, "Evidence changed after review")
    if await context_snapshot(db, run.project_id) != run.manifest.get("brand_context"):
        raise HTTPException(409, "Project brand context changed; refresh analysis")
    if await binding_snapshot(db, run.project_id) != run.manifest.get(
        "mention_bindings", {}
    ):
        raise HTTPException(409, "Occurrence attribution changed; refresh analysis")
    if await alias_snapshot(db, run.project_id) != run.manifest.get(
        "entity_aliases", {}
    ):
        raise HTTPException(
            409, "Alias mapping changed; approve facts against a refreshed run"
        )
    identity = (await identity_snapshot(db, run.project_id)).get(data.entity_id)
    entity = await db.scalar(
        select(IntelligenceEntity)
        .where(
            IntelligenceEntity.project_id == run.project_id,
            IntelligenceEntity.id == data.entity_id,
        )
        .with_for_update()
    )
    if (
        not entity
        or not identity
        or identity["decision"] != "confirmed"
        or entity.name.casefold() not in review.source_quote.casefold()
    ):
        raise HTTPException(
            409, "Fact requires confirmed entity explicitly present in source quote"
        )
    if (
        len(data.scope.strip()) < 5
        or len(data.reason.strip()) < 5
        or data.valid_until.astimezone(UTC).replace(tzinfo=None) <= utcnow()
    ):
        raise HTTPException(
            422, "Fact requires scope, rationale and unexpired validity"
        )
    key = stable_hash([entity.id, evidence.claim_hash, data.scope.strip()])
    previous_version = await db.scalar(
        select(BrandFact.version)
        .where(BrandFact.project_id == run.project_id, BrandFact.fact_key == key)
        .order_by(BrandFact.version.desc())
        .with_for_update()
        .limit(1)
    )
    if (previous_version or 0) != data.expected_version:
        raise HTTPException(409, "Fact version changed")
    fact = BrandFact(
        project_id=run.project_id,
        fact_key=key,
        version=data.expected_version + 1,
        entity_id=entity.id,
        identity_event_id=identity["event_id"],
        review_id=review.id,
        statement=review.source_quote,
        scope=data.scope.strip(),
        valid_from=data.valid_from.astimezone(UTC).replace(tzinfo=None),
        valid_until=data.valid_until.astimezone(UTC).replace(tzinfo=None),
        state="approved",
        reason=data.reason.strip(),
        actor_id=actor,
        idempotency_key=data.idempotency_key,
        request_hash=request_hash,
        source_ref=await citation_fact_source(db, run, evidence, review),
    )
    await add_event(db, fact)
    await request_refresh(
        db, run.project_id, run.audit_id, "approved_fact_changed", fact.id
    )
    return fact


async def withdraw_fact(db, run, fact, data, actor):
    await state_for_audit(db, run.project_id, run.audit_id)
    request_hash = stable_hash([fact.id, data.model_dump(), actor])
    previous = await db.scalar(
        select(BrandFact)
        .where(
            BrandFact.project_id == run.project_id,
            BrandFact.idempotency_key == data.idempotency_key,
        )
        .with_for_update()
    )
    if previous:
        if previous.request_hash != request_hash:
            raise HTTPException(409, "Withdrawal key conflict")
        return previous
    latest = await db.scalar(
        select(BrandFact)
        .where(
            BrandFact.project_id == run.project_id, BrandFact.fact_key == fact.fact_key
        )
        .order_by(BrandFact.version.desc())
        .with_for_update()
        .limit(1)
    )
    if (
        latest.id != fact.id
        or latest.version != data.expected_version
        or len(data.reason.strip()) < 5
    ):
        raise HTTPException(409, "Fact version changed or rationale missing")
    new = BrandFact(
        project_id=run.project_id,
        fact_key=fact.fact_key,
        version=fact.version + 1,
        entity_id=fact.entity_id,
        identity_event_id=fact.identity_event_id,
        review_id=fact.review_id,
        source_kind=fact.source_kind,
        target_id=fact.target_id,
        registry_brand_id=fact.registry_brand_id,
        owned_asset_revision_id=fact.owned_asset_revision_id,
        owned_asset_version=fact.owned_asset_version,
        source_ref=fact.source_ref,
        statement=fact.statement,
        scope=fact.scope,
        valid_from=fact.valid_from,
        valid_until=fact.valid_until,
        state="withdrawn",
        reason=data.reason.strip(),
        actor_id=actor,
        idempotency_key=data.idempotency_key,
        request_hash=request_hash,
    )
    await add_event(db, new)
    await request_refresh(
        db, run.project_id, run.audit_id, "approved_fact_changed", new.id
    )
    return new


async def fact_view(db, fact):
    from app.intelligence.owned_facts import fact_status, owned_fact_fresh

    if fact.source_kind == "owned_asset_revision":
        return await fact_status(db, fact, await owned_fact_fresh(db, fact), now=utcnow())
    review = await db.scalar(
        select(ClaimReview).where(
            ClaimReview.project_id == fact.project_id, ClaimReview.id == fact.review_id
        )
    )
    run = await db.scalar(
        select(IntelligenceRun).where(
            IntelligenceRun.project_id == fact.project_id,
            IntelligenceRun.id == review.run_id,
        )
    )
    latest = (await review_snapshot(db, fact.project_id, run.id)).get(
        review.evidence_id
    )
    identity = (await identity_snapshot(db, fact.project_id)).get(fact.entity_id)
    evidence = await db.scalar(
        select(ClaimEvidence).where(
            ClaimEvidence.project_id == fact.project_id,
            ClaimEvidence.id == review.evidence_id,
        )
    )
    try:
        _, citation, proof = await evidence_material(db, run, evidence)
        fetch = await db.scalar(
            select(SourceFetch)
            .where(
                SourceFetch.project_id == fact.project_id,
                SourceFetch.audit_id == run.audit_id,
                SourceFetch.document_id == citation["document_id"],
            )
            .order_by(SourceFetch.created_at.desc(), SourceFetch.id.desc())
            .limit(1)
        )
        fresh = proof == review.evidence_hash and (
            not fetch or fetch.snapshot_id == citation["snapshot_id"]
        )
    except HTTPException:
        fresh = False
    fresh = (
        fresh
        and await context_snapshot(db, fact.project_id)
        == run.manifest.get("brand_context")
        and await binding_snapshot(db, fact.project_id)
        == run.manifest.get("mention_bindings", {})
        and await alias_snapshot(db, fact.project_id)
        == run.manifest.get("entity_aliases", {})
        and latest["id"] == review.id
        and review.relation == "supports"
        and identity
        and identity["event_id"] == fact.identity_event_id
        and identity["decision"] == "confirmed"
    )
    return await fact_status(db, fact, fresh, now=utcnow())


async def citation_fact_source(db, run, evidence, review):
    source, citation, proof = await evidence_material(db, run, evidence)
    if proof != review.evidence_hash or review.relation != "supports":
        raise HTTPException(409, "Reviewed source no longer valid")
    start = source.text.find(review.source_quote)
    if start < 0 or source.text.find(review.source_quote, start + 1) >= 0:
        raise HTTPException(422, "Choose a uniquely located source quotation")
    snapshot = await db.get(SourceSnapshot, source.owner_id)
    return TypeAdapter(FactSource).validate_python({
        "kind": "citation_review", "review_id": review.id, "evidence_id": evidence.id,
        "citation_id": citation["id"], "snapshot_id": source.owner_id,
        "evidence": {"project_id": run.project_id, "run_id": run.id,
                     "owner_kind": "snapshot", "owner_id": source.owner_id,
                     "segment_id": source.id, "text_hash": snapshot.body_hash,
                     "quote": review.source_quote, "start": source.start_offset + start,
                     "end": source.start_offset + start + len(review.source_quote)},
    }).model_dump()
