"""Suggestions API — generate, list, and manage optimization suggestions."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.access import get_suggestion_for_project, require_project_scope
from app.api.auth import get_current_user
from app.database import get_db
from app.models.models import Report, Suggestion
from app.api.schemas import SuggestionOut
from app.services.suggestion_service import generate_suggestions

router = APIRouter()


@router.get("/{project_id}", response_model=list[SuggestionOut])
async def list_suggestions(
    project_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all suggestions for a project, newest first."""
    require_project_scope(current_user, project_id)

    result = await db.execute(
        select(Suggestion)
        .where(Suggestion.project_id == project_id)
        .order_by(Suggestion.created_at.desc())
    )
    return result.scalars().all()


@router.post("/{project_id}/generate", response_model=list[SuggestionOut])
async def generate(
    project_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate AI suggestions based on the latest report."""
    require_project_scope(current_user, project_id)

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
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark a suggestion as resolved."""
    s = await get_suggestion_for_project(db, current_user, suggestion_id)

    s.is_resolved = True
    await db.commit()
    await db.refresh(s)
    return s


@router.delete("/{suggestion_id}")
async def delete_suggestion(
    suggestion_id: int,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a suggestion."""
    s = await get_suggestion_for_project(db, current_user, suggestion_id)

    await db.delete(s)
    await db.commit()
    return {"ok": True}
