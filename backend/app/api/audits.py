"""Audit API endpoints — create, monitor, and retrieve audit results."""

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.access import get_audit_for_project, require_project_scope
from app.api.auth import get_current_user
from app.api.schemas import AuditCreate, AuditOut, QueryResultOut, ReportOut
from app.database import get_db
from app.models.models import (
    Audit,
    AuditEventLog,
    AuditPlatformRun,
    AuditStageRun,
    Prompt,
    QueryResult,
    QueryStatus,
    Report,
)
from app.services.audit_events import PlatformEvent, subscribe, unsubscribe
from app.services.audit_service import run_audit
from app.services.report_service import generate_report
from app.services.genilink_auth import verify_genilink_token

router = APIRouter()


def _sse_event(event_type: str, data: dict) -> str:
    """Format a single SSE event."""
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _dt(value):
    return value.isoformat() if value else None


def _serialize_audit(audit: Audit) -> dict:
    return {
        "id": audit.id,
        "project_id": audit.project_id,
        "status": audit.status.value,
        "stage": audit.stage.value,
        "stage_status": audit.stage_status.value,
        "platforms_json": audit.platforms_json or [],
        "brands_json": audit.brands_json or [],
        "created_at": _dt(audit.created_at),
        "completed_at": _dt(audit.completed_at),
        "stage_started_at": _dt(audit.stage_started_at),
        "stage_updated_at": _dt(audit.stage_updated_at),
        "last_heartbeat_at": _dt(audit.last_heartbeat_at),
        "analysis_run_id": audit.analysis_run_id,
        "attempt_count": audit.attempt_count,
        "error_code": audit.error_code,
        "error_message": audit.error_message,
        "recoverable_error": audit.recoverable_error,
        "next_retry_at": _dt(audit.next_retry_at),
        "locked_by_worker": audit.locked_by_worker,
        "locked_until": _dt(audit.locked_until),
    }


def _serialize_stage_run(stage_run: AuditStageRun) -> dict:
    return {
        "id": stage_run.id,
        "stage_name": stage_run.stage_name,
        "status": stage_run.status.value,
        "attempt_no": stage_run.attempt_no,
        "started_at": _dt(stage_run.started_at),
        "finished_at": _dt(stage_run.finished_at),
        "duration_ms": stage_run.duration_ms,
        "error_code": stage_run.error_code,
        "error_message": stage_run.error_message,
        "input_snapshot": stage_run.input_snapshot or {},
        "output_snapshot": stage_run.output_snapshot or {},
        "worker_id": stage_run.worker_id,
        "created_at": _dt(stage_run.created_at),
    }


def _serialize_platform_run(platform_run: AuditPlatformRun) -> dict:
    return {
        "id": platform_run.id,
        "stage_run_id": platform_run.stage_run_id,
        "prompt_id": platform_run.prompt_id,
        "platform": platform_run.platform,
        "status": platform_run.status.value,
        "attempt_no": platform_run.attempt_no,
        "started_at": _dt(platform_run.started_at),
        "finished_at": _dt(platform_run.finished_at),
        "duration_ms": platform_run.duration_ms,
        "error_code": platform_run.error_code,
        "error_message": platform_run.error_message,
        "response_record_id": platform_run.response_record_id,
        "worker_id": platform_run.worker_id,
        "retry_after": _dt(platform_run.retry_after),
        "created_at": _dt(platform_run.created_at),
    }


def _serialize_event_log(event: AuditEventLog) -> dict:
    return {
        "id": event.id,
        "stage_name": event.stage_name,
        "event_type": event.event_type,
        "payload": event.payload or {},
        "created_at": _dt(event.created_at),
    }


async def _build_audit_snapshot(db: AsyncSession, audit_id: int) -> dict:
    audit = await db.get(Audit, audit_id)
    if not audit:
        return {}

    stage_runs_result = await db.execute(
        select(AuditStageRun).where(AuditStageRun.audit_id == audit_id).order_by(AuditStageRun.created_at.asc())
    )
    platform_runs_result = await db.execute(
        select(AuditPlatformRun).where(AuditPlatformRun.audit_id == audit_id).order_by(AuditPlatformRun.created_at.asc())
    )
    event_logs_result = await db.execute(
        select(AuditEventLog).where(AuditEventLog.audit_id == audit_id).order_by(AuditEventLog.created_at.asc())
    )

    return {
        "audit": _serialize_audit(audit),
        "stage_runs": [_serialize_stage_run(row) for row in stage_runs_result.scalars().all()],
        "platform_runs": [_serialize_platform_run(row) for row in platform_runs_result.scalars().all()],
        "event_logs": [_serialize_event_log(row) for row in event_logs_result.scalars().all()],
    }


