"""Fenced context jobs; one snapshot may have different question interpretations."""

from pydantic import ValidationError
from sqlalchemy import select

from app.intelligence.contextual_analysis import (
    SYSTEM,
    checked_context,
    context_payload,
    context_signature,
    encoded_payload,
    frozen_context,
)
from app.intelligence.model_policy import structured_options
from app.intelligence.models import (
    AnalysisJob,
    ContentSegment,
    ContextAnalysis,
    SegmentAnalysis,
    SemanticCache,
)
from app.intelligence.semantics import SemanticOutputError, stable_hash
from app.intelligence.source_quality import usable_snapshot


async def analyze_run_contexts(pipeline, lease, manifest, api_key, base_url, model):
    signature = context_signature(model, base_url)
    responses = {row["id"]: row for row in manifest["responses"] if not row["error"]}
    async with pipeline.queue.sessions() as db:
        applications = (
            await db.execute(
                select(SegmentAnalysis, ContentSegment, SemanticCache)
                .join(ContentSegment, SegmentAnalysis.segment_id == ContentSegment.id)
                .outerjoin(SemanticCache, SegmentAnalysis.cache_id == SemanticCache.id)
                .where(
                    SegmentAnalysis.project_id == lease.project_id,
                    SegmentAnalysis.run_id == lease.run_id,
                    ContentSegment.project_id == lease.project_id,
                    ContentSegment.owner_kind.in_(("answer", "snapshot")),
                )
                .order_by(
                    ContentSegment.owner_kind,
                    ContentSegment.owner_id,
                    ContentSegment.ordinal,
                )
            )
        ).all()
    for application, segment, cache in applications:
        if segment.owner_kind == "answer":
            scopes = (
                [(responses[segment.owner_id], None)]
                if segment.owner_id in responses
                else []
            )
        else:
            scopes = [
                (responses[source["response_id"]], source)
                for source in manifest["sources"]
                if source["snapshot_id"] == segment.owner_id
                and usable_snapshot(source)
                and source["response_id"] in responses
            ]
        for response, source in scopes:
            context = frozen_context(manifest, response, source=source)
            result = cache.result if cache else {}
            context_hash = stable_hash(
                {
                    "context": context,
                    "start": segment.start_offset,
                    "end": segment.end_offset,
                }
            )
            input_hash = stable_hash([segment.text, result])
            operation_id = stable_hash([segment.id, context_hash, signature])
            async with pipeline.queue.sessions() as db:
                existing = await db.scalar(
                    select(ContextAnalysis).where(
                        ContextAnalysis.project_id == lease.project_id,
                        ContextAnalysis.run_id == lease.run_id,
                        ContextAnalysis.owner_kind == segment.owner_kind,
                        ContextAnalysis.owner_id == segment.owner_id,
                        ContextAnalysis.context_hash == context_hash,
                        ContextAnalysis.signature == signature,
                    )
                )
                if existing and existing.status in ("completed", "failed"):
                    continue
                reused = await db.scalar(
                    select(ContextAnalysis)
                    .where(
                        ContextAnalysis.project_id == lease.project_id,
                        ContextAnalysis.run_id != lease.run_id,
                        ContextAnalysis.input_hash == input_hash,
                        ContextAnalysis.context_hash == context_hash,
                        ContextAnalysis.signature == signature,
                        ContextAnalysis.status == "completed",
                    )
                    .order_by(ContextAnalysis.id)
                    .limit(1)
                )
            payload = context_payload(segment, result, context)
            receipt, usage, value = None, {}, None
            reason, status = None, "deferred"
            if application.status not in ("completed", "partial") or not isinstance(
                result.get("information_units"), list
            ):
                reason = "context_extraction_prerequisite_missing"
            elif reused:
                value, status = reused.result["interpretation"], "completed"
            elif (
                not payload["candidates"]
                and not payload["units"]
                and not result.get("rejected_spans")
            ):
                value = checked_context(
                    payload, {"subjects": [], "requirement_matches": []}
                )
                status = "completed"
            elif not api_key:
                reason = "analysis_provider_unconfigured"
            else:
                receipt = await pipeline.reserve_model_request(
                    lease,
                    phase="context",
                    messages=[SYSTEM, encoded_payload(payload)],
                    max_output=structured_options(base_url, model)["max_tokens"],
                    subject_id=operation_id,
                )
                if receipt:
                    try:
                        raw, usage = await pipeline.contextualize(
                            payload, api_key, base_url, model
                        )
                        value, status = checked_context(payload, raw), "completed"
                    except (SemanticOutputError, ValidationError) as error:
                        usage = getattr(error, "usage", usage)
                        status, reason = "failed", "context_output_invalid"
                    except Exception:
                        await pipeline.settle_model_request(lease, receipt, None)
                        raise
                else:
                    reason = "context_budget_deferred"

            async def save(
                db,
                receipt=receipt,
                usage=usage,
                reason=reason,
                status=status,
                operation_id=operation_id,
                segment=segment,
                context=context,
                context_hash=context_hash,
                input_hash=input_hash,
                value=value,
                reused=reused,
                application=application,
                result=result,
            ):
                if receipt:
                    await pipeline.record_model_usage(db, lease, receipt, usage)
                if reason and reason != "context_budget_deferred":
                    await pipeline.record_model_deferral(
                        db, lease, "context", operation_id, reason
                    )
                elif status == "completed":
                    job = await db.get(AnalysisJob, lease.id)
                    deferred = {**job.payload.get("model_deferred", {})}
                    deferred.pop(stable_hash(["context", operation_id]), None)
                    job.payload = {**job.payload, "model_deferred": deferred}
                row = await db.scalar(
                    select(ContextAnalysis).where(
                        ContextAnalysis.project_id == lease.project_id,
                        ContextAnalysis.run_id == lease.run_id,
                        ContextAnalysis.owner_kind == segment.owner_kind,
                        ContextAnalysis.owner_id == segment.owner_id,
                        ContextAnalysis.context_hash == context_hash,
                        ContextAnalysis.signature == signature,
                    )
                )
                if row is None:
                    row = ContextAnalysis(
                        project_id=lease.project_id,
                        run_id=lease.run_id,
                        owner_kind=segment.owner_kind,
                        owner_id=segment.owner_id,
                        context_hash=context_hash,
                        signature=signature,
                        input_hash=input_hash,
                    )
                    db.add(row)
                row.status, row.error_code, row.usage = status, reason, usage
                row.result = {
                    "segment_id": segment.id,
                    "segment_start": segment.start_offset,
                    "extraction_status": application.status,
                    "extraction_rejected_candidates": result.get("rejected_spans", 0),
                    "context": context,
                    "interpretation": value,
                    "cache_origin_id": reused.id
                    if reused and status == "completed"
                    else None,
                }

            await pipeline.queue.checkpoint(lease, save)


