"""Scheduled job API endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.access import get_schedule_for_project, require_project_scope, require_workspace_scope
from app.api.auth import get_current_user
from app.api.schemas import ScheduledJobCreate, ScheduledJobOut
from app.database import get_db
from app.models.models import ScheduledJob

router = APIRouter()


@router.post("", response_model=ScheduledJobOut)
async def create_schedule(
    data: ScheduledJobCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new scheduled audit job."""
    require_project_scope(current_user, data.project_id)

    # Validate cron expression format
    parts = data.cron_expression.strip().split()
    if len(parts) != 5:
        raise HTTPException(
            status_code=400,
            detail="Invalid cron expression. Format: minute hour day month weekday",
        )

    job = ScheduledJob(
        project_id=data.project_id,
        cron_expression=data.cron_expression.strip(),
        platforms_json=data.platforms,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


@router.get("", response_model=list[ScheduledJobOut])
async def list_schedules(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all scheduled jobs for the current user's projects."""
    require_workspace_scope(current_user)
    result = await db.execute(
        select(ScheduledJob)
        .order_by(ScheduledJob.created_at.desc())
    )
    return result.scalars().all()


@router.get("/{job_id}", response_model=ScheduledJobOut)
async def get_schedule(
    job_id: int,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    job = await get_schedule_for_project(db, current_user, job_id)
    return job


@router.patch("/{job_id}/toggle", response_model=ScheduledJobOut)
async def toggle_schedule(
    job_id: int,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Toggle a scheduled job active/inactive."""
    job = await get_schedule_for_project(db, current_user, job_id)

    job.is_active = not job.is_active
    await db.commit()
    await db.refresh(job)
    return job


@router.delete("/{job_id}", status_code=204)
async def delete_schedule(
    job_id: int,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    job = await get_schedule_for_project(db, current_user, job_id)

    await db.delete(job)
    await db.commit()
