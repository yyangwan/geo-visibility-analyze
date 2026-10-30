"""Task-local dependency projections. New runs and unrelated edits are not revocation."""

from copy import deepcopy

from fastapi import HTTPException
from sqlalchemy import select

from app.config import settings
from app.intelligence.aliases import alias_snapshot
from app.intelligence.blob import digest
from app.intelligence.evaluation_reviews import latest_cohort_review
from app.intelligence.identity import identity_snapshot
from app.intelligence.mention_bindings import binding_key, binding_snapshot
from app.intelligence.models import (
    ClaimEvidence,
    ContentSegment,
    EntityMention,
    OwnedContentAsset,
    SourceFetch,
    SourceSnapshot,
)
from app.intelligence.project_context import context_snapshot
from app.intelligence.semantics import analysis_signature, stable_hash
from app.intelligence.value_contracts import EvidenceRef, TaskPackV2

VERSION = "action-dependencies-v1"


async def scalar(db, query, lock=False):
    return await db.scalar(
        query.with_for_update().execution_options(populate_existing=True)
        if lock
        else query
    )


def identity_projection(context, identities, aliases, bindings, scope):
    """Include transitive alias edges and name collisions, not the entire registry."""
    entities = set(scope["entity_ids"])
    selected_aliases = {}
    for _ in range(65):
        edges = {
            key: value
            for key, value in aliases.items()
            if key in entities or value.get("target_entity_id") in entities
        }
        expanded = (
            entities
            | set(edges)
            | {
                row["target_entity_id"]
                for row in edges.values()
                if row.get("target_entity_id")
            }
        )
        selected_aliases.update(edges)
        if expanded == entities:
            break
        entities = expanded
    else:
        raise HTTPException(409, "Identity dependency chain exceeds supported bounds")
    brands = (context or {}).get("brands", [])
    chosen = [
        row for row in brands if "registry:" + str(row["id"]) in scope["target_ids"]
    ]
    names = {
        name.strip().casefold()
        for row in chosen
        for name in [row["name"], *(row.get("aliases") or [])]
        if name.strip()
    }
    collisions = [
        row
        for row in brands
        if row not in chosen
        and (
            row.get("entity_id") in entities
            or names.intersection(
                name.strip().casefold()
                for name in [row["name"], *(row.get("aliases") or [])]
            )
        )
    ]
    return {
        "brands": sorted(chosen, key=lambda row: row["id"]),
        "name_collisions": sorted(collisions, key=lambda row: row["id"]),
        "identities": {key: identities.get(key) for key in sorted(entities)},
        "aliases": {key: selected_aliases[key] for key in sorted(selected_aliases)},
        "bindings": {key: bindings.get(key) for key in scope["binding_keys"]},
    }


async def scoped_identity(
    db, run, entity_ids, segment_ids=(), target_ids=(), *, lock=False
):
    observations = (
        (
            await db.execute(
                select(EntityMention, ContentSegment)
                .join(
                    ContentSegment,
                    (EntityMention.project_id == ContentSegment.project_id)
                    & (EntityMention.segment_id == ContentSegment.id),
                )
                .where(
                    EntityMention.project_id == run.project_id,
                    EntityMention.run_id == run.id,
                    EntityMention.segment_id.in_(segment_ids),
                )
            )
        ).all()
        if segment_ids
        else []
    )
    scope = {
        "entity_ids": sorted(
            set(entity_ids) | {row.entity_id for row, _ in observations}
        ),
        "binding_keys": sorted(
            binding_key(row, segment) for row, segment in observations
        ),
        "target_ids": sorted(set(target_ids)),
    }
    frozen = identity_projection(
        run.manifest.get("brand_context"),
        run.manifest.get("entity_resolutions", {}),
        run.manifest.get("entity_aliases", {}),
        run.manifest.get("mention_bindings", {}),
        scope,
    )
    current = identity_projection(
        await context_snapshot(db, run.project_id, lock=lock),
        await identity_snapshot(db, run.project_id, lock=lock),
        await alias_snapshot(db, run.project_id, lock=lock),
        await binding_snapshot(db, run.project_id, lock=lock),
        scope,
    )
    return scope, frozen, current


def question_projection(cohort, keys):
    return {
        platform + ":" + prompt: next(
            (
                {
                    key: row.get(key)
                    for key in (
                        "prompt",
                        "prompt_hash",
                        "platform",
                        "category",
                        "eligibility",
                        "eligibility_reason",
                        "primary_scenario",
                        "question_context",
                    )
                }
                for row in (cohort or {}).get("cells", [])
                if row["platform"] == platform and row["prompt_hash"] == prompt
            ),
            None,
        )
        for platform, prompt in keys
    }