async def context_projection(db, run):
    rows = (
        await db.scalars(
            select(ContextAnalysis)
            .where(
                ContextAnalysis.project_id == run.project_id,
                ContextAnalysis.run_id == run.id,
            )
            .order_by(
                ContextAnalysis.owner_kind, ContextAnalysis.owner_id, ContextAnalysis.id
            )
        )
    ).all()
    observations = [
        {
            "id": row.id,
            "owner_kind": row.owner_kind,
            "owner_id": row.owner_id,
            "status": row.status,
            "error_code": row.error_code,
            "context_hash": row.context_hash,
            "signature": row.signature,
            **row.result,
        }
        for row in rows
    ]
    completed = sum(row.status == "completed" for row in rows)
    return {
        "version": "context-analysis-v1",
        "expected": len(rows),
        "completed": completed,
        "failed": sum(row.status == "failed" for row in rows),
        "deferred": sum(row.status == "deferred" for row in rows),
        "status": "processed"
        if rows and len(rows) == completed
        else "partial"
        if rows
        else "pending",
        "observations": observations,
    }


def annotate_subjects(entities, mentions, projection):
    """Semantic participation is distinct from server-owned registry identity."""
    from collections import defaultdict

    index, roles_by_entity, answers_by_entity = (
        defaultdict(list),
        defaultdict(set),
        defaultdict(lambda: defaultdict(set)),
    )
    for row in projection["observations"]:
        if row["status"] != "completed":
            continue
        for subject in row["interpretation"]["subjects"]:
            key = (
                row["segment_id"],
                subject["observed_name"],
                row["segment_start"] + subject["candidate_start"],
                row["segment_start"] + subject["candidate_end"],
            )
            index[key].append(
                {
                    "context_analysis_id": row["id"],
                    "context_role": subject["context_role"],
                    "observation": row["context"]["observation"],
                    "owner_kind": row.get("owner_kind"),
                    "review_state": "unreviewed",
                }
            )
    for mention in mentions:
        key = (
            mention["segment_id"],
            mention["observed_entity_name"],
            mention["start"],
            mention["end"],
        )
        interpretations = index.get(key, [])
        mention["context_interpretations"] = interpretations
        for interpretation in interpretations:
            roles_by_entity[mention["entity_id"]].add(interpretation["context_role"])
            if interpretation["owner_kind"] == "answer":
                answers_by_entity[mention["entity_id"]][
                    interpretation["observation"]["response_id"]
                ].add(interpretation["context_role"])
    for entity in entities:
        roles = roles_by_entity[entity["id"]]
        entity["context_roles"] = sorted(roles)
        entity["answer_context_roles"] = {
            key: sorted(value) for key, value in answers_by_entity[entity["id"]].items()
        }
        entity["comparison_context_status"] = (
            "eligible"
            if "evaluated_solution" in roles
            else "not_solution"
            if roles and "uncertain" not in roles
            else "unknown"
        )
