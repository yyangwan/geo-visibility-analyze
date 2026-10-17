"""A failed task remains a visible source state, never a permanent pending input."""

from sqlalchemy import select

from app.intelligence.models import (
    AuditIntelligenceState,
    RunRefreshRequest,
    SourceFetch,
)


async def terminal_job(db, job):
    if job.kind == "fetch_document":
        captured = await db.scalar(
            select(SourceFetch)
            .where(
                SourceFetch.project_id == job.project_id,
                SourceFetch.job_id == job.id,
                SourceFetch.status == "captured",
            )
            .limit(1)
        )
        if not captured:
            db.add(
                SourceFetch(
                    project_id=job.project_id,
                    document_id=job.subject_id,
                    audit_id=job.payload["audit_id"],
                    job_id=job.id,
                    status="failed",
                    error_code=job.error_code,
                )
            )
            await db.flush()
        from app.intelligence.refresh import request_refresh

        await request_refresh(
            db, job.project_id, int(job.payload["audit_id"]), "source_terminal", job.id
        )
    elif job.kind == "refresh_run":
        state = await db.scalar(
            select(AuditIntelligenceState)
            .where(
                AuditIntelligenceState.project_id == job.project_id,
                AuditIntelligenceState.audit_id == int(job.subject_id),
            )
            .with_for_update()
        )
        if state and state.pending_job_id == job.id:
            state.pending_job_id = None
            events = (
                await db.scalars(
                    select(RunRefreshRequest).where(
                        RunRefreshRequest.project_id == job.project_id,
                        RunRefreshRequest.audit_id == state.audit_id,
                        RunRefreshRequest.status.in_(["pending", "awaiting_refresh"]),
                    )
                )
            ).all()
            for event in events:
                event.status = "failed"