async def current_questions(db, run, keys, *, lock=False):
    review = await latest_cohort_review(db, run.project_id, run.audit_id, lock=lock)
    if review:
        base = (
            run.manifest.get("evaluation_base_cohort")
            or run.manifest.get("evaluation_cohort")
            or {}
        )
        if review.base_hash != base.get("base_hash", base.get("hash")):
            return {"incompatible_base": review.base_hash}
        return question_projection(review.cohort, keys)
    return question_projection(run.manifest.get("evaluation_cohort"), keys)


async def selected_fact(db, project, fact_id, *, lock=False):
    from app.intelligence.fact_qualification import qualified_fact

    return await qualified_fact(db, project, fact_id, lock=lock)


async def latest_fetch(db, run, source, *, lock=False):
    return (
        await scalar(
            db,
            select(SourceFetch)
            .where(
                SourceFetch.project_id == run.project_id,
                SourceFetch.audit_id == run.audit_id,
                SourceFetch.document_id == source.get("document_id"),
            )
            .order_by(SourceFetch.created_at.desc(), SourceFetch.id.desc())
            .limit(1),
            lock,
        )
        if source.get("document_id")
        else None
    )


async def material_dependency(db, project, revision_id, *, lock=False):
    from app.intelligence.owned_assets import latest_asset, material_hash
    from app.intelligence.owned_facts import owned_material_fresh

    row = await scalar(
        db,
        select(OwnedContentAsset).where(
            OwnedContentAsset.project_id == project, OwnedContentAsset.id == revision_id
        ),
        lock,
    )
    if not row:
        return {"id": revision_id, "status": "missing"}
    latest = await latest_asset(db, project, row.asset_key, lock=lock)
    return {
        "id": row.id,
        "version": row.version,
        "latest_id": latest.id,
        "hash": material_hash(row),
        "verification": deepcopy(row.verification),
        "usable": await owned_material_fresh(db, row, lock=lock),
    }


def ref_key(ref):
    return stable_hash(ref)


async def evidence_projection(db, run, refs):
    """Recheck complete owner text, exact Unicode span and frozen source association."""
    values = {}
    owners = {}
    for raw in refs:
        ref = EvidenceRef.model_validate(raw)
        valid = (
            ref.project_id == run.project_id
            and ref.run_id == run.id
            and ref.owner_kind != "owned_asset"
        )
        owner = (ref.owner_kind, ref.owner_id)
        if owner not in owners:
            anchor_segment = await db.scalar(
                select(ContentSegment).where(
                    ContentSegment.project_id == run.project_id,
                    ContentSegment.id == ref.segment_id,
                )
            )
            segments = (
                await db.scalars(
                    select(ContentSegment)
                    .where(
                        ContentSegment.project_id == run.project_id,
                        ContentSegment.owner_kind == ref.owner_kind,
                        ContentSegment.owner_id == ref.owner_id,
                        ContentSegment.extractor_version
                        == (anchor_segment.extractor_version if anchor_segment else ""),
                    )
                    .order_by(ContentSegment.start_offset, ContentSegment.ordinal)
                )
            ).all()
            # A frozen owner has one extraction version; overlapping alternate versions are rejected.
            position, parts = 0, []
            for row in segments:
                if row.start_offset != position or row.end_offset - position != len(
                    row.text
                ):
                    break
                parts.append(row.text)
                position = row.end_offset
            else:
                owners[owner] = ("".join(parts), {row.id: row for row in segments})
            owners.setdefault(owner, (None, {}))
        text, segments = owners[owner]
        segment = segments.get(ref.segment_id)
        valid = bool(
            valid
            and segment
            and text is not None
            and digest(text.encode()) == ref.text_hash
            and segment.start_offset <= ref.start < ref.end <= segment.end_offset
            and text[ref.start : ref.end] == ref.quote
        )
        if ref.owner_kind == "answer":
            response = next(
                (row for row in run.manifest["responses"] if row["id"] == ref.owner_id),
                None,
            )
            valid = valid and bool(response and response["text"] == text)
        elif ref.owner_kind == "citation":
            citation = next(
                (row for row in run.manifest["sources"] if row["id"] == ref.owner_id),
                None,
            )
            valid = valid and bool(
                citation
                and "\n".join(
                    value
                    for value in (citation.get("title"), citation.get("snippet"))
                    if value
                )
                == text
            )
        else:
            from app.intelligence.citation_context import snapshot_matches

            snapshot = await db.scalar(
                select(SourceSnapshot).where(
                    SourceSnapshot.project_id == run.project_id,
                    SourceSnapshot.id == ref.owner_id,
                )
            )
            sources = [
                row
                for row in run.manifest["sources"]
                if row.get("snapshot_id") == ref.owner_id
            ]
            valid = valid and bool(
                snapshot
                and snapshot.integrity_status == "verified"
                and sources
                and all(snapshot_matches(row, snapshot) for row in sources)
            )
        values[ref_key(raw)] = {"reference": raw, "valid": bool(valid)}
    return values


