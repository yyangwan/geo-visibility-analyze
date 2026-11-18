"""One source-archive settlement and immutable analysis input per v4 audit."""

import logging
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import case, func, select

from app.config import settings
from app.intelligence.models import (
    AnalysisJob,
    AuditSample,
    CapturedResponse,
    CitationObservation,
    SourceFetch,
    SourceSnapshot,
    IntelligenceRun,
)
from app.intelligence.queue import db_now
from app.intelligence.semantics import stable_hash
from app.models.models import Audit
from app.utils.timezone import utc_isoformat

logger = logging.getLogger(__name__)

FLOW_KIND = "visibility_audit"
ACTIVE_SOURCE_STATES = ("queued", "leased", "retry_wait")


def flow_key(audit_id):
    return stable_hash([FLOW_KIND, audit_id])


def create_flow(db, project_id, audit_id):
    # Conditional settlement is worker-driven. These states are not claimable
    # by the ordinary queue, whose claims require queued or retry_wait.
    db.add(
        AnalysisJob(
            project_id=project_id,
            kind=FLOW_KIND,
            subject_id=str(audit_id),
            idempotency_hash=flow_key(audit_id),
            status="waiting_capture",
        )
    )


async def find_flow(db, project_id, audit_id, *, lock=False):
    stmt = select(AnalysisJob).where(
        AnalysisJob.project_id == project_id,
        AnalysisJob.kind == FLOW_KIND,
        AnalysisJob.idempotency_hash == flow_key(audit_id),
    )
    if lock:
        stmt = stmt.with_for_update()
    return await db.scalar(stmt.execution_options(populate_existing=True))


