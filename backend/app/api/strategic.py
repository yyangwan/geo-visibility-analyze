"""Strategic Intelligence API — cross-audit trend analysis endpoints."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.api.projects import get_user_project
from app.api.schemas import (
    AnswerStructureEvolutionOut,
    CompetitorPositioningOut,
    MultiAuditComparisonOut,
    MultiAuditComparisonRequest,
    SourceAuthorityTrendsOut,
)
from app.database import get_db
from app.models.models import User
from app.services.strategic_intelligence_service import (
    get_answer_structure_evolution,
    get_competitor_positioning_map,
    get_multi_audit_comparison,
    get_source_authority_trends,
)

router = APIRouter()


@router.get(
    "/projects/{project_id}/source-authority-trends",
    response_model=SourceAuthorityTrendsOut,
)
async def source_authority_trends(
    project_id: int,
    limit: int = Query(10, ge=2, le=50),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Track which sources/domains AI platforms cite over time."""
    await get_user_project(project_id, current_user, db)
    return await get_source_authority_trends(db, project_id, limit)


@router.get(
    "/projects/{project_id}/competitor-positioning",
    response_model=CompetitorPositioningOut,
)
async def competitor_positioning(
    project_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Brand positioning across mention frequency, sentiment, and authority."""
    await get_user_project(project_id, current_user, db)
    return await get_competitor_positioning_map(db, project_id)


@router.get(
    "/projects/{project_id}/structure-evolution",
    response_model=AnswerStructureEvolutionOut,
)
async def structure_evolution(
    project_id: int,
    limit: int = Query(10, ge=2, le=50),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Track how AI platforms structure their answers over time."""
    await get_user_project(project_id, current_user, db)
    return await get_answer_structure_evolution(db, project_id, limit)


@router.post(
    "/projects/{project_id}/compare-audits",
    response_model=MultiAuditComparisonOut,
)
async def compare_audits(
    project_id: int,
    body: MultiAuditComparisonRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Compare multiple audits side-by-side with diffs."""
    await get_user_project(project_id, current_user, db)
    return await get_multi_audit_comparison(db, project_id, body.audit_ids)
