"""One current source/identity qualification for facts; no semantic entailment claim."""

import gzip

from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import or_, select

from app.intelligence.models import (
    BrandFact,
    ClaimEvidence,
    ClaimReview,
    IntelligenceRun,
    SourceSnapshot,
)
from app.intelligence.value_contracts import FactSource, TaskPackV2

POLICY = "fact-qualification-v1"


async def verify_citation_bytes(snapshot, source, blobs=None):
    from app.intelligence.blob import IntegrityError, configured_store, digest

    try:
        data = await (blobs or configured_store()).get(
            snapshot.text_key, snapshot.text_sha256
        )
    except (IntegrityError, FileNotFoundError) as error:
        raise HTTPException(409, "Saved citation material integrity changed") from error
    except Exception as error:
        raise HTTPException(
            503, "Citation material storage is temporarily unavailable"
        ) from error
    try:
        text = gzip.decompress(data).decode("utf-8")
    except (OSError, EOFError, UnicodeError) as error:
        raise HTTPException(409, "Saved citation text cannot be verified") from error
    if (
        digest(text.encode()) != source.evidence.text_hash
        or text[source.evidence.start : source.evidence.end] != source.evidence.quote
    ):
        raise HTTPException(409, "Saved citation quotation changed")


async def qualified_fact(db, project, fact_id, *, lock=False, read=False, blobs=None):
    from app.intelligence.action_dependencies import (
        latest_fetch,
        scalar,
        scoped_identity,
    )
    from app.intelligence.owned_facts import fact_status, owned_fact_fresh
    from app.intelligence.reviews import (
        citation_fact_source,
        evidence_material,
        review_snapshot,
    )

    fact = await scalar(
        db,
        select(BrandFact).where(
            BrandFact.project_id == project, BrandFact.id == fact_id
        ),
        lock,
    )
    if fact is None:
        return {
            "id": fact_id,
            "status": "missing",
            "qualification": {
                "policy": POLICY,
                "source_valid": False,
                "semantic_fit": "not_verified",
            },
        }
    fresh = False
    try:
        source = TypeAdapter(FactSource).validate_python(fact.source_ref)
        bound = (
            source.kind == fact.source_kind
            and source.evidence.project_id == project
            and source.evidence.quote == fact.statement
        )
        if bound and source.kind == "owned_asset_revision":
            fresh = await owned_fact_fresh(db, fact, lock=lock, read=read, blobs=blobs)
        elif bound and source.kind == "citation_review":
            review = await scalar(
                db,
                select(ClaimReview).where(
                    ClaimReview.project_id == project, ClaimReview.id == fact.review_id
                ),
                lock,
            )
            origin = (
                await scalar(
                    db,
                    select(IntelligenceRun).where(
                        IntelligenceRun.project_id == project,
                        IntelligenceRun.id == review.run_id,
                    ),
                    lock,
                )
                if review
                else None
            )
            evidence = (
                await scalar(
                    db,
                    select(ClaimEvidence).where(
                        ClaimEvidence.project_id == project,
                        ClaimEvidence.id == review.evidence_id,
                    ),
                    lock,
                )
                if review
                else None
            )
            if origin and evidence:
                _, citation, proof = await evidence_material(db, origin, evidence)
                _, frozen, current = await scoped_identity(
                    db,
                    origin,
                    [fact.entity_id],
                    [evidence.answer_segment_id, evidence.source_segment_id],
                    lock=lock,
                )
                identity = current["identities"].get(fact.entity_id) or {}
                latest = (await review_snapshot(db, project, origin.id, lock=lock)).get(
                    evidence.id
                ) or {}
                fetch = await latest_fetch(db, origin, citation, lock=lock)
                canonical = await citation_fact_source(db, origin, evidence, review)
                snapshot = await scalar(
                    db,
                    select(SourceSnapshot).where(
                        SourceSnapshot.project_id == project,
                        SourceSnapshot.id == source.snapshot_id,
                    ),
                    lock,
                )
                fresh = bool(
                    canonical == fact.source_ref
                    and proof == review.evidence_hash
                    and frozen == current
                    and latest.get("id") == review.id
                    and review.relation == "supports"
                    and bool(review.actor_id)
                    and identity.get("event_id") == fact.identity_event_id
                    and identity.get("decision") == "confirmed"
                    and (not fetch or fetch.snapshot_id == citation.get("snapshot_id"))
                    and snapshot
                    and snapshot.quality.get("evidence_usable") is not False
                )
                if fresh and read:
                    await verify_citation_bytes(snapshot, source, blobs)
    except HTTPException as error:
        if error.status_code >= 500:
            raise
        fresh = False
    except ValidationError:
        fresh = False
    value = await fact_status(db, fact, fresh, lock=lock)
    return {
        **value,
        "qualification": {
            "policy": POLICY,
            "source_valid": fresh,
            "semantic_fit": "not_verified",
        },
    }


