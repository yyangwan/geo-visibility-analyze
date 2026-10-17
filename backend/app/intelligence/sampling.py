"""Atomic device binding, immutable attempts and explicit independent sampling."""

import secrets
from datetime import UTC
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app.config import settings
from app.intelligence.blob import digest
from app.intelligence.capture_contract import CaptureV2, SamplingRequest
from app.intelligence.models import (
    AuditSample,
    CaptureAttempt,
    CapturedResponse,
    PromptVersion,
    SamplingAudit,
)
from app.intelligence.semantics import stable_hash
from app.models.device_gateway import DeviceTask
from app.models.models import Audit, AuditStage, Prompt, QueryStatus
from app.utils.timezone import utcnow


async def dispatch_attempt(db, sample, attempt_no):
    existing = await db.scalar(
        select(CaptureAttempt).where(
            CaptureAttempt.project_id == sample.project_id,
            CaptureAttempt.sample_id == sample.id,
            CaptureAttempt.attempt_no == attempt_no,
        )
    )
    if existing:
        return existing
    prompt = await db.get(PromptVersion, sample.prompt_version_id)
    if prompt is None or prompt.project_id != sample.project_id:
        raise ValueError("Prompt version outside sample project")
    task_id = str(uuid4())
    task = DeviceTask(
        id=task_id,
        project_id=sample.project_id,
        platform="yuanbao" if sample.platform == "hunyuan" else sample.platform,
        surface=sample.surface,
        target_gateway_id=settings.mobile_app_capture_gateway_id or None,
        task_type="browser.prompt" if sample.surface == "web" else "appium.prompt",
        payload={
            "prompt": prompt.text,
            "capture_contract_version": 2,
            "sample_id": sample.id,
            "attempt_no": attempt_no,
            "prompt_version_id": prompt.id,
            "conditions": sample.conditions,
            "timeout_seconds": settings.mobile_app_capture_task_timeout_seconds,
        },
        priority=settings.mobile_app_capture_priority,
        max_attempts=1,
        status="pending",
        available_at=utcnow(),
        idempotency_key=f"capture-v2:{sample.id}:{attempt_no}",
    )
    attempt = CaptureAttempt(
        project_id=sample.project_id,
        sample_id=sample.id,
        attempt_no=attempt_no,
        device_task_id=task_id,
    )
    db.add_all([task, attempt])
    await db.flush()
    return attempt


