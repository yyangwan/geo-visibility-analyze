"""Suggestions API — generate, list, and manage optimization suggestions."""

from fastapi import APIRouter, Depends, HTTPException, Query
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
    audit_id: int | None = Query(None),
    report_id: int | None = Query(None),
    latest: bool = Query(True, description="Default to suggestions for the latest report."),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List suggestions for a project, scoped to latest report by default."""
    require_project_scope(current_user, project_id)

    target_report_id = await _resolve_report_scope(
        db,
        project_id,
        audit_id=audit_id,
        report_id=report_id,
        latest=latest,
    )

    query = (
        select(Suggestion)
        .where(Suggestion.project_id == project_id)
        .order_by(Suggestion.created_at.desc())
    )
    if target_report_id is not None:
        query = query.where(Suggestion.report_id == target_report_id)

    result = await db.execute(query)
    suggestions = result.scalars().all()
    return await _serialize_suggestions(db, suggestions)


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
    return await _serialize_suggestions(db, suggestions)


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


async def _resolve_report_scope(
    db: AsyncSession,
    project_id: str,
    *,
    audit_id: int | None,
    report_id: int | None,
    latest: bool,
) -> int | None:
    """Resolve which report suggestions should be shown for."""
    if report_id is not None:
        report = await db.get(Report, report_id)
        if not report or report.project_id != project_id:
            raise HTTPException(status_code=404, detail="Report not found")
        return report.id

    if audit_id is not None:
        result = await db.execute(
            select(Report)
            .where(Report.project_id == project_id, Report.audit_id == audit_id)
            .order_by(Report.created_at.desc())
            .limit(1)
        )
        report = result.scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Report not found for audit")
        return report.id

    if latest:
        result = await db.execute(
            select(Report)
            .where(Report.project_id == project_id)
            .order_by(Report.created_at.desc())
            .limit(1)
        )
        report = result.scalar_one_or_none()
        return report.id if report else None

    return None


async def _serialize_suggestions(
    db: AsyncSession,
    suggestions: list[Suggestion],
) -> list[dict]:
    """Attach audit_id from Report so clients can show suggestion provenance."""
    if not suggestions:
        return []

    report_ids = {s.report_id for s in suggestions}
    result = await db.execute(select(Report).where(Report.id.in_(report_ids)))
    reports = {r.id: r for r in result.scalars().all()}

    serialized = []
    for suggestion in suggestions:
        report = reports.get(suggestion.report_id)
        serialized.append(
            {
                "id": suggestion.id,
                "project_id": suggestion.project_id,
                "report_id": suggestion.report_id,
                "audit_id": report.audit_id if report else None,
                "category": suggestion.category,
                "title": suggestion.title,
                "description": suggestion.description,
                "priority": suggestion.priority,
                "is_resolved": suggestion.is_resolved,
                "detail": suggestion.detail,
                "created_at": suggestion.created_at,
            }
        )
    return serialized
