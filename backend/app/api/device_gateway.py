import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.access import require_project_scope
from app.api.auth import get_current_user
from app.api.device_gateway_schemas import (
    DeviceTaskCreate,
    DeviceTaskOut,
    GatewayClaimedTask,
    GatewayClaimRequest,
    GatewayCompleteRequest,
    GatewayFailRequest,
    GatewayHeartbeatRequest,
    GatewayHeartbeatResponse,
    GatewayLeaseRequest,
)
from app.config import settings
from app.database import get_db
from app.models.device_gateway import DeviceTask
from app.services.device_gateway_service import (
    claim_task,
    complete_task,
    create_task,
    fail_task,
    renew_task,
    touch_gateway,
    utcnow,
)

task_router = APIRouter()
gateway_router = APIRouter()
gateway_bearer = HTTPBearer(auto_error=False)


async def authenticate_gateway(
    authorization: HTTPAuthorizationCredentials | None = Security(gateway_bearer),
    gateway_id: str | None = Header(default=None, alias="X-Gateway-Id"),
) -> str:
    configured_token = settings.device_gateway_token
    if not configured_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Device gateway authentication is not configured",
        )
    if (
        authorization is None
        or authorization.scheme.lower() != "bearer"
        or not secrets.compare_digest(authorization.credentials, configured_token)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid gateway credentials",
        )
    if not gateway_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Gateway-Id is required",
        )
    allowed_ids = {
        item.strip()
        for item in settings.device_gateway_ids.split(",")
        if item.strip()
    }
    if allowed_ids and gateway_id not in allowed_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Gateway is not allowed",
        )
    return gateway_id


@task_router.post("", response_model=DeviceTaskOut, status_code=status.HTTP_201_CREATED)
async def enqueue_device_task(
    data: DeviceTaskCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_project_scope(current_user, data.project_id)
    return await create_task(db, data)


@task_router.get("", response_model=list[DeviceTaskOut])
async def list_device_tasks(
    project_id: str = Query(min_length=1, max_length=50),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_project_scope(current_user, project_id)
    result = await db.execute(
        select(DeviceTask)
        .where(DeviceTask.project_id == project_id)
        .order_by(DeviceTask.created_at.desc())
        .limit(limit)
    )
    return result.scalars().all()


@task_router.get("/{task_id}", response_model=DeviceTaskOut)
async def get_device_task(
    task_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await db.get(DeviceTask, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    require_project_scope(current_user, task.project_id)
    return task


@gateway_router.post("/heartbeat", response_model=GatewayHeartbeatResponse)
async def gateway_heartbeat(
    data: GatewayHeartbeatRequest,
    gateway_id: str = Depends(authenticate_gateway),
    db: AsyncSession = Depends(get_db),
):
    await touch_gateway(db, gateway_id, data)
    return GatewayHeartbeatResponse(
        gateway_id=gateway_id,
        server_time=utcnow(),
        lease_seconds=settings.device_gateway_lease_seconds,
    )


@gateway_router.post("/tasks/claim", response_model=GatewayClaimedTask)
async def gateway_claim_task(
    data: GatewayClaimRequest,
    gateway_id: str = Depends(authenticate_gateway),
    db: AsyncSession = Depends(get_db),
):
    await touch_gateway(db, gateway_id)
    task, lease_token = await claim_task(db, gateway_id, data.capabilities)
    return GatewayClaimedTask(task=task, lease_token=lease_token)


@gateway_router.post("/tasks/{task_id}/heartbeat", response_model=DeviceTaskOut)
async def gateway_task_heartbeat(
    task_id: str,
    data: GatewayLeaseRequest,
    gateway_id: str = Depends(authenticate_gateway),
    db: AsyncSession = Depends(get_db),
):
    return await renew_task(db, gateway_id, task_id, data.lease_token)


@gateway_router.post("/tasks/{task_id}/complete", response_model=DeviceTaskOut)
async def gateway_complete_task(
    task_id: str,
    data: GatewayCompleteRequest,
    gateway_id: str = Depends(authenticate_gateway),
    db: AsyncSession = Depends(get_db),
):
    return await complete_task(
        db,
        gateway_id,
        task_id,
        data.lease_token,
        data.result,
    )


@gateway_router.post("/tasks/{task_id}/fail", response_model=DeviceTaskOut)
async def gateway_fail_task(
    task_id: str,
    data: GatewayFailRequest,
    gateway_id: str = Depends(authenticate_gateway),
    db: AsyncSession = Depends(get_db),
):
    return await fail_task(
        db,
        gateway_id,
        task_id,
        data.lease_token,
        data.error_code,
        data.error_message,
        data.retryable,
        data.retry_after_seconds,
    )