async def task_fact(db, run, pack, fact_id, *, lock=False, read=False, blobs=None):
    """Ownership and current qualification do not prove a fact satisfies the task."""
    from app.intelligence.owned_assets import own_brand

    value = await qualified_fact(
        db, run.project_id, fact_id, lock=lock, read=read, blobs=blobs
    )
    own = next(
        (
            row
            for row in run.manifest.get("brand_targets", [])
            if row["target_id"] == pack.own_target_id and row["role"] == "own"
        ),
        None,
    )
    own_current = False
    if own and own.get("registry_brand_id"):
        try:
            current = await own_brand(
                db, run.project_id, own["registry_brand_id"], lock=lock
            )
            own_current = current.get("binding_entity_id") == own.get(
                "binding_entity_id"
            )
        except HTTPException:
            pass
    matches = False
    if own and own_current:
        if value.get("source_kind") == "owned_asset_revision":
            matches = value.get("target_id") == pack.own_target_id and value.get(
                "registry_brand_id"
            ) == own.get("registry_brand_id")
        elif value.get("source_kind") == "citation_review":
            matches = bool(
                own.get("binding_entity_id")
                and value.get("entity_id") == own["binding_entity_id"]
            )
    valid = value["status"] == "valid" and matches
    return {
        **value,
        "eligible": valid,
        "selected": fact_id in pack.own_fact_ids,
        "eligibility_reason": "same_own_brand_requires_scope_review"
        if valid
        else "own_target_mismatch"
        if not matches
        else value["status"],
        "semantic_fit": "requires_named_review",
    }


async def eligible_facts(db, action, expected_revision, *, offset=0, limit=100):
    from app.intelligence.action_revisions import current_revision, revision_integrity
    from app.intelligence.semantics import stable_hash

    revision, run = await current_revision(db, action)
    if revision.revision != expected_revision or not revision_integrity(
        action, revision
    ):
        raise HTTPException(409, "Action revision changed; refresh fact choices")
    target = revision.payload["target"]
    if not target.get("task_pack"):
        raise HTTPException(409, "Fact selection requires the current TaskPack v2")
    pack = TaskPackV2.model_validate(target["task_pack"])
    own = next(
        (
            row
            for row in run.manifest.get("brand_targets", [])
            if row["target_id"] == pack.own_target_id and row["role"] == "own"
        ),
        {},
    )
    conditions = [
        BrandFact.target_id == pack.own_target_id,
        BrandFact.id.in_(pack.own_fact_ids),
    ]
    if own.get("binding_entity_id"):
        conditions.append(BrandFact.entity_id == own["binding_entity_id"])
    rows = (
        await db.scalars(
            select(BrandFact)
            .where(BrandFact.project_id == action.project_id, or_(*conditions))
            .order_by(BrandFact.fact_key, BrandFact.version.desc(), BrandFact.id)
        )
    ).all()
    current_keys, ids = set(), []
    for row in rows:
        if row.fact_key not in current_keys or row.id in pack.own_fact_ids:
            ids.append(row.id)
        current_keys.add(row.fact_key)
    items = [await task_fact(db, run, pack, fact_id) for fact_id in ids]
    # Include missing/foreign IDs only as blocked selected entries, without foreign data.
    items += [
        await task_fact(db, run, pack, fact_id)
        for fact_id in pack.own_fact_ids
        if fact_id not in ids
    ]
    version = stable_hash([POLICY, revision.id, revision.signature, items])
    return {
        "version": POLICY,
        "action_id": action.id,
        "revision_id": revision.id,
        "revision": revision.revision,
        "projection_hash": version,
        "items": items[offset : offset + limit],
        "total": len(items),
        "next_offset": offset + limit if offset + limit < len(items) else None,
        "semantic_fit": "requires_named_review",
        "selection_is_not_content_authorization": True,
    }