@router.post("", response_model=AuditOut)
async def create_audit(
    data: AuditCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new audit and start execution in the background."""
    require_project_scope(current_user, data.project_id)
    audit = Audit(
        project_id=data.project_id,
        status=QueryStatus.PENDING,
        platforms_json=data.platforms,
    )
    audit.brands_json = data.brands
    db.add(audit)
    await db.commit()
    await db.refresh(audit)

    # Background task claims the audit atomically before starting work.
    asyncio.create_task(run_audit(audit.id))
    return audit


@router.get("/{audit_id}", response_model=AuditOut)
async def get_audit(
    audit_id: int,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    audit = await get_audit_for_project(db, current_user, audit_id)
    return audit


@router.get("/{audit_id}/results", response_model=list[QueryResultOut])
async def get_audit_results(
    audit_id: int,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    audit = await get_audit_for_project(db, current_user, audit_id)

    result = await db.execute(
        select(QueryResult).where(QueryResult.audit_id == audit_id)
    )
    results = result.scalars().all()

    # Bulk load prompts to avoid N+1
    # Note: We include deleted prompts for historical audit visibility
    prompt_ids = {r.prompt_id for r in results}

    prompts_result = await db.execute(
        select(Prompt).where(Prompt.id.in_(prompt_ids))
    )
    prompt_map = {p.id: p for p in prompts_result.scalars().all()}

    # Build brand name lookup from audit.brands_json
    brand_map = {b.get("id"): b.get("name") for b in (audit.brands_json or [])}

    out = []
    for r in results:
        prompt = prompt_map.get(r.prompt_id)
        out.append(
            QueryResultOut(
                id=r.id,
                platform=r.platform,
                prompt_text=prompt.text if prompt else None,
                brand_name=brand_map.get(r.brand_id),
                mention_found=r.mention_found,
                mention_position=r.mention_position,
                mention_context=r.mention_context,
                mention_confidence=r.mention_confidence,
                is_recommended=r.is_recommended,
                recommendation_rank=r.recommendation_rank,
                error=r.error,
            )
        )
    return out


@router.get("/{audit_id}/events")
async def audit_events(
    audit_id: int,
    token: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """SSE endpoint that streams per-platform completion events."""
    # Manual auth since EventSource can't send Authorization header
    payload = await verify_genilink_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")

    audit = await db.get(Audit, audit_id)
    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found")
    require_project_scope(payload, audit.project_id)

    queue = subscribe(audit_id)

    async def _stream():
        try:
            snapshot = await _build_audit_snapshot(db, audit_id)
            if snapshot:
                yield _sse_event("audit_snapshot", snapshot)

            if audit.status.value in ("completed", "partial", "failed"):
                yield _sse_event("audit_done", {"status": audit.status.value})
                return

            while True:
                try:
                    event: PlatformEvent = await asyncio.wait_for(queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    # Heartbeat to keep connection alive
                    yield ": heartbeat\n\n"
                    continue

                data = {"type": event.type}
                if event.platform:
                    data["platform"] = event.platform
                if event.error:
                    data["error"] = event.error

                yield _sse_event(event.type, data)

                if event.type in ("audit_done", "audit_failed"):
                    break
        finally:
            unsubscribe(audit_id, queue)

    return StreamingResponse(_stream(), media_type="text/event-stream")


@router.post("/{audit_id}/report", response_model=ReportOut)
async def create_report(
    audit_id: int,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate a report from a completed audit."""
    audit = await get_audit_for_project(db, current_user, audit_id)

    if audit.status.value not in ("completed", "partial"):
        raise HTTPException(
            status_code=400,
            detail=f"Audit status is '{audit.status.value}', must be completed or partial",
        )

    # Check if report already exists (idempotent)
    existing = await db.execute(
        select(Report).where(Report.audit_id == audit_id)
    )
    existing_report = existing.scalar_one_or_none()
    if existing_report:
        return existing_report

    report = await generate_report(db, audit)
    return report


@router.get("/{audit_id}/report", response_model=ReportOut)
async def get_report(
    audit_id: int,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    audit = await get_audit_for_project(db, current_user, audit_id)

    result = await db.execute(
        select(Report).where(Report.audit_id == audit_id)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return report
