"""Durable coalescing, bounded enhancement and monotonic report publication."""

from datetime import timedelta

from sqlalchemy import or_, select

from app.config import settings
from app.intelligence.models import (
    AnalysisJob,
    AuditIntelligenceState,
    RunRefreshRequest,
    SourceFetch,
)
from app.intelligence.semantics import stable_hash
from app.models.models import Audit
from app.utils.timezone import utcnow


async def state_for_audit(db, project_id, audit_id):
    audit = await db.scalar(
        select(Audit)
        .where(Audit.id == audit_id, Audit.project_id == project_id)
        .with_for_update()
    )
    if audit is None:
        raise LookupError("Audit not found")
    state = await db.scalar(
        select(AuditIntelligenceState)
        .where(
            AuditIntelligenceState.project_id == project_id,
            AuditIntelligenceState.audit_id == audit_id,
        )
        .with_for_update()
    )
    if state is None:
        state = AuditIntelligenceState(project_id=project_id, audit_id=audit_id)
        db.add(state)
        await db.flush()
    return state


async def settled_inputs(db, project_id, audit_id):
    audit = await db.scalar(
        select(Audit).where(Audit.id == audit_id, Audit.project_id == project_id)
    )
    if audit is None or audit.status in ("pending", "running"):
        return False
    # A fetch commits its terminal observation before its own leased job is closed.
    terminal = (
        select(SourceFetch.id)
        .where(
            SourceFetch.project_id == project_id,
            SourceFetch.job_id == AnalysisJob.id,
            AnalysisJob.status == "leased",
            or_(
                SourceFetch.http_status.is_(None),
                (SourceFetch.http_status < 500) & (SourceFetch.http_status != 429),
            ),
        )
        .exists()
    )
    active = await db.scalar(
        select(AnalysisJob.id)
        .where(
            AnalysisJob.project_id == project_id,
            AnalysisJob.kind == "fetch_document",
            AnalysisJob.payload["audit_id"].as_integer() == audit_id,
            AnalysisJob.status.in_(["queued", "retry_wait", "leased"]),
            ~terminal,
        )
        .limit(1)
    )
    return active is None


async def recover_settled_refreshes(db):
    # Recover durable quota waits on worker restart without changing audit data.
    states = (
        await db.scalars(
            select(AuditIntelligenceState)
            .where(
                AuditIntelligenceState.pending_job_id.is_not(None),
                AuditIntelligenceState.input_revision
                > AuditIntelligenceState.handled_revision,
            )
            .limit(100)
        )
    ).all()
    recovered = 0
    now = utcnow().replace(microsecond=0)
    for state in states:
        job = await db.get(AnalysisJob, state.pending_job_id)
        if (
            job
            and job.status in ("queued", "retry_wait")
            and job.available_at > now
            and await final_inputs_ready(db, state.project_id, state.audit_id)
        ):
            job.available_at = now
            job.payload = {**job.payload, "waiting_reason": None}
            recovered += 1
    return recovered


async def final_inputs_ready(db, project_id, audit_id):
    if not await settled_inputs(db, project_id, audit_id):
        return False
    # Only new capture/source facts bypass enhancement quotas. Editorial changes
    # after final convergence retain the normal daily limit.
    event = await db.scalar(
        select(RunRefreshRequest.id)
        .where(
            RunRefreshRequest.project_id == project_id,
            RunRefreshRequest.audit_id == audit_id,
            RunRefreshRequest.status.in_(["pending", "awaiting_refresh"]),
            RunRefreshRequest.reason.in_(
                ["sample_sealed", "capture_terminal", "source_terminal"]
            ),
        )
        .limit(1)
    )
    return event is not None


async def request_refresh(db, project_id, audit_id, reason, event_id):
    state = await state_for_audit(db, project_id, audit_id)
    event_hash = stable_hash([audit_id, reason, event_id])
    if await db.scalar(
        select(RunRefreshRequest.id).where(
            RunRefreshRequest.project_id == project_id,
            RunRefreshRequest.event_hash == event_hash,
        )
    ):
        return
    state.input_revision += 1
    event = RunRefreshRequest(
        project_id=project_id, audit_id=audit_id, reason=reason, event_hash=event_hash
    )
    db.add(event)
    settled = await final_inputs_ready(db, project_id, audit_id)
    if state.pending_job_id:
        pending = await db.get(AnalysisJob, state.pending_job_id)
        if settled and pending and pending.status in ("queued", "retry_wait"):
            pending.available_at = min(
                pending.available_at,
                utcnow().replace(microsecond=0)
                + timedelta(seconds=settings.intelligence_refresh_debounce_seconds),
            )
            pending.payload = {**pending.payload, "waiting_reason": None}
        return
    now = utcnow().replace(microsecond=0)
    state.refresh_generation += 1
    available = now + timedelta(seconds=settings.intelligence_refresh_debounce_seconds)
    if (
        not settled
        and state.window_start
        and state.automatic_runs >= settings.intelligence_auto_refresh_limit
    ):
        available = max(available, state.window_start + timedelta(hours=24))
        event.status = "awaiting_refresh"
    job = AnalysisJob(
        project_id=project_id,
        kind="refresh_run",
        subject_id=str(audit_id),
        payload={
            "generation": state.refresh_generation,
            "waiting_reason": "enhancement_quota"
            if event.status == "awaiting_refresh"
            else None,
        },
        available_at=available,
        idempotency_hash=stable_hash(
            ["refresh_run", audit_id, state.refresh_generation]
        ),
    )
    db.add(job)
    await db.flush()
    state.pending_job_id = job.id


async def perform_refresh(db, project_id, audit_id, job_id):
    from app.intelligence.ingestion import prepare_run

    state = await state_for_audit(db, project_id, audit_id)
    now = utcnow().replace(microsecond=0)
    if not state.window_start or state.window_start + timedelta(hours=24) <= now:
        state.window_start, state.automatic_runs = now, 0
    settled = await final_inputs_ready(db, project_id, audit_id)
    if not settled and state.automatic_runs >= settings.intelligence_auto_refresh_limit:
        # Normally available_at already prevents this; a manual concurrent refresh
        # can still exhaust the window. Keep a durable next-window job.
        state.pending_job_id = None
        await request_refresh(
            db, project_id, audit_id, "window_wait", str(state.input_revision)
        )
        return
    # Newly sealed samples may introduce URLs. Per-audit/document job keys
    # dispatch each initial fetch once; terminal-source refreshes reuse the key.
    await prepare_run(db, project_id, audit_id, enrich=True)
    if not settled:
        state.automatic_runs += 1
    state.handled_revision = state.input_revision
    if state.pending_job_id == job_id:
        state.pending_job_id = None
    events = (
        await db.scalars(
            select(RunRefreshRequest).where(
                RunRefreshRequest.project_id == project_id,
                RunRefreshRequest.audit_id == audit_id,
                RunRefreshRequest.status.in_(["pending", "awaiting_refresh"]),
            )
        )
    ).all()
    for event in events:
        event.status = "processed"


async def publish_pointer(db, run):
    state = await state_for_audit(db, run.project_id, run.audit_id)
    if run.revision > state.published_revision:
        state.published_run_id, state.published_revision = run.id, run.revision
