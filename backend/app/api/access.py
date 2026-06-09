"""JWT scope helpers for project- and workspace-scoped routes."""

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Audit, Report, ScheduledJob, Suggestion


def require_workspace_scope(current_user: dict) -> None:
    """Ensure the request was signed with a workspace-scoped Portal token."""
    if current_user.get("scope") != "workspace":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Workspace scope required",
        )


def require_project_scope(current_user: dict, project_id: str) -> None:
    """Ensure the request was signed with a project-scoped Portal token."""
    if current_user.get("scope") != "project":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Project scope required",
        )
    if current_user.get("pid") != project_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Project scope required",
        )


async def get_audit_for_project(
    db: AsyncSession,
    current_user: dict,
    audit_id: int,
) -> Audit:
    audit = await db.get(Audit, audit_id)
    if not audit:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audit not found")
    require_project_scope(current_user, audit.project_id)
    return audit


async def get_report_for_project(
    db: AsyncSession,
    current_user: dict,
    report_id: int,
) -> Report:
    report = await db.get(Report, report_id)
    if not report:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    require_project_scope(current_user, report.project_id)
    return report


async def get_suggestion_for_project(
    db: AsyncSession,
    current_user: dict,
    suggestion_id: int,
) -> Suggestion:
    suggestion = await db.get(Suggestion, suggestion_id)
    if not suggestion:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Suggestion not found")
    require_project_scope(current_user, suggestion.project_id)
    return suggestion


async def get_schedule_for_project(
    db: AsyncSession,
    current_user: dict,
    job_id: int,
) -> ScheduledJob:
    job = await db.get(ScheduledJob, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scheduled job not found")
    require_project_scope(current_user, job.project_id)
    return job
