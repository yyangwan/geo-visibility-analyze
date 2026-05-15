"""Integration summary endpoint for GeniLink portal dashboard.

Returns aggregated visibility data for a user's projects.
Accepts GeniLink RS256 JWT via Authorization: Bearer header.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.database import get_db
from app.models.models import (
    Audit,
    Brand,
    Project,
    QueryResult,
    Report,
    Suggestion,
    User,
)

router = APIRouter()


@router.get("/summary")
async def integration_summary(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return dashboard summary for the user's most recently audited project."""

    # Get user's projects (pick the one with the most recent audit)
    latest_audit_subq = (
        select(Audit.project_id, func.max(Audit.created_at).label("last_audit"))
        .group_by(Audit.project_id)
        .subquery()
    )

    project_result = await db.execute(
        select(Project)
        .where(Project.user_id == current_user.id)
        .order_by(Project.created_at.desc())
    )
    projects = project_result.scalars().all()

    if not projects:
        return {
            "overallScore": None,
            "mentionCount": 0,
            "platformCoverage": [],
            "competitorRank": None,
            "suggestions": [],
            "latestAuditDate": None,
        }

    # Get latest completed audit across all projects
    project_ids = [p.id for p in projects]
    audit_result = await db.execute(
        select(Audit)
        .where(
            Audit.project_id.in_(project_ids),
            Audit.status.in_(["completed", "partial"]),
        )
        .order_by(Audit.created_at.desc())
        .limit(1)
    )
    latest_audit = audit_result.scalar_one_or_none()

    # Get latest report for overall score
    overall_score = None
    competitor_rank = None
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
            Audit.project_id.in_(project_ids),
            QueryResult.mention_found == True,  # noqa: E712
        )
    )
    mention_count = mention_result.scalar() or 0

    # Get platform coverage from latest audit
    platform_coverage = []
    if latest_audit:
        # Get per-platform mention rate
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

    # Get competitor rank from brands
    brands_result = await db.execute(
        select(Brand)
        .where(
            Brand.project_id.in_(project_ids),
            Brand.is_competitor == False,  # noqa: E712
        )
        .limit(1)
    )
    client_brand = brands_result.scalar_one_or_none()

    if client_brand and latest_audit:
        # Simple rank calculation based on mention position
        rank_result = await db.execute(
            select(QueryResult)
            .where(
                QueryResult.audit_id == latest_audit.id,
                QueryResult.brand_id == client_brand.id,
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
        .where(Suggestion.project_id.in_(project_ids))
        .order_by(Suggestion.created_at.desc())
        .limit(5)
    )
    suggestions = [
        {"text": s.text, "priority": s.priority}
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