async def create_sampling_audit(
    db, project_id: str, request: SamplingRequest, brands: list[dict]
):
    request_hash = stable_hash([request.model_dump(mode="json", by_alias=True), brands])
    previous = await db.scalar(
        select(SamplingAudit).where(
            SamplingAudit.project_id == project_id,
            SamplingAudit.idempotency_key == request.idempotency_key,
        )
    )
    if previous:
        if previous.request_hash != request_hash:
            raise HTTPException(
                409, "Sampling idempotency key reused with different configuration"
            )
        return await db.get(Audit, previous.audit_id), previous.expected_samples
    prompts = (
        await db.scalars(
            select(Prompt)
            .where(
                Prompt.project_id == project_id,
                Prompt.deleted_at.is_(None),
            )
            .order_by(Prompt.id)
            .with_for_update()
        )
    ).all()
    # Lock the project prompt set in stable order so concurrent requests using
    # the same key but disjoint prompt selections still converge deterministically.
    prompts = [prompt for prompt in prompts if prompt.id in request.prompt_ids]
    if len(prompts) != len(request.prompt_ids):
        raise HTTPException(404, "Prompt not found in project")
    previous = await db.scalar(
        select(SamplingAudit)
        .where(
            SamplingAudit.project_id == project_id,
            SamplingAudit.idempotency_key == request.idempotency_key,
        )
        .with_for_update()
    )
    if previous:
        if previous.request_hash != request_hash:
            raise HTTPException(
                409, "Sampling idempotency key reused with different configuration"
            )
        return await db.get(Audit, previous.audit_id), previous.expected_samples
    audit = Audit(
        project_id=project_id,
        status=QueryStatus.RUNNING,
        stage=AuditStage.QUERYING,
        platforms_json=request.platforms,
        brands_json=brands,
    )
    db.add(audit)
    await db.flush()
    expected = len(prompts) * len(request.platforms) * request.rounds
    db.add(
        SamplingAudit(
            project_id=project_id,
            audit_id=audit.id,
            idempotency_key=request.idempotency_key,
            request_hash=request_hash,
            expected_samples=expected,
            max_attempts=settings.mobile_app_capture_max_attempts,
        )
    )
    for prompt in prompts:
        text_hash = digest(prompt.text.encode())
        version = await db.scalar(
            select(PromptVersion).where(
                PromptVersion.project_id == project_id,
                PromptVersion.prompt_id == prompt.id,
                PromptVersion.text_hash == text_hash,
            )
        )
        if version is None:
            version = PromptVersion(
                project_id=project_id,
                prompt_id=prompt.id,
                text=prompt.text,
                text_hash=text_hash,
                category=prompt.category.value,
            )
            db.add(version)
            await db.flush()
        for platform in request.platforms:
            for sample_index in range(1, request.rounds + 1):
                conditions = request.conditions.model_dump(by_alias=True)
                sample = AuditSample(
                    project_id=project_id,
                    audit_id=audit.id,
                    prompt_version_id=version.id,
                    platform=platform,
                    surface=request.surface,
                    sample_index=sample_index,
                    conditions=conditions,
                    condition_hash=stable_hash([request.surface, conditions]),
                )
                db.add(sample)
                await db.flush()
                await dispatch_attempt(db, sample, 1)
    await db.flush()
    return audit, expected


async def refresh_event(db, project_id, audit_id, reason, event_id):
    from app.intelligence.refresh import request_refresh

    await request_refresh(db, project_id, audit_id, reason, event_id)


async def synchronize_capture_state(db, project_id, audit_id):
    audit = await db.scalar(
        select(Audit)
        .where(Audit.id == audit_id, Audit.project_id == project_id)
        .with_for_update()
    )
    samples = (
        await db.scalars(
            select(AuditSample).where(
                AuditSample.project_id == project_id, AuditSample.audit_id == audit_id
            )
        )
    ).all()
    terminal = all(
        sample.status in ("sealed", "failed", "cancelled") for sample in samples
    )
    if terminal and samples:
        sealed = sum(sample.status == "sealed" for sample in samples)
        audit.status = (
            QueryStatus.COMPLETED
            if sealed == len(samples)
            else QueryStatus.PARTIAL
            if sealed
            else QueryStatus.FAILED
        )
        audit.stage = (
            AuditStage.COMPLETED
            if sealed == len(samples)
            else AuditStage.PARTIAL
            if sealed
            else AuditStage.FAILED
        )
        audit.completed_at = utcnow()


