"""Scheduled audit service.

Lightweight cron-based scheduler that runs inside the FastAPI process.
Parses simple cron expressions and triggers audits automatically.
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from app.utils.timezone import utcnow

from sqlalchemy import select

from app.database import async_session
from app.logging_config import get_logger
from app.models.models import Audit, ScheduledJob
from app.services.audit_service import run_audit
from app.services.report_service import generate_report

logger = get_logger("scheduler")

# Cron weekday (0=Sun..6=Sat) -> Python weekday (0=Mon..6=Sun)
_CRON_TO_PY_WEEKDAY = {0: 6, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}

_scheduler_task: Optional[asyncio.Task] = None
_tz: Optional[ZoneInfo] = None


def _get_tz() -> ZoneInfo:
    global _tz
    if _tz is None:
        from app.config import settings
        _tz = ZoneInfo(settings.tz)
    return _tz


def _parse_cron_field(field: str, all_values: range) -> list[int]:
    """Parse a single cron field into a list of matching values."""
    if field == "*":
        return list(all_values)
    values = []
    for part in field.split(","):
        if "/" in part:
            base, step = part.split("/")
            start = min(all_values) if base == "*" else int(base)
            step = int(step)
            values.extend(range(start, max(all_values) + 1, step))
        elif "-" in part:
            start, end = part.split("-")
            values.extend(range(int(start), int(end) + 1))
        else:
            values.append(int(part))
    return sorted(set(values))


def should_run_now(cron_expr: str, now: datetime) -> bool:
    """Check if the current time matches the cron expression.

    Supported format: minute hour day month weekday
    Example: "0 22 * * *" = every day at 22:00

    Weekday follows cron convention: 0=Sunday, 1=Monday, ..., 6=Saturday.
    """
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        return False

    minutes = _parse_cron_field(parts[0], range(0, 60))
    hours = _parse_cron_field(parts[1], range(0, 24))
    days = _parse_cron_field(parts[2], range(1, 32))
    months = _parse_cron_field(parts[3], range(1, 13))
    cron_weekdays = _parse_cron_field(parts[4], range(0, 7))

    # Convert cron weekdays (0=Sun) to Python weekdays (0=Mon)
    py_weekdays = sorted(set(_CRON_TO_PY_WEEKDAY.get(w, w) for w in cron_weekdays))

    return (
        now.minute in minutes
        and now.hour in hours
        and now.day in days
        and now.month in months
        and now.weekday() in py_weekdays
    )


async def scheduler_loop() -> None:
    """Main scheduler loop — checks every 60 seconds for jobs to run."""
    logger.info("scheduler_started")
    while True:
        try:
            now = datetime.now(_get_tz())
            async with async_session() as db:
                result = await db.execute(
                    select(ScheduledJob).where(ScheduledJob.is_active == True)  # noqa: E712
                )
                jobs = result.scalars().all()

                for job in jobs:
                    if job.last_run_at:
                        elapsed = (now - job.last_run_at).total_seconds()
                        if elapsed < 60:
                            continue

                    if should_run_now(job.cron_expression, now):
                        await _execute_scheduled_job(job, db)

        except Exception as e:
            logger.error("scheduler_error", error=str(e))

        await asyncio.sleep(60)


async def _execute_scheduled_job(job: ScheduledJob, db) -> None:
    """Execute a scheduled job: create audit, run it, generate report."""
    logger.info("scheduled_job_started", job_id=job.id, project_id=job.project_id)

    audit = Audit(
        project_id=job.project_id,
        platforms_json=job.platforms_json or [],
    )
    db.add(audit)
    job.last_run_at = utcnow()
    await db.commit()
    await db.refresh(audit)

    job.last_audit_id = audit.id
    await db.commit()

    # Run audit in a separate task so we don't block the scheduler
    asyncio.create_task(_run_and_report(audit.id))


async def _run_and_report(audit_id: int) -> None:
    """Run audit and generate report."""
    try:
        await run_audit(audit_id)
        async with async_session() as db:
            from app.models.models import Audit as AuditModel
            audit = await db.get(AuditModel, audit_id)
            if audit and audit.status.value in ("completed", "partial"):
                await generate_report(db, audit)
        logger.info("scheduled_audit_completed", audit_id=audit_id)
    except Exception as e:
        logger.error("scheduled_audit_failed", audit_id=audit_id, error=str(e))


def start_scheduler() -> None:
    """Start the scheduler background task."""
    global _scheduler_task
    if _scheduler_task is None or _scheduler_task.done():
        _scheduler_task = asyncio.create_task(scheduler_loop())


def stop_scheduler() -> None:
    """Stop the scheduler background task."""
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
        _scheduler_task = None
