"""Audit execution service.

Orchestrates the full audit pipeline:
1. Load prompts and brands (from audit.brands_json snapshot)
2. Query each platform via adapters
3. Create PlatformResponseRecords (one per prompt+platform)
4. Run source extraction synchronously
5. Detect brand mentions in responses
6. Store results and update audit status
"""

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, func, select, text
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import ErrorCode, PlatformResponse
from app.adapters.registry import get_adapters
from app.database import async_session
from app.logging_config import get_logger
from app.models.models import (
    Audit,
    AuditEventLog,
    AuditPlatformRun,
    AuditStage,
    AuditStageRun,
    PlatformResponseRecord,
    Prompt,
    QueryResult,
    QueryStatus,
    RunStatus,
    SourceCitation,
)
from app.services.audit_events import PlatformEvent, publish
from app.services.detect import detect_mentions
from app.services.platform_config_service import get_platform_config
from app.services.source_extraction import extract_sources
from app.utils.timezone import utcnow

logger = get_logger("audit")


class BrandData:
    """Simple brand data container (replaces ORM Brand model)."""

    def __init__(self, id: str, name: str, aliases: list[str], is_competitor: bool):
        self.id = id
        self.name = name
        self.aliases = aliases
        self.is_competitor = is_competitor


@dataclass(slots=True)
class PlatformQueryOutcome:
    platform_name: str
    responses: list[PlatformResponse]
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    error: Exception | None = None


_WORKER_ID = f"local-{os.getpid()}"
_LEASE_SECONDS = 15 * 60


def is_degraded_response(resp: PlatformResponse) -> bool:
    """Return whether a response should be archived but excluded from scoring."""
    finish_reason = (resp.finish_reason or "").strip().lower()
    if finish_reason == "length":
        return True

    metadata = resp.search_metadata or {}
    return bool(metadata.get("response_degraded"))


async def _next_stage_attempt_no(db: AsyncSession, audit_id: int, stage_name: str) -> int:
    result = await db.execute(
        select(func.coalesce(func.max(AuditStageRun.attempt_no), 0)).where(
            AuditStageRun.audit_id == audit_id,
            AuditStageRun.stage_name == stage_name,
        )
    )
    return int(result.scalar_one() or 0) + 1


async def _next_platform_attempt_no(
    db: AsyncSession,
    audit_id: int,
    prompt_id: int,
    platform: str,
) -> int:
    result = await db.execute(
        select(func.coalesce(func.max(AuditPlatformRun.attempt_no), 0)).where(
            AuditPlatformRun.audit_id == audit_id,
            AuditPlatformRun.prompt_id == prompt_id,
            AuditPlatformRun.platform == platform,
        )
    )
    return int(result.scalar_one() or 0) + 1


