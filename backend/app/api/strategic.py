"""Strategic Intelligence API — cross-audit trend analysis endpoints."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.access import require_project_scope
from app.api.auth import get_current_user
from app.api.schemas import (
    AnswerStructureEvolutionOut,
    CompetitorPositioningOut,
    MultiAuditComparisonOut,
    MultiAuditComparisonRequest,
    SourceAuthorityTrendsOut,
)
from app.database import get_db
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
    project_id: str,
    limit: int = Query(10, ge=2, le=50),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Track which sources/domains AI platforms cite over time."""
    require_project_scope(current_user, project_id)
    return await get_source_authority_trends(db, project_id, limit)


@router.get(
    "/projects/{project_id}/competitor-positioning",
    response_model=CompetitorPositioningOut,
)
async def competitor_positioning(
    project_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Brand positioning across mention frequency, sentiment, and authority."""
    require_project_scope(current_user, project_id)
    return await get_competitor_positioning_map(db, project_id)


@router.get(
    "/projects/{project_id}/structure-evolution",
    response_model=AnswerStructureEvolutionOut,
)
async def structure_evolution(
    project_id: str,
    limit: int = Query(10, ge=2, le=50),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Track how AI platforms structure their answers over time."""
    require_project_scope(current_user, project_id)
    return await get_answer_structure_evolution(db, project_id, limit)


@router.post(
    "/projects/{project_id}/compare-audits",
    response_model=MultiAuditComparisonOut,
)
async def compare_audits(
    project_id: str,
    body: MultiAuditComparisonRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Compare multiple audits side-by-side with diffs."""
    require_project_scope(current_user, project_id)
    return await get_multi_audit_comparison(db, project_id, body.audit_ids)
