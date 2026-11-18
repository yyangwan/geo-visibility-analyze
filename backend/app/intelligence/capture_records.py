"""Read collected facts independently of analysis runs and model availability."""

import gzip
from typing import Literal

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select

from app.intelligence.blob import IntegrityError, configured_store, digest
from app.intelligence.models import (
    AnalysisJob,
    AuditSample,
    CaptureAttempt,
    CapturedResponse,
    CitationObservation,
    IntelligenceRun,
    PromptVersion,
    SourceFetch,
    SourceSnapshot,
)
from app.intelligence.sources import SourceBlocked, normalized_url
from app.models.device_gateway import DeviceTask
from app.utils.timezone import utc_isoformat

CaptureState = Literal["pending", "received", "failed"]
SAMPLE_STATES = {
    "pending": ("pending",),
    "received": ("sealed",),
    "failed": ("failed", "cancelled"),
}


def sample_state(sample):
    if sample.status == "sealed":
        return "received"
    if sample.status in SAMPLE_STATES["failed"]:
        return "failed"
    return "pending"


def attempt_reason(code):
    if code is None:
        return None
    if code in {
        "timeout",
        "lease_expired",
        "gateway_device_wait_timeout",
    } or code.endswith("_timeout"):
        return "capture_timeout"
    if code == "gateway_device_unavailable":
        return "capture_waiting"
    if code in {"cancelled", "canceled"}:
        return "capture_cancelled"
    return "capture_error"


def sample_scope(project_id, audit_id):
    return (AuditSample.project_id == project_id, AuditSample.audit_id == audit_id)


def response_join():
    return and_(
        CapturedResponse.project_id == AuditSample.project_id,
        CapturedResponse.sample_id == AuditSample.id,
        CapturedResponse.id == AuditSample.selected_response_id,
    )


def prompt_join():
    return and_(
        PromptVersion.project_id == AuditSample.project_id,
        PromptVersion.id == AuditSample.prompt_version_id,
    )


def verified_snapshot_join():
    return and_(
        SourceSnapshot.project_id == SourceFetch.project_id,
        SourceSnapshot.document_id == SourceFetch.document_id,
        SourceSnapshot.id == SourceFetch.snapshot_id,
        SourceSnapshot.integrity_status == "verified",
    )


def archived_source_count(project_id, audit_id):
    archive = (
        select(SourceFetch.id)
        .join(SourceSnapshot, verified_snapshot_join())
        .where(
            SourceFetch.project_id == project_id,
            SourceFetch.audit_id == audit_id,
            SourceFetch.document_id == CitationObservation.document_id,
        )
        .exists()
    )
    return (
        select(func.count(CitationObservation.id))
        .where(
            CitationObservation.project_id == project_id,
            CitationObservation.audit_id == audit_id,
            CitationObservation.response_id == CapturedResponse.id,
            archive,
        )
        .correlate(CapturedResponse)
        .scalar_subquery()
        .label("archived_count")
    )


async def list_capture_records(
    db, project_id, audit_id, *, platform, prompt_id, status, cursor, limit
):
    scope = sample_scope(project_id, audit_id)
    filters = []
    if platform is not None:
        filters.append(AuditSample.platform == platform)
    if prompt_id is not None:
        filters.append(PromptVersion.prompt_id == prompt_id)
    if cursor is not None:
        after = (
            await db.execute(
                select(AuditSample.created_at, AuditSample.id)
                .join(PromptVersion, prompt_join())
                .where(*scope, AuditSample.id == cursor, *filters)
            )
        ).one_or_none()
        if after is None:
            raise HTTPException(400, "Invalid capture record cursor")
        filters.append(
            or_(
                AuditSample.created_at > after.created_at,
                and_(
                    AuditSample.created_at == after.created_at,
                    AuditSample.id > after.id,
                ),
            )
        )

    if status is not None:
        filters.append(AuditSample.status.in_(SAMPLE_STATES[status]))

    rows = (
        await db.execute(
            select(
                AuditSample,
                PromptVersion.prompt_id,
                func.substr(PromptVersion.text, 1, 160).label("prompt_excerpt"),
                CapturedResponse.payload["answer"]["status"]
                .as_string()
                .label("answer_status"),
                CapturedResponse.payload["sourcePanel"]["capturedCount"]
                .as_integer()
                .label("source_count"),
                archived_source_count(project_id, audit_id),
            )
            .join(PromptVersion, prompt_join())
            .outerjoin(CapturedResponse, response_join())
            .where(*scope, *filters)
            .order_by(AuditSample.created_at, AuditSample.id)
            .limit(limit + 1)
        )
    ).all()
    items = []
    for (
        sample,
        question_id,
        excerpt,
        answer_status,
        source_count,
        archived_count,
    ) in rows[:limit]:
        items.append(
            {
                "id": sample.id,
                "prompt_id": question_id,
                "prompt_excerpt": excerpt,
                "platform": sample.platform,
                "status": sample_state(sample),
                "answer_status": answer_status,
                "source_count": source_count or 0,
                "archived_count": archived_count,
            }
        )
    return {
        "items": items,
        "next_cursor": items[-1]["id"] if len(rows) > limit else None,
    }


