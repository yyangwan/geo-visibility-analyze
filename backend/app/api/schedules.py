"""Scheduled job API endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.api.projects import get_user_project
from app.api.schemas import ScheduledJobCreate, ScheduledJobOut
from app.database import get_db
from app.models.models import Project, ScheduledJob, User

router = APIRouter()


@router.post("", response_model=ScheduledJobOut)
async def create_schedule(
    data: ScheduledJobCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new scheduled audit job."""
    await get_user_project(data.project_id, current_user, db)

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
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all scheduled jobs for the current user's projects."""
    # Get user's project IDs
    result = await db.execute(
        select(Project).where(Project.user_id == current_user.id)
    )
    project_ids = [p.id for p in result.scalars().all()]

    if not project_ids:
        return []

    result = await db.execute(
        select(ScheduledJob)
        .where(ScheduledJob.project_id.in_(project_ids))
        .order_by(ScheduledJob.created_at.desc())
    )
    return result.scalars().all()


@router.get("/{job_id}", response_model=ScheduledJobOut)
async def get_schedule(
    job_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    job = await db.get(ScheduledJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Scheduled job not found")
    await get_user_project(job.project_id, current_user, db)
    return job


@router.patch("/{job_id}/toggle", response_model=ScheduledJobOut)
async def toggle_schedule(
    job_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Toggle a scheduled job active/inactive."""
    job = await db.get(ScheduledJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Scheduled job not found")
    await get_user_project(job.project_id, current_user, db)
    job.is_active = not job.is_active
    await db.commit()
    await db.refresh(job)
    return job


@router.delete("/{job_id}", status_code=204)
async def delete_schedule(
    job_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    job = await db.get(ScheduledJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Scheduled job not found")
    await get_user_project(job.project_id, current_user, db)
    await db.delete(job)
    await db.commit()
