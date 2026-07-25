import hashlib
import secrets
import uuid
from datetime import datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.device_gateway_schemas import DeviceTaskCreate, GatewayHeartbeatRequest
from app.config import settings
from app.models.device_gateway import DeviceGateway, DeviceTask


def utcnow() -> datetime:
    return datetime.utcnow()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def touch_gateway(
    db: AsyncSession,
    gateway_id: str,
    heartbeat: GatewayHeartbeatRequest | None = None,
) -> DeviceGateway:
    gateway = await db.get(DeviceGateway, gateway_id)
    if gateway is None:
        gateway = DeviceGateway(id=gateway_id)
        db.add(gateway)

    gateway.status = heartbeat.status if heartbeat else "online"
    gateway.last_seen_at = utcnow()
    if heartbeat:
        gateway.display_name = heartbeat.display_name
        gateway.capabilities = heartbeat.capabilities
        gateway.device_snapshot = heartbeat.device_snapshot
        gateway.agent_version = heartbeat.agent_version
    await db.commit()
    await db.refresh(gateway)
    return gateway


async def create_task(db: AsyncSession, data: DeviceTaskCreate) -> DeviceTask:
    if data.idempotency_key:
        existing = await db.scalar(
            select(DeviceTask).where(
                DeviceTask.project_id == data.project_id,
                DeviceTask.idempotency_key == data.idempotency_key,
            )
        )
        if existing:
            return existing

    task = DeviceTask(
        id=str(uuid.uuid4()),
        project_id=data.project_id,
        target_gateway_id=data.target_gateway_id,
        task_type=data.task_type,
        platform=data.platform,
        surface=data.surface,
        payload=data.payload,
        priority=data.priority,
        max_attempts=data.max_attempts,
        idempotency_key=data.idempotency_key,
        status="pending",
        available_at=utcnow(),
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


async def claim_task(
    db: AsyncSession,
    gateway_id: str,
    capabilities: list[str],
) -> tuple[DeviceTask | None, str | None]:
    now = utcnow()
    await db.execute(
        update(DeviceTask)
        .where(
            DeviceTask.status == "leased",
            DeviceTask.lease_expires_at <= now,
            DeviceTask.attempt_count >= DeviceTask.max_attempts,
        )
        .values(
            status="failed",
            error_code="lease_exhausted",
            error_message="Task lease expired after maximum attempts",
            completed_at=now,
            lease_owner=None,
            lease_token_hash=None,
            lease_expires_at=None,
        )
    )

    claimable = or_(
        DeviceTask.status == "pending",
        and_(
            DeviceTask.status == "leased",
            DeviceTask.lease_expires_at <= now,
            DeviceTask.attempt_count < DeviceTask.max_attempts,
        ),
    )
    conditions = [
        claimable,
        DeviceTask.available_at <= now,
        or_(
            DeviceTask.target_gateway_id.is_(None),
            DeviceTask.target_gateway_id == gateway_id,
        ),
    ]
    if capabilities:
        conditions.append(DeviceTask.task_type.in_(capabilities))

    task = await db.scalar(
        select(DeviceTask)
        .where(*conditions)
        .order_by(DeviceTask.priority.desc(), DeviceTask.created_at.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if task is None:
        await db.commit()
        return None, None

    lease_token = secrets.token_urlsafe(32)
    task.gateway_id = gateway_id
    task.lease_owner = gateway_id
    task.lease_token_hash = _token_hash(lease_token)
    task.lease_expires_at = now + timedelta(
        seconds=settings.device_gateway_lease_seconds
    )
    task.status = "leased"
    task.attempt_count += 1
    task.started_at = task.started_at or now
    await db.commit()
    await db.refresh(task)
    return task, lease_token


async def get_leased_task(
    db: AsyncSession,
    gateway_id: str,
    task_id: str,
    lease_token: str,
) -> DeviceTask:
    task = await db.get(DeviceTask, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    valid_token = (
        task.lease_token_hash
        and secrets.compare_digest(task.lease_token_hash, _token_hash(lease_token))
    )
    if (
        task.status != "leased"
        or task.lease_owner != gateway_id
        or not valid_token
        or not task.lease_expires_at
        or task.lease_expires_at <= utcnow()
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Task lease is invalid or expired",
        )
    return task


async def renew_task(
    db: AsyncSession,
    gateway_id: str,
    task_id: str,
    lease_token: str,
) -> DeviceTask:
    task = await get_leased_task(db, gateway_id, task_id, lease_token)
    task.lease_expires_at = utcnow() + timedelta(
        seconds=settings.device_gateway_lease_seconds
    )
    await db.commit()
    await db.refresh(task)
    return task


async def complete_task(
    db: AsyncSession,
    gateway_id: str,
    task_id: str,
    lease_token: str,
    result: dict,
) -> DeviceTask:
    task = await get_leased_task(db, gateway_id, task_id, lease_token)
    task.status = "completed"
    task.result = result
    task.error_code = None
    task.error_message = None
    task.completed_at = utcnow()
    task.lease_owner = None
    task.lease_token_hash = None
    task.lease_expires_at = None
    await db.commit()
    await db.refresh(task)
    return task


async def fail_task(
    db: AsyncSession,
    gateway_id: str,
    task_id: str,
    lease_token: str,
    error_code: str,
    error_message: str,
    retryable: bool,
    retry_after_seconds: int,
) -> DeviceTask:
    task = await get_leased_task(db, gateway_id, task_id, lease_token)
    task.error_code = error_code
    task.error_message = error_message
    task.lease_owner = None
    task.lease_token_hash = None
    task.lease_expires_at = None
    if retryable and task.attempt_count < task.max_attempts:
        task.status = "pending"
        task.gateway_id = None
        task.available_at = utcnow() + timedelta(seconds=retry_after_seconds)
    else:
        task.status = "failed"
        task.completed_at = utcnow()
    await db.commit()
    await db.refresh(task)
    return task