async def get_sample(db, project_id, audit_id, sample_id):
    row = (
        await db.execute(
            select(AuditSample, PromptVersion, CapturedResponse)
            .join(PromptVersion, prompt_join())
            .outerjoin(CapturedResponse, response_join())
            .where(*sample_scope(project_id, audit_id), AuditSample.id == sample_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(404, "Capture record not found")
    return row


def safe_link(value):
    if value:
        try:
            normalized_url(value)
            return value
        except SourceBlocked:
            pass
    return None


def export_source(source, observation, fetch, snapshot, queued):
    url = safe_link(source["url"])
    if snapshot is not None:
        archive = {
            "status": "archived",
            "snapshot_id": snapshot.id,
            "archived_at": utc_isoformat(fetch.created_at),
            "completeness": snapshot.completeness,
        }
    elif source["urlResolution"] != "exact" or source["kind"] == "named_inference":
        archive = {"status": "unavailable", "reason": "link_unresolved"}
    elif url is None:
        archive = {"status": "unavailable", "reason": "link_unsupported"}
    elif queued:
        archive = {"status": "pending"}
    elif observation is None:
        archive = {"status": "not_started"}
    elif observation.document_id is None:
        archive = {"status": "unavailable", "reason": "link_unsupported"}
    elif fetch is None:
        archive = {"status": "not_started"}
    else:
        archive = {
            "status": "unavailable",
            "reason": "source_blocked"
            if fetch and fetch.status == "blocked"
            else "source_unavailable",
        }
    return {
        "ordinal": source["ordinal"],
        "kind": source["kind"],
        "title": source["title"],
        "url": url,
        "url_resolution": source["urlResolution"],
        "snippet": source["snippet"],
        "archive": archive,
    }


def source_fetches(project_id, audit_id):
    return (
        select(SourceFetch, SourceSnapshot)
        .outerjoin(SourceSnapshot, verified_snapshot_join())
        .where(SourceFetch.project_id == project_id, SourceFetch.audit_id == audit_id)
        .order_by(SourceFetch.created_at.desc(), SourceFetch.id.desc())
    )


async def captured_sources(db, project_id, audit_id, response):
    observations = (
        await db.scalars(
            select(CitationObservation).where(
                CitationObservation.project_id == project_id,
                CitationObservation.audit_id == audit_id,
                CitationObservation.response_id == response.id,
            )
        )
    ).all()
    by_ordinal = {observation.ordinal: observation for observation in observations}
    document_ids = {
        observation.document_id
        for observation in observations
        if observation.document_id
    }
    saved, latest, queued = {}, {}, set()
    if document_ids:
        fetches = await db.execute(
            source_fetches(project_id, audit_id).where(
                SourceFetch.document_id.in_(document_ids)
            )
        )
        for fetch, snapshot in fetches:
            latest.setdefault(fetch.document_id, fetch)
            if snapshot is not None:
                saved.setdefault(fetch.document_id, (fetch, snapshot))
        queued = set(
            await db.scalars(
                select(AnalysisJob.subject_id).where(
                    AnalysisJob.project_id == project_id,
                    AnalysisJob.kind == "fetch_document",
                    AnalysisJob.subject_id.in_(document_ids),
                    AnalysisJob.payload["audit_id"].as_integer() == audit_id,
                    AnalysisJob.status.in_(("queued", "leased", "retry_wait")),
                )
            )
        )

    items = []
    for source in response.payload["sources"]:
        observation = by_ordinal.get(source["ordinal"])
        document_id = observation.document_id if observation is not None else None
        fetch, snapshot = saved.get(document_id, (latest.get(document_id), None))
        items.append(
            export_source(source, observation, fetch, snapshot, document_id in queued)
        )
    return items


async def capture_record(db, project_id, audit_id, sample_id):
    sample, prompt, response = await get_sample(db, project_id, audit_id, sample_id)
    attempts = await db.execute(
        select(
            CaptureAttempt.attempt_no,
            CaptureAttempt.status,
            CaptureAttempt.error_code,
            DeviceTask.status.label("task_status"),
            DeviceTask.error_code.label("task_error"),
            DeviceTask.started_at,
            DeviceTask.completed_at,
        )
        .outerjoin(
            DeviceTask,
            and_(
                DeviceTask.project_id == CaptureAttempt.project_id,
                DeviceTask.id == CaptureAttempt.device_task_id,
            ),
        )
        .where(
            CaptureAttempt.project_id == project_id,
            CaptureAttempt.sample_id == sample.id,
        )
        .order_by(CaptureAttempt.attempt_no)
    )
    history = [
        {
            "number": attempt.attempt_no,
            "status": attempt.task_status or attempt.status,
            "reason": attempt_reason(attempt.error_code or attempt.task_error)
            if attempt.status != "accepted"
            else None,
            "started_at": utc_isoformat(attempt.started_at),
            "ended_at": utc_isoformat(attempt.completed_at),
        }
        for attempt in attempts
    ]
    answer, sources, panel = None, [], None
    if response is not None:
        answer = {
            "id": response.id,
            "text": response.text,
            "status": response.payload["answer"]["status"],
            "observed_at": utc_isoformat(response.observed_at),
        }
        sources = await captured_sources(db, project_id, audit_id, response)
        from app.intelligence.visibility_flow import find_flow

        flow = await find_flow(db, project_id, audit_id)
        if flow and flow.payload.get("run_id"):
            frozen_sources = await db.scalar(
                select(IntelligenceRun.manifest["sources"]).where(
                    IntelligenceRun.project_id == project_id,
                    IntelligenceRun.audit_id == audit_id,
                    IntelligenceRun.id == flow.payload["run_id"],
                )
            )
            frozen = {
                item["ordinal"]: item["snapshot_id"]
                for item in frozen_sources or []
                if item["response_id"] == response.id
            }
            for source in sources:
                archive = source["archive"]
                if archive["status"] == "archived":
                    archive["used_in_analysis"] = archive["snapshot_id"] == frozen.get(
                        source["ordinal"]
                    )
        panel = response.payload.get("sourcePanel")
    return {
        "id": sample.id,
        "platform": sample.platform,
        "status": sample_state(sample),
        "prompt": {"id": prompt.prompt_id, "text": prompt.text},
        "attempts": history,
        "answer": answer,
        "sources": sources,
        "source_panel": panel,
    }


async def capture_source_text(
    db, project_id, audit_id, sample_id, ordinal, snapshot_id
):
    _, _, response = await get_sample(db, project_id, audit_id, sample_id)
    if response is None:
        raise HTTPException(404, "Saved citation not found")
    row = (
        await db.execute(
            source_fetches(project_id, audit_id)
            .join(
                CitationObservation,
                and_(
                    CitationObservation.project_id == SourceFetch.project_id,
                    CitationObservation.audit_id == SourceFetch.audit_id,
                    CitationObservation.document_id == SourceFetch.document_id,
                ),
            )
            .where(
                CitationObservation.response_id == response.id,
                CitationObservation.ordinal == ordinal,
                SourceSnapshot.id == snapshot_id,
            )
            .limit(1)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(404, "Saved citation not found")
    fetch, snapshot = row
    try:
        data = await configured_store().get(snapshot.text_key, snapshot.text_sha256)
    except (IntegrityError, FileNotFoundError):
        raise HTTPException(409, "Saved citation text cannot be verified") from None
    except Exception:
        raise HTTPException(
            503, "Citation material storage is temporarily unavailable"
        ) from None
    try:
        text = gzip.decompress(data).decode("utf-8")
    except (OSError, EOFError, UnicodeError):
        raise HTTPException(409, "Saved citation text cannot be verified") from None
    if digest(text.encode()) != snapshot.body_hash:
        raise HTTPException(409, "Saved citation text cannot be verified")
    return {
        "snapshot_id": snapshot.id,
        "text": text,
        "archived_at": utc_isoformat(fetch.created_at),
        "completeness": snapshot.completeness,
    }
