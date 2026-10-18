"""Latest audit overview. Never mix history or fall back to an older success."""

from sqlalchemy import func, or_, select

from app.config import settings
from app.intelligence.models import AnalysisJob, IntelligenceRun
from app.intelligence.reporting import audit_report
from app.models.models import Audit
from app.utils.timezone import utc_isoformat, utcnow


async def project_dashboard(db, project_id):
    result = {
        "schema": "citation-dashboard-1",
        "project_id": project_id,
        "enabled": settings.intelligence_enabled,
        "latest_audit": None,
        "report": None,
        "processing": False,
        "waiting": False,
    }
    if not settings.intelligence_enabled:
        return result
    audit = await db.scalar(
        select(Audit)
        .where(Audit.project_id == project_id)
        .order_by(Audit.created_at.desc(), Audit.id.desc())
        .limit(1)
    )
    if audit is None:
        return result
    report = await audit_report(db, audit, include_measurements=False)
    # Dashboard needs denominators and diagnoses; full evidence stays in the report.
    overview = {
        key: value
        for key, value in report.items()
        if key not in ("sources", "evidence", "responses", "source_states")
    }
    processing = await audit_processing(db, audit)
    result.update(
        latest_audit={
            "id": str(audit.id),
            "status": audit.status.value,
            "stage": audit.stage.value,
            "created_at": utc_isoformat(audit.created_at),
            "platforms": audit.platforms_json or [],
        },
        report=overview,
        processing=processing,
        waiting=processing and not await audit_processing(db, audit, due_only=True),
    )
    return result


async def audit_processing(db, audit, *, due_only=False):
    project_id = audit.project_id
    active = await db.scalar(
        select(func.count(AnalysisJob.id)).where(
            AnalysisJob.project_id == project_id,
            AnalysisJob.status.in_(["queued", "leased", "retry_wait"]),
            or_(
                not due_only,
                AnalysisJob.status == "leased",
                AnalysisJob.available_at <= utcnow(),
            ),
            or_(
                AnalysisJob.run_id.in_(
                    select(IntelligenceRun.id).where(
                        IntelligenceRun.project_id == project_id,
                        IntelligenceRun.audit_id == audit.id,
                    )
                ),
                (AnalysisJob.kind == "fetch_document")
                & (AnalysisJob.payload["audit_id"].as_integer() == audit.id),
                (AnalysisJob.kind == "refresh_run")
                & (AnalysisJob.subject_id == str(audit.id)),
            ),
        )
    )
    return bool(active) or audit.status.value in ("pending", "running")