async def accept_capture(db, task, lease_token: str, result: dict):
    try:
        capture = CaptureV2.model_validate(result)
    except ValueError:
        raise HTTPException(
            422, "Invalid capture-v2 result; facts were not stored"
        ) from None
    if (
        capture.task_id != task.id
        or capture.platform != task.platform
        or capture.surface != task.surface
    ):
        raise HTTPException(409, "Capture result differs from issued task")
    attempt = await db.scalar(
        select(CaptureAttempt)
        .where(
            CaptureAttempt.device_task_id == task.id,
            CaptureAttempt.project_id == task.project_id,
        )
        .with_for_update()
    )
    if attempt is None:
        raise HTTPException(409, "Capture attempt binding missing")
    payload = capture.model_dump(mode="json", by_alias=True)
    accepted_hash = stable_hash(payload)
    token_hash = digest(lease_token.encode())
    if attempt.accepted_hash:
        if attempt.accepted_hash == accepted_hash and secrets.compare_digest(
            attempt.accepted_token_hash or "", token_hash
        ):
            return
        raise HTTPException(409, "Conflicting duplicate capture result")
    if (
        task.status != "leased"
        or task.lease_token_hash != token_hash
        or not task.lease_expires_at
        or task.lease_expires_at <= utcnow()
    ):
        raise HTTPException(409, "Capture lease expired")
    sample = await db.scalar(
        select(AuditSample)
        .where(
            AuditSample.id == attempt.sample_id,
            AuditSample.project_id == task.project_id,
        )
        .with_for_update()
    )
    if sample is None or sample.status != "pending":
        raise HTTPException(409, "Sample is already sealed or outside project")
    # User-requested conditions are not proof of what the platform actually applied.
    response = CapturedResponse(
        project_id=task.project_id,
        sample_id=sample.id,
        attempt_no=attempt.attempt_no,
        text=capture.answer.text,
        content_hash=capture.answer.content_hash,
        payload=payload,
        observed_at=capture.observed_at.astimezone(UTC).replace(tzinfo=None),
    )
    db.add(response)
    await db.flush()
    attempt.status, attempt.accepted_hash, attempt.accepted_token_hash = (
        "accepted",
        accepted_hash,
        token_hash,
    )
    attempt.response_id = response.id
    sample.selected_response_id, sample.status = response.id, "sealed"
    await synchronize_capture_state(db, sample.project_id, sample.audit_id)
    await refresh_event(
        db, sample.project_id, sample.audit_id, "sample_sealed", sample.id
    )
    # Recheck at the transaction boundary before any fact can become visible.
    if task.lease_expires_at <= utcnow():
        raise HTTPException(409, "Capture lease expired while accepting facts")


async def capture_failed(db, task, code, *, retryable=True):
    attempt = await db.scalar(
        select(CaptureAttempt)
        .where(CaptureAttempt.device_task_id == task.id)
        .with_for_update()
    )
    if attempt is None or attempt.status != "pending":
        return
    sample = await db.scalar(
        select(AuditSample)
        .where(
            AuditSample.id == attempt.sample_id,
            AuditSample.project_id == task.project_id,
        )
        .with_for_update()
    )
    if sample is None or sample.status != "pending":
        return
    attempt.status, attempt.error_code = "failed", code[:100]
    config = await db.scalar(
        select(SamplingAudit).where(
            SamplingAudit.audit_id == sample.audit_id,
            SamplingAudit.project_id == sample.project_id,
        )
    )
    if retryable and config and attempt.attempt_no < config.max_attempts:
        await dispatch_attempt(db, sample, attempt.attempt_no + 1)
    else:
        sample.status = "failed"
        await synchronize_capture_state(db, sample.project_id, sample.audit_id)
        await refresh_event(
            db, sample.project_id, sample.audit_id, "capture_terminal", sample.id
        )


async def reconcile_capture_failures(db):
    expired = (
        await db.scalars(
            select(DeviceTask)
            .join(CaptureAttempt, CaptureAttempt.device_task_id == DeviceTask.id)
            .where(
                CaptureAttempt.status == "pending",
                CaptureAttempt.project_id == DeviceTask.project_id,
                DeviceTask.status == "leased",
                DeviceTask.lease_expires_at <= utcnow(),
            )
            .with_for_update()
            .limit(100)
        )
    ).all()
    for task in expired:
        task.status, task.error_code, task.completed_at = (
            "failed",
            "lease_expired",
            utcnow(),
        )
        task.lease_owner, task.lease_token_hash, task.lease_expires_at = (
            None,
            None,
            None,
        )
        await capture_failed(db, task, "lease_expired")
    tasks = (
        await db.scalars(
            select(DeviceTask)
            .join(CaptureAttempt, CaptureAttempt.device_task_id == DeviceTask.id)
            .where(
                CaptureAttempt.status == "pending",
                CaptureAttempt.project_id == DeviceTask.project_id,
                DeviceTask.status.in_(["failed", "cancelled"]),
            )
            .limit(100)
        )
    ).all()
    for task in tasks:
        await capture_failed(
            db,
            task,
            task.error_code or "device_task_failed",
            retryable=task.status != "cancelled",
        )
