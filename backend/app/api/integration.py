"""Integration summary endpoint for GeniLink portal dashboard.

Returns aggregated visibility data for a project.
Accepts GeniLink RS256 JWT via Authorization: Bearer header.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.access import require_project_scope
from app.api.auth import get_current_user
from app.database import get_db
from app.models.models import (
    Audit,
    QueryResult,
    Report,
    Suggestion,
)

router = APIRouter()


@router.get("/summary")
async def integration_summary(
    project_id: str = Query(..., description="Project ID (CUID string)"),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return dashboard summary for a project."""
    require_project_scope(current_user, project_id)

    # Get latest completed audit
    audit_result = await db.execute(
        select(Audit)
        .where(
            Audit.project_id == project_id,
            Audit.status.in_(["completed", "partial"]),
        )
        .order_by(Audit.created_at.desc())
        .limit(1)
    )
    latest_audit = audit_result.scalar_one_or_none()

    # Get latest report for overall score
    overall_score = None
    if latest_audit:
        report_result = await db.execute(
            select(Report)
            .where(Report.audit_id == latest_audit.id)
            .order_by(Report.created_at.desc())
            .limit(1)
        )
        report = report_result.scalar_one_or_none()
        if report and report.overall_score is not None:
            overall_score = report.overall_score

    # Get total mention count
    mention_result = await db.execute(
        select(func.count(QueryResult.id))
        .select_from(QueryResult)
        .join(Audit, QueryResult.audit_id == Audit.id)
        .where(
            Audit.project_id == project_id,
            QueryResult.mention_found == True,  # noqa: E712
        )
    )
    mention_count = mention_result.scalar() or 0

    # Get platform coverage from latest audit
    platform_coverage = []
    if latest_audit:
        from app.models.models import PlatformResponseRecord

        platform_result = await db.execute(
            select(
                PlatformResponseRecord.platform,
                func.count(QueryResult.id).label("total"),
                func.sum(
                    func.cast(QueryResult.mention_found, func.column("INTEGER"))
                ).label("mentions"),
            )
            .select_from(PlatformResponseRecord)
            .join(
                QueryResult,
                QueryResult.response_record_id == PlatformResponseRecord.id,
            )
            .where(PlatformResponseRecord.audit_id == latest_audit.id)
            .group_by(PlatformResponseRecord.platform)
        )
        for row in platform_result:
            total = row.total or 1
            mentions = row.mentions or 0
            score = round((mentions / total) * 100)
            platform_coverage.append({"name": row.platform, "score": score})

    # Get competitor rank from brands snapshot
    competitor_rank = None
    if latest_audit and latest_audit.brands_json:
        # Find own brand (non-competitor) and get its mention position
        own_brands = [b for b in latest_audit.brands_json if not b.get("is_competitor", False)]
        if own_brands:
            own_brand_id = own_brands[0].get("id", "")
            rank_result = await db.execute(
                select(QueryResult)
                .where(
                    QueryResult.audit_id == latest_audit.id,
                    QueryResult.brand_id == own_brand_id,
                    QueryResult.mention_found == True,  # noqa: E712
                )
                .order_by(QueryResult.mention_position.asc())
                .limit(1)
            )
            top_mention = rank_result.scalar_one_or_none()
            if top_mention and top_mention.mention_position:
                competitor_rank = top_mention.mention_position

    # Get suggestions
    suggestions_result = await db.execute(
        select(Suggestion)
        .where(Suggestion.project_id == project_id)
        .order_by(Suggestion.created_at.desc())
        .limit(5)
    )
    suggestions = [
        {"text": s.description, "priority": s.priority}
        for s in suggestions_result.scalars().all()
    ]

    return {
        "overallScore": overall_score,
        "mentionCount": mention_count,
        "platformCoverage": platform_coverage,
        "competitorRank": competitor_rank,
        "suggestions": suggestions,
        "latestAuditDate": latest_audit.created_at.isoformat() if latest_audit else None,
    }