async def lock_audit(db, project_id, audit_id):
    return await db.scalar(
        select(Audit)
        .where(Audit.project_id == project_id, Audit.id == audit_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def capture_event(db, project_id, audit_id, reason, event_id):
    from app.intelligence.ingestion import (
        queue_source_documents,
        register_capture_sources,
    )

    audit = await lock_audit(db, project_id, audit_id)
    flow = await find_flow(db, project_id, audit_id, lock=True)
    if audit is None or flow is None or flow.status != "waiting_capture":
        return

    if reason == "sample_sealed":
        record = (
            await db.execute(
                select(AuditSample, CapturedResponse)
                .join(
                    CapturedResponse,
                    (CapturedResponse.project_id == AuditSample.project_id)
                    & (CapturedResponse.sample_id == AuditSample.id)
                    & (CapturedResponse.id == AuditSample.selected_response_id),
                )
                .where(
                    AuditSample.project_id == project_id,
                    AuditSample.audit_id == audit_id,
                    AuditSample.id == event_id,
                )
            )
        ).one_or_none()
        if record is None:
            raise ValueError("V4 sample seal is missing its captured response")
        sample, response = record
        exists = await db.scalar(
            select(CitationObservation.id)
            .where(
                CitationObservation.project_id == project_id,
                CitationObservation.audit_id == audit_id,
                CitationObservation.response_id == response.id,
            )
            .limit(1)
        )
        if not exists:
            observations = await register_capture_sources(
                db, project_id, audit_id, sample, response
            )
            await queue_source_documents(
                db,
                project_id,
                audit_id,
                (item.document_id for item in observations if item.document_id),
            )

    if audit.status in ("completed", "partial", "failed"):
        now = await db_now(db)
        flow.payload = {"capture_closed_at": utc_isoformat(now)}
        if audit.status == "failed":
            flow.status = "succeeded"
            flow.payload = {
                **flow.payload,
                "settled_at": utc_isoformat(now),
                "skipped": True,
            }
        else:
            flow.status = "archiving"
            flow.available_at = now + timedelta(
                seconds=settings.intelligence_visibility_archive_seconds
            )


async def source_publication_time(db, project_id, audit_id):
    if await find_flow(db, project_id, audit_id) is None:
        return None
    # A successful fetch and the worker freeze both take this audit lock.
    # Stamp availability after acquiring it so it cannot predate the cutoff
    # if the source writes only after an already-completed settlement.
    await lock_audit(db, project_id, audit_id)
    return await db_now(db)


async def settle_flow(db, project_id, audit_id):
    from app.intelligence.ingestion import prepare_run
    from app.intelligence.refresh import settled_inputs

    audit = await lock_audit(db, project_id, audit_id)
    if audit is None:
        return False
    flow = await find_flow(db, project_id, audit_id, lock=True)
    if flow is None or flow.status != "archiving":
        return False
    now = await db_now(db)
    if now < flow.available_at and not await settled_inputs(db, project_id, audit_id):
        return False

    run = await prepare_run(
        db,
        project_id,
        audit_id,
        enrich=False,
        freeze_at=min(now, flow.available_at),
        settled_at=now,
    )
    await db.flush()
    job_id = await db.scalar(
        select(AnalysisJob.id).where(
            AnalysisJob.project_id == project_id,
            AnalysisJob.kind == "analyze_run",
            AnalysisJob.run_id == run.id,
        )
    )
    if job_id is None:
        raise RuntimeError("V4 analysis dispatch missing")
    flow.status = "succeeded"
    flow.payload = {
        **flow.payload,
        "settled_at": utc_isoformat(now),
        "archive_cutoff_at": run.manifest["visibility_flow"]["archive_cutoff_at"],
        "run_id": run.id,
        "current_job_id": job_id,
    }
    return True


async def reconcile_flows(sessions):
    async with sessions() as db:
        # Scan in a different transaction from the per-audit lock so MySQL
        # repeatable-read does not see a stale snapshot while freezing inputs.
        rows = (
            await db.execute(
                select(AnalysisJob.project_id, AnalysisJob.subject_id)
                .where(AnalysisJob.kind == FLOW_KIND, AnalysisJob.status == "archiving")
                .order_by(AnalysisJob.available_at, AnalysisJob.id)
                .limit(20)
            )
        ).all()
    for project_id, audit_id in rows:
        try:
            async with sessions() as db, db.begin():
                await settle_flow(db, project_id, int(audit_id))
        except Exception as error:
            # Keep processing other audits. The failed transaction has neither
            # closed this audit nor claimed its single automatic dispatch.
            logger.error(
                "archive_settlement_error audit=%s class=%s",
                audit_id,
                type(error).__name__,
            )


async def retry_analysis(db, project_id, audit_id, failed_attempt_id, actor):
    if await lock_audit(db, project_id, audit_id) is None:
        raise HTTPException(404, "Audit not found")
    flow = await find_flow(db, project_id, audit_id, lock=True)
    if flow is None or not flow.payload.get("run_id"):
        raise HTTPException(409, "No frozen analysis is available")
    run_id = flow.payload["run_id"]
    retry_key = stable_hash(["visibility_retry", audit_id, failed_attempt_id])
    existing = await db.scalar(
        select(AnalysisJob).where(
            AnalysisJob.project_id == project_id,
            AnalysisJob.run_id == run_id,
            AnalysisJob.kind == "analyze_run",
            AnalysisJob.idempotency_hash == retry_key,
        )
    )
    if existing:
        return existing
    if flow.payload.get("current_job_id") != failed_attempt_id:
        raise HTTPException(409, "The specified attempt is not the current failure")
    failed = await db.scalar(
        select(AnalysisJob)
        .where(
            AnalysisJob.project_id == project_id,
            AnalysisJob.run_id == run_id,
            AnalysisJob.kind == "analyze_run",
            AnalysisJob.id == failed_attempt_id,
        )
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    run = await db.scalar(
        select(IntelligenceRun).where(
            IntelligenceRun.project_id == project_id,
            IntelligenceRun.audit_id == audit_id,
            IntelligenceRun.id == run_id,
        )
    )
    if (
        failed is None
        or failed.status != "failed"
        or run is None
        or run.status != "failed"
    ):
        raise HTTPException(409, "Only a failed analysis may be retried")
    job = AnalysisJob(
        project_id=project_id,
        kind="analyze_run",
        run_id=run.id,
        subject_id=run.id,
        idempotency_hash=retry_key,
        payload={
            "visibility_attempt": failed.payload["visibility_attempt"] + 1,
            "retry_of": failed.id,
            "requested_by": actor,
        },
    )
    db.add(job)
    run.status, run.finished_at = "queued", None
    await db.flush()
    flow.payload = {**flow.payload, "current_job_id": job.id}
    return job


async def archive_coverage(db, project_id, audit_id):
    archived = (
        select(SourceFetch.id)
        .join(
            SourceSnapshot,
            (SourceSnapshot.project_id == SourceFetch.project_id)
            & (SourceSnapshot.document_id == SourceFetch.document_id)
            & (SourceSnapshot.id == SourceFetch.snapshot_id)
            & (SourceSnapshot.integrity_status == "verified"),
        )
        .where(
            SourceFetch.project_id == project_id,
            SourceFetch.audit_id == audit_id,
            SourceFetch.document_id == CitationObservation.document_id,
        )
        .exists()
    )
    active = (
        select(AnalysisJob.id)
        .where(
            AnalysisJob.project_id == project_id,
            AnalysisJob.kind == "fetch_document",
            AnalysisJob.payload["audit_id"].as_integer() == audit_id,
            AnalysisJob.subject_id == CitationObservation.document_id,
            AnalysisJob.status.in_(ACTIVE_SOURCE_STATES),
        )
        .exists()
    )
    observed, linked, stored, pending = (
        await db.execute(
            select(
                func.count(CitationObservation.id),
                func.count(
                    case(
                        (
                            CitationObservation.url.is_not(None)
                            & (CitationObservation.url != ""),
                            1,
                        )
                    )
                ),
                func.count(case((archived, 1))),
                func.count(case((~archived & active, 1))),
            ).where(
                CitationObservation.project_id == project_id,
                CitationObservation.audit_id == audit_id,
            )
        )
    ).one()
    return {
        "observed": observed,
        "linked": linked,
        "archived": stored,
        "pending": pending,
        "unavailable": observed - stored - pending,
    }


async def flow_progress(db, flow, capture_status, source_pending, platforms):
    capture = (
        "collecting"
        if flow.status == "waiting_capture"
        else "failed"
        if flow.payload.get("skipped")
        else capture_status
    )
    archive = (
        ("archiving" if source_pending else "pending")
        if flow.status == "waiting_capture"
        else "archiving"
        if flow.status == "archiving"
        else "skipped"
        if flow.payload.get("skipped")
        else "settled"
    )
    job = (
        await db.scalar(
            select(AnalysisJob).where(
                AnalysisJob.project_id == flow.project_id,
                AnalysisJob.run_id == flow.payload["run_id"],
                AnalysisJob.id == flow.payload["current_job_id"],
            )
        )
        if flow.payload.get("current_job_id")
        else None
    )
    analysis = (
        "waiting_for_capture"
        if flow.status == "waiting_capture"
        else "waiting_for_archive"
        if flow.status == "archiving"
        else "skipped"
    )
    if job is not None:
        analysis = {
            "leased": "running",
            "failed": "failed",
            "succeeded": "completed",
        }.get(job.status, "queued")
    return {
        "mode": "v4",
        "capture": {
            "state": capture,
            "closed_at": flow.payload.get("capture_closed_at"),
            **{
                field: sum(counts[field] for counts in platforms.values())
                for field in ("planned", "received", "failed", "pending")
            },
        },
        "archive": {
            **await archive_coverage(db, flow.project_id, int(flow.subject_id)),
            "state": archive,
            "deadline_at": utc_isoformat(flow.available_at)
            if "capture_closed_at" in flow.payload and archive != "skipped"
            else None,
            "settled_at": flow.payload.get("settled_at"),
            "cutoff_at": flow.payload.get("archive_cutoff_at"),
        },
        "analysis": {
            "state": analysis,
            "attempt_id": job.id if job else None,
            "attempt_no": job.payload["visibility_attempt"] if job else None,
            "snapshot_id": job.run_id if job else None,
            "can_retry": analysis == "failed",
            "failure_reason": (
                "analysis_interrupted"
                if job.error_code == "lease_expired"
                else "analysis_failed"
            )
            if analysis == "failed"
            else None,
        },
        "poll_after_ms": None
        if analysis in ("completed", "failed", "skipped")
        else 4000,
    }