async def task_scope(db, run, target, *, lock=False):
    pack = (
        TaskPackV2.model_validate(target["task_pack"])
        if target.get("task_pack")
        else None
    )
    refs = (
        [ref.model_dump(mode="json") for ref in pack.diagnostic_evidence]
        if pack
        else []
    )
    refs += target.get("counterevidence", [])
    responses = set(target.get("response_ids", [])) | {
        ref["owner_id"] for ref in refs if ref["owner_kind"] == "answer"
    }
    keys = (
        {(row.platform, row.prompt_hash) for row in pack.ai_targets} if pack else set()
    )
    if not responses:
        responses = {
            row["id"]
            for row in run.manifest["responses"]
            if row["platform"] == target.get("platform")
            and (
                not target.get("prompt_hash")
                or row["prompt_hash"] == target["prompt_hash"]
            )
        }
    sources = set(target.get("source_ids", [])) | {
        ref["owner_id"] for ref in refs if ref["owner_kind"] == "citation"
    }
    sources |= {
        row["id"]
        for row in run.manifest["sources"]
        if row.get("snapshot_id")
        in {ref["owner_id"] for ref in refs if ref["owner_kind"] == "snapshot"}
    }
    responses |= {
        row["response_id"] for row in run.manifest["sources"] if row["id"] in sources
    }
    keys |= {
        (row["platform"], row["prompt_hash"])
        for row in run.manifest["responses"]
        if row["id"] in responses
    }
    segments = (
        await db.scalars(
            select(ContentSegment.id).where(
                ContentSegment.project_id == run.project_id,
                ContentSegment.owner_kind == "answer",
                ContentSegment.owner_id.in_(responses),
            )
        )
    ).all()
    entity_ids = {
        value
        for key, value in target.items()
        if key in ("own_entity_id", "comparison_entity_id", "entity_id") and value
    }
    target_ids = set(target.get("comparison_target_ids", [])) | (
        {pack.own_target_id} if pack else set()
    )
    entity_ids |= {key[7:] for key in target_ids if key.startswith("entity:")}
    entity_ids |= {
        row["binding_entity_id"]
        for row in run.manifest.get("brand_targets", [])
        if row["target_id"] in target_ids and row.get("binding_entity_id")
    }
    identity_scope, frozen, current = await scoped_identity(
        db, run, entity_ids, segments, target_ids, lock=lock
    )
    related_evidence = (
        (
            await db.scalars(
                select(ClaimEvidence.id).where(
                    ClaimEvidence.project_id == run.project_id,
                    ClaimEvidence.run_id == run.id,
                    ClaimEvidence.citation_id.in_(sources),
                    ClaimEvidence.answer_segment_id.in_(segments),
                )
            )
        ).all()
        if sources and segments
        else []
    )
    return {
        "responses": sorted(responses),
        "sources": sorted(sources),
        "refs": list({ref_key(ref): ref for ref in refs}.values()),
        "questions": sorted(keys),
        "identity": identity_scope,
        "frozen_identity": frozen,
        "current_identity": current,
        "evidence_ids": sorted(
            set(target.get("evidence_ids", [])) | set(related_evidence)
        ),
        "fact_ids": list(pack.own_fact_ids) if pack else [],
        "material_id": pack.delivery.material_revision_id if pack else None,
    }