def _build_source_snapshot_hash(
    citations: list[dict] | None,
    search_metadata: dict | None,
    request_params: dict | None,
) -> str:
    """Build a stable digest for the source snapshot behind a platform answer."""
    snapshot = {
        "citations": citations or [],
        "search_metadata": search_metadata or {},
        "request_params": request_params or {},
    }
    payload = json.dumps(
        snapshot,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _append_event(
    db: AsyncSession,
    audit: Audit,
    event_type: str,
    payload: dict | None = None,
    stage_name: str | None = None,
) -> AuditEventLog:
    event = AuditEventLog(
        audit_id=audit.id,
        stage_name=stage_name,
        event_type=event_type,
        payload=payload or {},
    )
    db.add(event)
    await db.flush()
    return event


async def _get_platform_response_record(
    db: AsyncSession,
    audit_id: int,
    prompt_id: int,
    platform: str,
) -> PlatformResponseRecord | None:
    result = await db.execute(
        select(PlatformResponseRecord).where(
            PlatformResponseRecord.audit_id == audit_id,
            PlatformResponseRecord.prompt_id == prompt_id,
            PlatformResponseRecord.platform == platform,
        )
    )
    return result.scalar_one_or_none()


async def _upsert_platform_response_record(
    db: AsyncSession,
    audit_id: int,
    prompt_id: int,
    platform: str,
    response_text: str | None,
    citations: list[dict],
    prompt_tokens: int,
    completion_tokens: int,
    response_model: str,
    finish_reason: str,
    search_enabled: bool,
    error: str | None,
    raw_response: dict | None = None,
    raw_response_text: str | None = None,
    search_metadata: dict | None = None,
    request_params: dict | None = None,
    parse_error: str | None = None,
    source_snapshot_hash: str | None = None,
) -> PlatformResponseRecord:
    """Upsert a platform response record with raw response archiving (Issue 1.2, 4.1)."""
    prr = await _get_platform_response_record(db, audit_id, prompt_id, platform)
    if prr is None:
        prr = PlatformResponseRecord(
            audit_id=audit_id,
            prompt_id=prompt_id,
            platform=platform,
            response_text=response_text,
            citations=citations,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            response_model=response_model,
            finish_reason=finish_reason,
            search_enabled=search_enabled,
            error=error,
            # Issue 1.2: Raw response archiving fields
            raw_response=raw_response,
            raw_response_text=raw_response_text,
            search_metadata=search_metadata,
            request_params=request_params,
            parse_error=parse_error,
            source_snapshot_hash=source_snapshot_hash,
        )
        db.add(prr)
    else:
        prr.response_text = response_text
        prr.citations = citations
        prr.prompt_tokens = prompt_tokens
        prr.completion_tokens = completion_tokens
        prr.response_model = response_model
        prr.finish_reason = finish_reason
        prr.search_enabled = search_enabled
        prr.error = error
        # Issue 1.2: Update raw response archiving fields
        prr.raw_response = raw_response
        prr.raw_response_text = raw_response_text
        prr.search_metadata = search_metadata
        prr.request_params = request_params
        prr.parse_error = parse_error
        prr.source_snapshot_hash = source_snapshot_hash
    await db.flush()
    return prr


async def _upsert_platform_run(
    db: AsyncSession,
    audit: Audit,
    stage_run: AuditStageRun,
    prompt_id: int,
    platform: str,
    started_at: datetime,
    finished_at: datetime,
    error_code: str | None = None,
    error_message: str | None = None,
    response_record_id: int | None = None,
) -> AuditPlatformRun:
    result = await db.execute(
        select(AuditPlatformRun)
        .where(
            AuditPlatformRun.audit_id == audit.id,
            AuditPlatformRun.prompt_id == prompt_id,
            AuditPlatformRun.platform == platform,
        )
        .order_by(AuditPlatformRun.attempt_no.desc())
    )
    existing_run = result.scalars().first()
    if existing_run is not None:
        existing_run.stage_run_id = stage_run.id
        existing_run.status = RunStatus.FAILED if error_message else RunStatus.COMPLETED
        existing_run.started_at = started_at
        existing_run.finished_at = finished_at
        existing_run.duration_ms = int((finished_at - started_at).total_seconds() * 1000)
        existing_run.error_code = error_code
        existing_run.error_message = error_message
        existing_run.response_record_id = response_record_id
        existing_run.worker_id = _WORKER_ID
        existing_run.retry_after = None
        await db.flush()
        return existing_run

    return await _start_platform_run(
        db,
        audit,
        stage_run,
        prompt_id=prompt_id,
        platform=platform,
        started_at=started_at,
        finished_at=finished_at,
        error_code=error_code,
        error_message=error_message,
        response_record_id=response_record_id,
    )


async def claim_audit(db: AsyncSession, audit_id: int) -> Audit | None:
    """Atomically claim a pending audit for execution.

    Returns the claimed audit row when the worker successfully acquires the
    execution lease. If the audit is already running, completed, or claimed by
    another worker, returns None.
    """
    now = utcnow()
    analysis_run_id = uuid4().hex
    result = await db.execute(
        update(Audit)
        .where(Audit.id == audit_id)
        .where(Audit.status == QueryStatus.PENDING)
        .values(
            status=QueryStatus.RUNNING,
            stage=AuditStage.QUEUED,
            stage_status=RunStatus.PENDING,
            stage_started_at=None,
            stage_updated_at=now,
            last_heartbeat_at=now,
            analysis_run_id=analysis_run_id,
            attempt_count=Audit.attempt_count + 1,
            error_code=None,
            error_message=None,
            recoverable_error=False,
            next_retry_at=None,
            locked_by_worker=_WORKER_ID,
            locked_until=now + timedelta(seconds=_LEASE_SECONDS),
        )
    )
    if result.rowcount != 1:
        await db.rollback()
        return None

    await db.commit()
    audit = await db.get(Audit, audit_id)
    if audit is not None:
        await db.refresh(audit)
    return audit


async def _start_stage(
    db: AsyncSession,
    audit: Audit,
    stage_name: AuditStage,
    input_snapshot: dict | None = None,
) -> AuditStageRun:
    now = utcnow()
    audit.stage = stage_name
    audit.stage_status = RunStatus.RUNNING
    audit.stage_started_at = audit.stage_started_at or now
    audit.stage_updated_at = now
    audit.last_heartbeat_at = now
    audit.locked_by_worker = _WORKER_ID
    audit.locked_until = now + timedelta(seconds=_LEASE_SECONDS)
    if audit.attempt_count <= 0:
        audit.attempt_count = 1

    stage_run = AuditStageRun(
        audit_id=audit.id,
        stage_name=stage_name.value,
        status=RunStatus.RUNNING,
        attempt_no=await _next_stage_attempt_no(db, audit.id, stage_name.value),
        started_at=now,
        worker_id=_WORKER_ID,
        input_snapshot=input_snapshot,
    )
    db.add(stage_run)
    await db.flush()
    await _append_event(
        db,
        audit,
        "stage_started",
        {"stage": stage_name.value, "attempt_no": stage_run.attempt_no},
        stage_name=stage_name.value,
    )
    return stage_run


async def _finish_stage(
    db: AsyncSession,
    audit: Audit,
    stage_run: AuditStageRun,
    status: RunStatus,
    output_snapshot: dict | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    now = utcnow()
    stage_run.status = status
    stage_run.finished_at = now
    stage_run.duration_ms = int((now - (stage_run.started_at or now)).total_seconds() * 1000)
    stage_run.output_snapshot = output_snapshot
    stage_run.error_code = error_code
    stage_run.error_message = error_message
    audit.stage_status = status
    audit.stage_updated_at = now
    audit.last_heartbeat_at = now
    if status in (RunStatus.COMPLETED, RunStatus.FAILED):
        audit.locked_by_worker = None
        audit.locked_until = None
    await _append_event(
        db,
        audit,
        "stage_finished",
        {
            "stage": stage_run.stage_name,
            "status": status.value,
            "attempt_no": stage_run.attempt_no,
            "error_code": error_code,
            "error_message": error_message,
        },
        stage_name=stage_run.stage_name,
    )


async def _start_platform_run(
    db: AsyncSession,
    audit: Audit,
    stage_run: AuditStageRun,
    prompt_id: int,
    platform: str,
    started_at: datetime,
    finished_at: datetime,
    error_code: str | None = None,
    error_message: str | None = None,
    response_record_id: int | None = None,
) -> AuditPlatformRun:
    platform_run = AuditPlatformRun(
        audit_id=audit.id,
        stage_run_id=stage_run.id,
        prompt_id=prompt_id,
        platform=platform,
        status=RunStatus.FAILED if error_message else RunStatus.COMPLETED,
        attempt_no=await _next_platform_attempt_no(db, audit.id, prompt_id, platform),
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=int((finished_at - started_at).total_seconds() * 1000),
        error_code=error_code,
        error_message=error_message,
        response_record_id=response_record_id,
        worker_id=_WORKER_ID,
    )
    db.add(platform_run)
    await db.flush()
    return platform_run


async def run_audit(audit_id: int) -> None:
    """Execute an audit by ID. Designed to be run as a background task."""
    async with async_session() as db:
        audit = await claim_audit(db, audit_id)
        if not audit:
            return

        logger.info("audit_started", audit_id=audit_id, project_id=audit.project_id)

        try:
            await _execute_audit(db, audit)
        except Exception as e:
            logger.exception("audit_failed", audit_id=audit_id, error=str(e))
            await _mark_audit_failed(db, audit_id, audit, str(e))
            publish(audit_id, PlatformEvent(type="audit_failed", error=str(e)))


async def _execute_audit(db: AsyncSession, audit: Audit) -> None:
    """Core audit execution logic."""
    result = await db.execute(
        select(Prompt).where(
            Prompt.project_id == audit.project_id,
            Prompt.deleted_at.is_(None)
        )
    )
    prompts = result.scalars().all()
    if not prompts:
        audit.status = QueryStatus.FAILED
        audit.error_message = "No prompts found for this project"
        audit.completed_at = utcnow()
        await db.commit()
        return

    brands_raw = audit.brands_json or []
    brands = [
        BrandData(
            id=b.get("id", ""),
            name=b.get("name", ""),
            aliases=b.get("aliases", []),
            is_competitor=b.get("is_competitor", False),
        )
        for b in brands_raw
    ]
    if not brands:
        audit.status = QueryStatus.FAILED
        audit.error_message = "No brands found for this project"
        audit.completed_at = utcnow()
        await db.commit()
        return

    if not audit.analysis_run_id:
        audit.analysis_run_id = uuid4().hex
        await db.flush()

    prompt_texts = [p.text for p in prompts]
    platforms = audit.platforms_json or []
    adapters = get_adapters(platforms)

    # Issue 2.1: Load platform configs and inject into adapters
    for adapter in adapters:
        config = await get_platform_config(db, adapter.platform_name)
        adapter.set_platform_config(config)
        runtime_context = {
            "analysis_run_id": audit.analysis_run_id,
            "audit_id": audit.id,
            "project_id": audit.project_id,
        }
        if hasattr(adapter, "set_runtime_context"):
            adapter.set_runtime_context(runtime_context)

    query_stage = await _start_stage(
        db,
        audit,
        AuditStage.QUERYING,
        input_snapshot={
            "prompt_count": len(prompts),
            "platforms": platforms,
            "brand_count": len(brands),
        },
    )
    await db.commit()

    async def _run_platform_query(name: str, adapter, prompt_batch: list[str]) -> PlatformQueryOutcome:
        started_at = utcnow()
        started_monotonic = time.monotonic()
        logger.info(
            "audit_platform_query_started",
            audit_id=audit.id,
            project_id=audit.project_id,
            platform=name,
            prompt_count=len(prompt_batch),
            adapter=adapter.__class__.__name__,
        )
        try:
            responses = await _query_platform(name, adapter, prompt_batch)
            finished_at = utcnow()
            duration_ms = int((time.monotonic() - started_monotonic) * 1000)
            logger.info(
                "audit_platform_query_completed",
                audit_id=audit.id,
                project_id=audit.project_id,
                platform=name,
                prompt_count=len(prompt_batch),
                response_count=len(responses),
                duration_ms=duration_ms,
            )
            return PlatformQueryOutcome(
                platform_name=name,
                responses=responses,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
            )
        except Exception as e:
            finished_at = utcnow()
            duration_ms = int((time.monotonic() - started_monotonic) * 1000)
            logger.error(
                "audit_platform_query_failed",
                audit_id=audit.id,
                project_id=audit.project_id,
                platform=name,
                prompt_count=len(prompt_batch),
                duration_ms=duration_ms,
                error=str(e),
            )
            failure_responses = [
                PlatformResponse(
                    platform=name,
                    prompt=prompt_text,
                    response_text="",
                    error_code=ErrorCode.UNKNOWN,
                    error_message=str(e),
                )
                for prompt_text in prompt_batch
            ]
            return PlatformQueryOutcome(
                platform_name=name,
                responses=failure_responses,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                error=e,
            )

    for adapter in adapters:
        await _append_event(
            db,
            audit,
            "platform_start",
            {
                "platform": adapter.platform_name,
                "prompt_count": len(prompt_texts),
            },
            stage_name=query_stage.stage_name,
        )
        publish(audit.id, PlatformEvent(type="platform_start", platform=adapter.platform_name))
    await db.commit()

    tasks = [
        asyncio.create_task(_run_platform_query(adapter.platform_name, adapter, prompt_texts))
        for adapter in adapters
    ]
    outcomes = [await coro for coro in asyncio.as_completed(tasks)]

    response_records: dict[tuple[str, str], PlatformResponseRecord] = {}
    all_responses: list[tuple[str, PlatformResponse]] = []
    platform_error_count = 0

    for outcome in outcomes:
        platform_name = outcome.platform_name
        all_responses.extend((platform_name, resp) for resp in outcome.responses)
        if outcome.error:
            platform_error_count += 1
            await _append_event(
                db,
                audit,
                "platform_error",
                {"platform": platform_name, "error": str(outcome.error)},
                stage_name=query_stage.stage_name,
            )
            publish(audit.id, PlatformEvent(type="platform_error", platform=platform_name, error=str(outcome.error)))
        else:
            await _append_event(
                db,
                audit,
                "platform_done",
                {"platform": platform_name},
                stage_name=query_stage.stage_name,
            )
            publish(audit.id, PlatformEvent(type="platform_done", platform=platform_name))

    await _finish_stage(
        db,
        audit,
        query_stage,
        RunStatus.COMPLETED,
        output_snapshot={
            "platform_count": len(platforms),
            "platform_error_count": platform_error_count,
            "response_count": len(all_responses),
        },
    )
    await db.commit()

    persisting_stage = await _start_stage(
        db,
        audit,
        AuditStage.PERSISTING,
        input_snapshot={
            "response_count": len(all_responses),
            "platform_error_count": platform_error_count,
        },
    )
    await db.commit()

    for outcome in outcomes:
        platform_name = outcome.platform_name
        for resp in outcome.responses:
            prompt_obj = next((p for p in prompts if p.text == resp.prompt), None)
            if not prompt_obj:
                continue

            extracted = []
            if resp.success and resp.response_text:
                extracted = extract_sources(resp.response_text, api_citations=resp.citations)

            citations_json = [
                {"domain": s.domain, "urls": s.urls, "title": s.title}
                for s in extracted
            ]

            prr = await _upsert_platform_response_record(
                db,
                audit.id,
                prompt_obj.id,
                platform_name,
                resp.response_text if resp.success else None,
                citations_json,
                resp.prompt_tokens,
                resp.completion_tokens,
                resp.response_model,
                resp.finish_reason,
                resp.search_enabled,
                resp.error_message if not resp.success else None,
                # Issue 4.1: Raw response archiving
                raw_response=resp.raw_response,
                raw_response_text=resp.raw_response_text,
                search_metadata=resp.search_metadata,
                request_params=resp.request_params,
                parse_error=resp.parse_error,
                source_snapshot_hash=_build_source_snapshot_hash(
                    resp.citations,
                    resp.search_metadata,
                    resp.request_params,
                ),
            )
            response_records[(resp.prompt, platform_name)] = prr

            await _upsert_platform_run(
                db,
                audit,
                query_stage,
                prompt_id=prompt_obj.id,
                platform=platform_name,
                started_at=outcome.started_at,
                finished_at=outcome.finished_at,
                error_code=resp.error_code.value if resp.error_code else None,
                error_message=resp.error_message if not resp.success else None,
                response_record_id=prr.id,
            )

            if extracted:
                await _upsert_source_citations(
                    db, audit.project_id, audit.id, platform_name, extracted
                )

    await db.commit()

    await _finish_stage(
        db,
        audit,
        persisting_stage,
        RunStatus.COMPLETED,
        output_snapshot={
            "response_record_count": len(response_records),
            "source_citation_count": len(response_records),
        },
    )
    await db.commit()

    calculating_stage = await _start_stage(
        db,
        audit,
        AuditStage.CALCULATING,
        input_snapshot={
            "response_record_count": len(response_records),
            "brand_count": len(brands),
        },
    )
    await db.execute(delete(QueryResult).where(QueryResult.audit_id == audit.id))
    await db.commit()

    results = []
    for platform_name, resp in all_responses:
        prompt_obj = next((p for p in prompts if p.text == resp.prompt), None)
        if not prompt_obj:
            continue

        key = (resp.prompt, platform_name)
        prr = response_records.get(key)

        for brand in brands:
            qr = QueryResult(
                audit_id=audit.id,
                prompt_id=prompt_obj.id,
                brand_id=brand.id,
                platform=platform_name,
                response_text=resp.response_text if resp.success else None,
                response_record_id=prr.id if prr else None,
                error=resp.error_message if not resp.success else None,
            )

            if resp.success and resp.response_text and not is_degraded_response(resp):
                mentions = detect_mentions(resp.response_text, brand.name, brand.aliases, industry="")
                if mentions:
                    best = max(mentions, key=lambda m: m.confidence)
                    qr.mention_found = True
                    qr.mention_position = best.position
                    qr.mention_context = best.context
                    qr.mention_confidence = best.confidence
                    qr.is_recommended = best.is_recommended

                    if best.is_recommended:
                        all_recommended: list[tuple[str, int]] = []
                        for b in brands:
                            b_mentions = detect_mentions(resp.response_text, b.name, b.aliases, industry="")
                            for m in b_mentions:
                                if m.is_recommended:
                                    all_recommended.append((b.name, m.position))
                        all_recommended.sort(key=lambda x: x[1])
                        rank = next(
                            (i + 1 for i, (name, _) in enumerate(all_recommended) if name == brand.name),
                            None,
                        )
                        qr.recommendation_rank = rank

            results.append(qr)

    db.add_all(results)
    await db.commit()

    await _finish_stage(
        db,
        audit,
        calculating_stage,
        RunStatus.COMPLETED,
        output_snapshot={
            "query_result_count": len(results),
            "completed_response_count": sum(1 for _, r in all_responses if r.success),
            "failed_response_count": sum(1 for _, r in all_responses if not r.success),
        },
    )
    await db.commit()

    final_stage = await _start_stage(
        db,
        audit,
        AuditStage.FINALIZING,
        input_snapshot={
            "query_result_count": len(results),
            "response_record_count": len(response_records),
        },
    )
    await db.commit()

    error_count = sum(1 for _, r in all_responses if not r.success)
    total = len(all_responses)
    if error_count == total:
        audit.status = QueryStatus.FAILED
        audit.error_message = "All platform queries failed"
        final_run_status = RunStatus.FAILED
    elif error_count > 0:
        audit.status = QueryStatus.PARTIAL
        final_run_status = RunStatus.COMPLETED
    else:
        audit.status = QueryStatus.COMPLETED
        final_run_status = RunStatus.COMPLETED

    audit.stage = AuditStage.FAILED if audit.status == QueryStatus.FAILED else (
        AuditStage.PARTIAL if audit.status == QueryStatus.PARTIAL else AuditStage.COMPLETED
    )
    audit.completed_at = utcnow()
    audit.locked_by_worker = None
    audit.locked_until = None
    await db.commit()

    await _finish_stage(
        db,
        audit,
        final_stage,
        final_run_status,
        output_snapshot={
            "final_status": audit.status.value,
            "error_count": error_count,
            "total": total,
        },
        error_message=audit.error_message if audit.status == QueryStatus.FAILED else None,
    )
    await db.commit()

    await _append_event(
        db,
        audit,
        "audit_failed" if audit.status == QueryStatus.FAILED else "audit_completed",
        {
            "status": audit.status.value,
            "error_count": error_count,
            "total": total,
        },
    )
    await db.commit()

    publish(audit.id, PlatformEvent(type="audit_failed" if audit.status == QueryStatus.FAILED else "audit_done"))
    logger.info(
        "audit_completed",
        audit_id=audit.id,
        status=audit.status.value,
        results=len(results),
        response_records=len(response_records),
    )


async def _mark_audit_failed(
    db: AsyncSession,
    audit_id: int,
    audit: Audit | None,
    error_message: str,
) -> None:
    """Persist a failed audit state without leaving the session poisoned."""
    try:
        await db.rollback()
    except Exception as rollback_error:
        logger.warning(
            "audit_failed_rollback_error",
            audit_id=audit_id,
            error=str(rollback_error),
        )

    async with async_session() as fallback_db:
        fallback_audit = await fallback_db.get(Audit, audit_id)
        if not fallback_audit:
            return

        fallback_audit.status = QueryStatus.FAILED
        fallback_audit.stage = AuditStage.FAILED
        fallback_audit.stage_status = RunStatus.FAILED
        fallback_audit.error_message = error_message
        fallback_audit.completed_at = utcnow()
        fallback_audit.last_heartbeat_at = utcnow()
        fallback_audit.locked_by_worker = None
        fallback_audit.locked_until = None
        try:
            await _append_event(
                fallback_db,
                fallback_audit,
                "audit_failed",
                {"error": error_message},
                stage_name=fallback_audit.stage.value,
            )
            await fallback_db.commit()
        except Exception as fallback_commit_error:
            logger.error(
                "audit_failed_fallback_persist_error",
                audit_id=audit_id,
                error=str(fallback_commit_error),
            )


async def _upsert_source_citations(
    db: AsyncSession,
    project_id: str,
    audit_id: int,
    platform: str,
    sources: list,
) -> None:
    """Upsert SourceCitation rows using INSERT ON DUPLICATE KEY UPDATE."""
    for source in sources:
        urls_json = source.urls if source.urls else []

        await db.execute(
            text(
                """
                INSERT INTO source_citations
                    (project_id, audit_id, domain, urls, citation_count, platform, created_at)
                VALUES
                    (:project_id, :audit_id, :domain, :urls, 1, :platform, NOW())
                ON DUPLICATE KEY UPDATE
                    citation_count = citation_count + 1,
                    urls = JSON_MERGE_PRESERVE(urls, :urls)
            """
            ),
            {
                "project_id": project_id,
                "audit_id": audit_id,
                "domain": source.domain,
                "urls": json.dumps(urls_json),
                "platform": platform,
            },
        )


async def _query_platform(
    platform_name: str, adapter, prompt_texts: list[str]
) -> list[PlatformResponse]:
    """Query a single platform with all prompts."""
    return await adapter.query(prompt_texts)
