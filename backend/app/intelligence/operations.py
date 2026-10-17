"""Read-only operational projection; no claiming, probing, retries or provider calls."""

from datetime import timedelta

from sqlalchemy import func, select

from app.config import settings
from app.intelligence.models import (
    AnalysisJob,
    SourceFetch,
    SourceSnapshot,
    WorkerHeartbeat,
)
from app.intelligence.monitor import RECENT_SECONDS
from app.intelligence.queue import db_now
from app.utils.timezone import utc_isoformat

JOB_STATUSES = {"queued", "retry_wait", "leased", "succeeded", "failed"}
FETCH_STATUSES = {
    "captured",
    "blocked",
    "unavailable",
    "timeout",
    "http_error",
    "not_found",
    "unsupported_format",
    "unsupported_encoding",
    "source_too_large",
    "empty_content",
    "invalid_redirect",
    "redirect_limit",
}
ERROR_CODES = {
    "lease_expired",
    "IntegrityError",
    "SourceBlocked",
    "SourceUnavailable",
    "TimeoutError",
    "unsupported_job_kind",
}


def safe_error(code):
    return code if code in ERROR_CODES else "other"


async def grouped(db, model, column, project_id, allowed, *conditions):
    rows = (
        await db.execute(
            select(column, func.count())
            .where(model.project_id == project_id, *conditions)
            .group_by(column)
        )
    ).all()
    result = {}
    for value, count in rows:
        key = value if value in allowed else "other"
        result[key] = result.get(key, 0) + count
    return result


async def operations_view(db, project_id):
    now = await db_now(db)
    cutoff = now - timedelta(seconds=RECENT_SECONDS)
    recent = (
        await db.scalars(
            select(WorkerHeartbeat).where(
                WorkerHeartbeat.last_seen_at >= cutoff,
                WorkerHeartbeat.last_seen_at <= now,
                WorkerHeartbeat.state.in_(["idle", "working"]),
            )
        )
    ).all()

    def probe_summary(status_field, time_field, allowed):
        counts = {}
        for worker in recent:
            checked = getattr(worker, time_field)
            status = getattr(worker, status_field)
            key = (
                status
                if checked and cutoff <= checked <= now and status in allowed
                else "unknown"
            )
            counts[key] = counts.get(key, 0) + 1
        return counts

    last_seen = await db.scalar(
        select(func.max(WorkerHeartbeat.last_seen_at)).where(
            WorkerHeartbeat.last_seen_at <= now
        )
    )
    due = [
        AnalysisJob.status.in_(["queued", "retry_wait"]),
        AnalysisJob.available_at <= now,
    ]
    count_due, oldest = (
        await db.execute(
            select(func.count(), func.min(AnalysisJob.available_at)).where(
                AnalysisJob.project_id == project_id, *due
            )
        )
    ).one()
    expired = await db.scalar(
        select(func.count())
        .select_from(AnalysisJob)
        .where(
            AnalysisJob.project_id == project_id,
            AnalysisJob.status == "leased",
            AnalysisJob.lease_until <= now,
        )
    )
    failures = (
        await db.scalars(
            select(AnalysisJob)
            .where(
                AnalysisJob.project_id == project_id,
                AnalysisJob.status == "failed",
            )
            .order_by(AnalysisJob.available_at.desc(), AnalysisJob.id.desc())
            .limit(10)
        )
    ).all()
    reservations = 0
    unknown_reservations = 0
    # Stream only the required JSON field, never return payloads or materialize source text.
    payloads = await db.stream_scalars(
        select(AnalysisJob.payload)
        .where(AnalysisJob.project_id == project_id)
        .execution_options(yield_per=500)
    )
    async for payload in payloads:
        value = payload.get("model_requests", 0) if isinstance(payload, dict) else None
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            reservations += value
        else:
            unknown_reservations += 1
    return {
        "project_id": project_id,
        "observed_at": utc_isoformat(now),
        "enabled": settings.intelligence_enabled,
        "worker": {
            "scope": "shared_service",
            "status": "recent" if recent else "missing" if last_seen else "unknown",
            "recent_count": len(recent),
            "last_seen_at": utc_isoformat(last_seen),
            "threshold_seconds": RECENT_SECONDS,
            "storage_configuration": probe_summary(
                "storage_status",
                "storage_checked_at",
                {"configuration_verified", "failed", "development_local"},
            ),
            "temporary_disk": probe_summary(
                "disk_status", "disk_checked_at", {"ok", "low"}
            ),
        },
        "queue": {
            "counts": await grouped(
                db, AnalysisJob, AnalysisJob.status, project_id, JOB_STATUSES
            ),
            "due": count_due,
            "oldest_due_at": utc_isoformat(oldest),
            "expired_leases": expired,
            # available_at records the terminal transition plus its retry delay, not an exact failure timestamp.
            "recent_failures": [
                {"id": row.id, "category": safe_error(row.error_code)}
                for row in failures
            ],
        },
        "sources": {
            "fetches_last_24h": await grouped(
                db,
                SourceFetch,
                SourceFetch.status,
                project_id,
                FETCH_STATUSES,
                SourceFetch.created_at >= now - timedelta(days=1),
            ),
            "snapshot_integrity_records": await grouped(
                db,
                SourceSnapshot,
                SourceSnapshot.integrity_status,
                project_id,
                {"verified", "failed", "unknown"},
            ),
            "live_integrity_check": "not_performed",
        },
        "usage": {
            "model_request_reservations": reservations,
            "unknown_job_records": unknown_reservations,
            "scope": "project_lifetime",
            "billed_cost": None,
            "oss_traffic_bytes": None,
        },
        "backup_restore_status": "not_connected",
    }
