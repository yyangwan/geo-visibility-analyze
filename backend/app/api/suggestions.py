"""Suggestions API — generate, list, and manage optimization suggestions."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.api.projects import get_user_project
from app.database import get_db
from app.models.models import Project, Report, Suggestion, User
from app.api.schemas import SuggestionOut
from app.services.suggestion_service import generate_suggestions

router = APIRouter()


@router.get("/{project_id}", response_model=list[SuggestionOut])
async def list_suggestions(
    project_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all suggestions for a project, newest first."""
    await get_user_project(project_id, current_user, db)
    result = await db.execute(
        select(Suggestion)
        .where(Suggestion.project_id == project_id)
        .order_by(Suggestion.created_at.desc())
    )
    return result.scalars().all()


@router.post("/{project_id}/generate", response_model=list[SuggestionOut])
async def generate(
    project_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate AI suggestions based on the latest report."""
    await get_user_project(project_id, current_user, db)

    result = await db.execute(
        select(Report)
        .where(Report.project_id == project_id)
        .order_by(Report.created_at.desc())
        .limit(1)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="No report found for this project")

    suggestions = await generate_suggestions(db, report)
    return suggestions


@router.patch("/{suggestion_id}/resolve", response_model=SuggestionOut)
async def resolve_suggestion(
    suggestion_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark a suggestion as resolved."""
    s = await db.get(Suggestion, suggestion_id)
    if not s:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    # Verify ownership via project
    await get_user_project(s.project_id, current_user, db)
    s.is_resolved = True
    await db.commit()
    await db.refresh(s)
    return s


@router.delete("/{suggestion_id}")
async def delete_suggestion(
    suggestion_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a suggestion."""
    s = await db.get(Suggestion, suggestion_id)
    if not s:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    await get_user_project(s.project_id, current_user, db)
    await db.delete(s)
    await db.commit()
    return {"ok": True}
