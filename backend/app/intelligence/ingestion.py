"""Freeze observations before dispatch; registration never filters analysis input."""

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.intelligence.answer_quality import is_status_only_answer
from app.intelligence.models import (
    AnalysisJob,
    AuditSample,
    CapturedResponse,
    CitationObservation,
    IntelligenceRun,
    PromptVersion,
    SamplingAudit,
    SourceDocument,
    SourceFetch,
    SourceSnapshot,
)
from app.intelligence.semantics import analysis_signature, stable_hash
from app.intelligence.sources import SourceBlocked, normalized_url
from app.models.models import Audit, PlatformResponseRecord, Prompt
from app.utils.timezone import utc_isoformat

KINDS = {
    "answer_reference",
    "search_candidate",
    "text_link",
    "named_inference",
    "unknown",
}


async def prepare_run(
    db: AsyncSession,
    project_id: str,
    audit_id: int,
    *,
    enrich: bool = True,
    retry_failed: bool = False,
) -> IntelligenceRun:
    # Serialize revisions with an existing project-owned audit row, not MAX alone.
    audit = await db.scalar(
        select(Audit)
        .where(Audit.id == audit_id, Audit.project_id == project_id)
        .with_for_update()
    )
    if audit is None:
        raise LookupError("Audit not found")
    responses = (
        await db.scalars(
            select(PlatformResponseRecord)
            .where(
                PlatformResponseRecord.audit_id == audit_id,
            )
            .order_by(PlatformResponseRecord.id)
        )
    ).all()
    observations = (
        await db.scalars(
            select(CitationObservation).where(
                CitationObservation.project_id == project_id,
                CitationObservation.audit_id == audit_id,
            )
        )
    ).all()
    existing_responses = {str(row.response_id) for row in observations}
    for response in responses:
        if str(response.id) in existing_responses:
            continue
        raw = response.raw_response or {}
        if not isinstance(raw, dict):
            raw = {}
        sources = (
            raw.get("sources")
            if isinstance(raw.get("sources"), list)
            else response.citations or []
        )
        for ordinal, source in enumerate(sources, 1):
            if not isinstance(source, dict):
                continue
            url = str(source.get("url") or "").strip()
            kind = source.get("kind", "unknown")
            if kind not in KINDS:
                kind = "unknown"
            resolution = (
                source.get("url_resolution") or source.get("urlResolution") or "unknown"
            )
            document = None
            # Candidate/inferred URLs are never silently promoted to captured sources.
            if (
                url
                and resolution == "exact"
                and kind not in ("search_candidate", "named_inference")
            ):
                try:
                    normalized = normalized_url(url)
                    url_hash = stable_hash(normalized)
                    document = await db.scalar(
                        select(SourceDocument).where(
                            SourceDocument.project_id == project_id,
                            SourceDocument.url_hash == url_hash,
                        )
                    )
                    if document is None:
                        document = SourceDocument(
                            project_id=project_id,
                            url=normalized,
                            url_hash=url_hash,
                            title=str(source.get("title") or ""),
                        )
                        try:
                            async with db.begin_nested():
                                db.add(document)
                                await db.flush()
                        except IntegrityError:
                            # Another audit may have first discovered this same URL.
                            # A current locking read sees it even at MySQL REPEATABLE READ.
                            document = await db.scalar(
                                select(SourceDocument)
                                .where(
                                    SourceDocument.project_id == project_id,
                                    SourceDocument.url_hash == url_hash,
                                )
                                .with_for_update()
                            )
                            if document is None:
                                raise
                except SourceBlocked:
                    resolution = "invalid"
            observation = CitationObservation(
                project_id=project_id,
                audit_id=audit_id,
                response_id=str(response.id),
                ordinal=ordinal,
                platform=response.platform,
                kind=kind,
                url_resolution=resolution,
                url=url or None,
                title=str(source.get("title") or source.get("page_title") or ""),
                snippet=str(source.get("snippet") or source.get("summary") or ""),
                document_id=document.id if document else None,
                anchor=source.get("answerAnchor")
                if isinstance(source.get("answerAnchor"), dict)
                else None,
            )
            db.add(observation)
            observations.append(observation)
    await db.flush()
    frozen_responses = []
    for response in responses:
        prompt = await db.scalar(
            select(Prompt).where(
                Prompt.id == response.prompt_id, Prompt.project_id == project_id
            )
        )
        frozen_responses.append(
            {
                "id": str(response.id),
                "platform": response.platform,
                "prompt_id": str(response.prompt_id),
                "prompt": prompt.text if prompt else "",
                "prompt_hash": stable_hash(prompt.text if prompt else ""),
                "text": response.response_text or "",
                "error": bool(response.error)
                or is_status_only_answer(response.response_text or ""),
                "observed_at": utc_isoformat(response.created_at),
                "conditions": {
                    key: (response.request_params or {}).get(key)
                    for key in (
                        "surface",
                        "search_mode",
                        "model_version",
                        "account_class",
                        "location",
                    )
                },
                "search_metadata": {
                    key: (response.search_metadata or {}).get(key)
                    for key in ("surface", "source_completeness", "capture_mode")
                },
            }
        )
    sources = []
    sampling = await db.scalar(
        select(SamplingAudit).where(
            SamplingAudit.project_id == project_id, SamplingAudit.audit_id == audit_id
        )
    )
    sample_statuses = {}
    evaluation_samples = []
    if sampling:
        frozen_responses = []
        samples = (
            await db.scalars(
                select(AuditSample)
                .where(
                    AuditSample.project_id == project_id,
                    AuditSample.audit_id == audit_id,
                )
                .order_by(AuditSample.id)
            )
        ).all()
        sample_statuses = {sample.id: sample.status for sample in samples}
        for sample in samples:
            version = await db.get(PromptVersion, sample.prompt_version_id)
            if not version or version.project_id != project_id:
                raise ValueError("Invalid project-scoped prompt binding")
            evaluation_samples.append({
                "sample_id": sample.id, "sample_index": sample.sample_index,
                "platform": sample.platform, "prompt_hash": version.text_hash,
                "prompt": version.text, "category": version.category,
                "prompt_version_id": version.id,
            })
            if sample.status != "sealed" or not sample.selected_response_id:
                continue
            response = await db.get(CapturedResponse, sample.selected_response_id)
            version = await db.get(PromptVersion, sample.prompt_version_id)
            if (
                not response
                or response.project_id != project_id
                or not version
                or version.project_id != project_id
            ):
                raise ValueError("Invalid project-scoped sample binding")
            payload = response.payload
            frozen_responses.append(
                {
                    "id": response.id,
                    "sample_id": sample.id,
                    "sample_index": sample.sample_index,
                    "attempt_no": response.attempt_no,
                    "platform": sample.platform,
                    "surface": sample.surface,
                    "prompt_id": str(version.prompt_id),
                    "prompt_version_id": version.id,
                    "prompt": version.text,
                    "prompt_hash": version.text_hash,
                    "text": response.text,
                    "error": is_status_only_answer(response.text),
                    "quality_reason": "ui_status_only"
                    if is_status_only_answer(response.text)
                    else None,
                    "observed_at": utc_isoformat(response.observed_at),
                    "answer_status": payload["answer"]["status"],
                    "conditions": payload["conditions"],
                    "requested_conditions": sample.conditions,
                    "source_panel": payload["sourcePanel"],
                }
            )
            if response.id in existing_responses:
                continue
            for source in payload["sources"]:
                document = None
                if (
                    source["url"]
                    and source["urlResolution"] == "exact"
                    and source["kind"] != "named_inference"
                ):
                    try:
                        normalized = normalized_url(source["url"])
                        url_hash = stable_hash(normalized)
                        document = await db.scalar(
                            select(SourceDocument).where(
                                SourceDocument.project_id == project_id,
                                SourceDocument.url_hash == url_hash,
                            )
                        )
                        if document is None:
                            document = SourceDocument(
                                project_id=project_id,
                                url=normalized,
                                url_hash=url_hash,
                                title=source["title"],
                            )
                            try:
                                async with db.begin_nested():
                                    db.add(document)
                                    await db.flush()
                            except IntegrityError:
                                document = await db.scalar(
                                    select(SourceDocument)
                                    .where(
                                        SourceDocument.project_id == project_id,
                                        SourceDocument.url_hash == url_hash,
                                    )
                                    .with_for_update()
                                )
                                if document is None:
                                    raise
                    except SourceBlocked:
                        pass
                observation = CitationObservation(
                    project_id=project_id,
                    audit_id=audit_id,
                    response_id=response.id,
                    ordinal=source["ordinal"],
                    platform=sample.platform,
                    kind=source["kind"],
                    url_resolution=source["urlResolution"],
                    url=source["url"],
                    title=source["title"],
                    snippet=source["snippet"],
                    document_id=document.id if document else None,
                    anchor=source["answerAnchor"],
                )
                db.add(observation)
                observations.append(observation)
        await db.flush()
    for observation in observations:
        snapshot, fetch = None, None
        if observation.document_id:
            fetch = await db.scalar(
                select(SourceFetch)
                .where(
                    SourceFetch.project_id == project_id,
                    SourceFetch.document_id == observation.document_id,
                    SourceFetch.audit_id == audit_id,
                )
                .order_by(SourceFetch.created_at.desc(), SourceFetch.id.desc())
                .limit(1)
            )
            if fetch and fetch.snapshot_id:
                snapshot = await db.get(SourceSnapshot, fetch.snapshot_id)
        sources.append(
            {
                "id": observation.id,
                "response_id": str(observation.response_id),
                "document_id": observation.document_id,
                "platform": observation.platform,
                "kind": observation.kind,
                "url": observation.url,
                "title": observation.title,
                "snippet": observation.snippet,
                "url_resolution": observation.url_resolution,
                "snapshot_id": snapshot.id if snapshot else None,
                "quality": snapshot.quality if snapshot else {},
                "fetch_status": fetch.status
                if fetch
                else "pending"
                if observation.document_id
                else "unavailable",
            }
        )
    from app.intelligence.aliases import alias_snapshot
    from app.intelligence.identity import identity_snapshot
    from app.intelligence.mention_bindings import binding_snapshot, split_snapshot
    from app.intelligence.project_context import context_snapshot
    from app.intelligence.reviews import fact_snapshot

    brand_context = await context_snapshot(db, project_id)
    manifest = {
        "entity_aliases": await alias_snapshot(db, project_id),
        "mention_bindings": await binding_snapshot(db, project_id),
        "entity_splits": await split_snapshot(db, project_id),
        "entity_resolutions": await identity_snapshot(db, project_id),
        "approved_facts": await fact_snapshot(db, project_id),
        "responses": frozen_responses,
        "sources": sources,
        "brands": brand_context["brands"] if brand_context else audit.brands_json or [],
        "brand_context": brand_context,
        "capture_status": audit.status.value,
        "sample_statuses": sample_statuses,
        "expected_samples": sampling.expected_samples
        if sampling
        else len(frozen_responses),
    }
    from app.intelligence.brand_presence import freeze_registered_targets

    manifest["brand_targets"] = freeze_registered_targets(manifest["brands"])
    if sampling:
        from app.intelligence.brand_evaluation import freeze_cohort

        # Source capture/identity revisions must not change the planned denominator.
        prior_manifest = await db.scalar(
            select(IntelligenceRun.manifest).where(
                IntelligenceRun.project_id == project_id,
                IntelligenceRun.audit_id == audit_id,
            ).order_by(IntelligenceRun.revision.desc()).limit(1)
        )
        manifest["evaluation_base_cohort"] = (
            (prior_manifest.get("evaluation_base_cohort") or prior_manifest.get("evaluation_cohort")) if prior_manifest else None
        ) or freeze_cohort(evaluation_samples, audit.brands_json or [])
        from app.intelligence.evaluation_reviews import latest_cohort_review

        review = await latest_cohort_review(db, project_id, audit_id)
        base = manifest["evaluation_base_cohort"]
        manifest["evaluation_cohort"] = review.cohort if review and review.base_hash == base["hash"] else base
        manifest["evaluation_review"] = {"id": review.id, "version": review.version} if review and review.base_hash == base["hash"] else None
    _, base_url, model = settings.get_analysis_llm_config()
    signature, input_hash = analysis_signature(model, base_url), stable_hash(manifest)
    previous = await db.scalar(
        select(IntelligenceRun)
        .where(
            IntelligenceRun.project_id == project_id,
            IntelligenceRun.audit_id == audit_id,
            IntelligenceRun.input_hash == input_hash,
            IntelligenceRun.signature == signature,
        )
        .execution_options(populate_existing=True)
    )
    if previous:
        if retry_failed and previous.status == "failed":
            latest = await db.scalar(
                select(IntelligenceRun.id)
                .where(
                    IntelligenceRun.project_id == project_id,
                    IntelligenceRun.audit_id == audit_id,
                )
                .order_by(IntelligenceRun.revision.desc())
                .limit(1)
            )
            jobs = (
                await db.scalars(
                    select(AnalysisJob)
                    .where(
                        AnalysisJob.project_id == project_id,
                        AnalysisJob.run_id == previous.id,
                        AnalysisJob.kind == "analyze_run",
                    )
                    .with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True)
                )
            ).all()
            if (
                latest != previous.id
                or len(jobs) != 1
                or jobs[0].status != "failed"
                or jobs[0].subject_id != previous.id
                or jobs[0].max_attempts < 1
            ):
                raise HTTPException(
                    409, "Failed analysis is not in a recoverable state"
                )
            from app.intelligence.queue import db_now

            job = jobs[0]
            now = await db_now(db)
            job.payload = {
                **job.payload,
                "manual_retry_history": [
                    *job.payload.get("manual_retry_history", []),
                    {
                        "requested_at": utc_isoformat(now),
                        "error_code": job.error_code,
                        "attempt": job.attempt,
                        "lease_version": job.lease_version,
                    },
                ],
            }
            job.lease_version += 1
            job.lease_owner = job.lease_token = job.lease_until = None
            job.status, job.attempt, job.error_code = "queued", 0, None
            job.available_at = now
            previous.status, previous.finished_at = "queued", None
        return previous
    revision = (
        await db.scalar(
            select(func.max(IntelligenceRun.revision)).where(
                IntelligenceRun.project_id == project_id,
                IntelligenceRun.audit_id == audit_id,
            )
        )
        or 0
    ) + 1
    run = IntelligenceRun(
        project_id=project_id,
        audit_id=audit_id,
        revision=revision,
        manifest=manifest,
        input_hash=input_hash,
        signature=signature,
    )
    db.add(run)
    await db.flush()
    db.add(
        AnalysisJob(
            project_id=project_id,
            run_id=run.id,
            kind="analyze_run",
            subject_id=run.id,
            idempotency_hash=stable_hash(["analyze_run", run.id]),
        )
    )
    if enrich:
        for document_id in sorted(
            {source["document_id"] for source in sources if source["document_id"]}
        ):
            key = stable_hash(["fetch_document", audit_id, document_id])
            if not await db.scalar(
                select(AnalysisJob.id).where(
                    AnalysisJob.project_id == project_id,
                    AnalysisJob.idempotency_hash == key,
                )
            ):
                db.add(
                    AnalysisJob(
                        project_id=project_id,
                        kind="fetch_document",
                        subject_id=document_id,
                        payload={"audit_id": audit_id},
                        idempotency_hash=key,
                    )
                )
    return run