async def dependency_projection(db, run, target, scope, *, current, lock=False):
    from app.intelligence.citation_context import snapshot_version
    from app.intelligence.reviews import review_snapshot

    _, base_url, model = settings.get_analysis_llm_config()
    parts = {
        "interpretation": analysis_signature(model, base_url)
        if current
        else run.signature,
        "identity": identity_projection(
            await context_snapshot(db, run.project_id, lock=lock),
            await identity_snapshot(db, run.project_id, lock=lock),
            await alias_snapshot(db, run.project_id, lock=lock),
            await binding_snapshot(db, run.project_id, lock=lock),
            scope["identity"],
        )
        if current
        else scope["frozen_identity"],
        "questions": await current_questions(db, run, scope["questions"], lock=lock)
        if current
        else question_projection(
            run.manifest.get("evaluation_cohort"), scope["questions"]
        ),
        "evidence": await evidence_projection(db, run, scope["refs"]),
    }
    # A newer run never changes this frozen observation. An in-place mutation does.
    parts["run"] = {
        "id": run.id,
        "input_hash": run.input_hash,
        "manifest_hash": stable_hash(run.manifest),
        "published": run.status in ("ready", "partial"),
    }
    for source in run.manifest["sources"]:
        if source["id"] not in scope["sources"]:
            continue
        value = {
            "frozen": source,
            "snapshot_version": source.get("snapshot_version"),
            "latest_snapshot_id": source.get("snapshot_id"),
            "quality_hash": stable_hash(source.get("quality", {})),
        }
        snapshot = None
        if source.get("snapshot_id"):
            snapshot = await scalar(
                db,
                select(SourceSnapshot).where(
                    SourceSnapshot.project_id == run.project_id,
                    SourceSnapshot.id == source["snapshot_id"],
                ),
                lock,
            )
            value["storage_fingerprint"] = (
                stable_hash([snapshot.text_key, snapshot.text_sha256])
                if snapshot
                else None
            )
        if current:
            if source.get("snapshot_id"):
                value["snapshot_version"] = (
                    snapshot_version(snapshot) if snapshot else None
                )
                value["quality_hash"] = (
                    stable_hash(snapshot.quality) if snapshot else None
                )
            fetch = await latest_fetch(db, run, source, lock=lock)
            value["latest_snapshot_id"] = (
                fetch.snapshot_id if fetch else source["snapshot_id"]
            )
        parts["source:" + source["id"]] = value
    reviews = (
        await review_snapshot(db, run.project_id, run.id, lock=lock)
        if current
        else target.get("dependency_reviews", {})
    )
    for key in scope["evidence_ids"]:
        parts["review:" + key] = reviews.get(key)
    for key in scope["fact_ids"]:
        parts["fact:" + key] = await selected_fact(db, run.project_id, key, lock=lock)
    if scope["material_id"]:
        parts["material:" + scope["material_id"]] = await material_dependency(
            db, run.project_id, scope["material_id"], lock=lock
        )
    return parts


def dependency_changes(before, after):
    return [
        {
            "dependency": key,
            "before": before.get(key),
            "after": after.get(key),
            "reason": "Relevant dependency changed; review a new revision",
        }
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    ]


async def freeze_dependencies(db, run, target, *, lock=False):
    from app.intelligence.reviews import review_snapshot

    scope = await task_scope(db, run, target, lock=lock)
    # Human review status is captured at plan revision time; it is not silently re-signed.
    target_for_snapshot = {
        **target,
        "dependency_reviews": await review_snapshot(
            db, run.project_id, run.id, lock=lock
        ),
    }
    before = await dependency_projection(
        db, run, target_for_snapshot, scope, current=False, lock=lock
    )
    after = await dependency_projection(
        db, run, target_for_snapshot, scope, current=True, lock=lock
    )
    errors = dependency_changes(before, after)
    if not before["run"]["published"] or any(
        not row["valid"] for row in before["evidence"].values()
    ):
        errors.append(
            {
                "dependency": "evidence_integrity",
                "reason": "Published exact evidence required",
            }
        )
    for key, value in before.items():
        if (
            key.startswith("fact:")
            and value.get("status") != "valid"
            or key.startswith("material:")
            and not value.get("usable")
        ):
            errors.append(
                {
                    "dependency": key,
                    "reason": "Selected own material or fact is not valid",
                }
            )
    if errors:
        raise HTTPException(
            409, {"code": "action_dependencies_changed", "changes": errors}
        )
    scope.pop("current_identity")
    return {"version": VERSION, "scope": scope, "expected": before}


async def check_dependencies(db, run, target, dependencies, *, lock=False):
    if dependencies.get("version") != VERSION:
        return [{"dependency": "policy", "reason": "Unsupported dependency policy"}]
    current = await dependency_projection(
        db, run, target, dependencies["scope"], current=True, lock=lock
    )
    return dependency_changes(dependencies["expected"], current)
