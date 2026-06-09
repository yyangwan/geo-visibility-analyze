"""Trend data API — aggregated historical visibility scores."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.access import require_project_scope
from app.api.auth import get_current_user
from app.database import get_db
from app.models.models import Audit, Report

router = APIRouter()


@router.get("/{project_id}")
async def get_trend_data(
    project_id: str,
    period: str = Query("daily", pattern="^(daily|weekly|monthly)$"),
    limit: int = Query(30, ge=1, le=365),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get historical trend data for a project."""
    require_project_scope(current_user, project_id)

    # Get reports with their audits, ordered by date
    result = await db.execute(
        select(Report, Audit)
        .join(Audit, Report.audit_id == Audit.id)
        .where(Report.project_id == project_id)
        .order_by(Report.created_at.desc())
        .limit(limit)
    )
    rows = result.all()

    if not rows:
        return {"project_id": project_id, "data": []}

    # Build trend data points
    data_points = []
    for report, audit in reversed(rows):
        date_key = _truncate_date(report.created_at, period)
        data_points.append({
            "date": date_key,
            "overall_score": report.overall_score,
            "mention_rate": report.mention_rate,
            "competitor_rank": report.competitor_rank,
            "platform_scores": report.platform_scores,
            "audit_id": report.audit_id,
        })

    # Aggregate if period is weekly or monthly
    if period in ("weekly", "monthly"):
        data_points = _aggregate_points(data_points, period)

    return {"project_id": project_id, "data": data_points}


@router.get("/{project_id}/latest-report")
async def get_latest_report(
    project_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the latest report for a project."""
    require_project_scope(current_user, project_id)

    result = await db.execute(
        select(Report)
        .where(Report.project_id == project_id)
        .order_by(Report.created_at.desc())
        .limit(1)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="No reports found")
    return report


@router.get("/{project_id}/audits-history")
async def get_audits_history(
    project_id: str,
    limit: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get audit history for a project with status."""
    require_project_scope(current_user, project_id)

    result = await db.execute(
        select(Audit)
        .where(Audit.project_id == project_id)
        .order_by(Audit.created_at.desc())
        .limit(limit)
    )
    audits = result.scalars().all()
    return [
        {
            "id": a.id,
            "status": a.status.value,
            "platforms": a.platforms_json,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "completed_at": a.completed_at.isoformat() if a.completed_at else None,
            "error_message": a.error_message,
        }
        for a in audits
    ]


def _truncate_date(dt, period: str) -> str:
    """Truncate datetime to the period boundary."""
    if period == "daily":
        return dt.strftime("%Y-%m-%d")
    elif period == "weekly":
        return f"{dt.isocalendar()[0]}-W{dt.isocalendar()[1]:02d}"
    else:
        return dt.strftime("%Y-%m")


def _aggregate_points(points: list[dict], period: str) -> list[dict]:
    """Aggregate multiple data points per period bucket."""
    buckets: dict[str, list[dict]] = {}
    for p in points:
        key = p["date"]
        buckets.setdefault(key, []).append(p)

    result = []
    for key, group in buckets.items():
        avg_score = sum(g["overall_score"] for g in group) / len(group)
        avg_mention = sum(g["mention_rate"] for g in group) / len(group)

        all_platforms: dict[str, list[float]] = {}
        for g in group:
            for plat, score in g.get("platform_scores", {}).items():
                all_platforms.setdefault(plat, []).append(score)

        platform_avg = {
            plat: sum(scores) / len(scores)
            for plat, scores in all_platforms.items()
        }

        result.append({
            "date": key,
            "overall_score": round(avg_score, 1),
            "mention_rate": round(avg_mention, 3),
            "platform_scores": platform_avg,
            "audit_id": group[-1]["audit_id"],
        })

    return result
