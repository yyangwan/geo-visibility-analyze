"""PDF report export endpoint."""

import io
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.api.projects import get_user_project
from app.constants import PLATFORM_LABELS
from app.database import get_db
from app.models.models import Brand, Project, QueryResult, Report, User
from jinja2 import Environment, FileSystemLoader, select_autoescape

router = APIRouter()

_env = Environment(
    loader=FileSystemLoader("app/templates"),
    autoescape=select_autoescape(["html"]),
)


@router.get("/{report_id}/pdf")
async def export_pdf(
    report_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Export a report as PDF."""
    report = await db.get(Report, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")

    await get_user_project(report.project_id, current_user, db)
    project = await db.get(Project, report.project_id)

    # Load brands
    brands_result = await db.execute(
        select(Brand).where(Brand.project_id == report.project_id)
    )
    brands = brands_result.scalars().all()
    brand_map = {b.id: b.name for b in brands}

    # Load query results for this audit
    qr_result = await db.execute(
        select(QueryResult).where(QueryResult.audit_id == report.audit_id)
    )
    query_results = qr_result.scalars().all()

    # Prepare platform scores with labels
    platform_data = []
    for key, score in report.platform_scores.items():
        platform_data.append({
            "name": PLATFORM_LABELS.get(key, key),
            "key": key,
            "score": score,
            "status": "优秀" if score >= 70 else "中等" if score >= 40 else "需改进",
        })
    platform_data.sort(key=lambda x: x["score"], reverse=True)

    # Compute per-platform mention counts
    platform_mentions = {}
    for qr in query_results:
        if qr.platform not in platform_mentions:
            platform_mentions[qr.platform] = {"total": 0, "mentions": 0}
        platform_mentions[qr.platform]["total"] += 1
        if qr.mention_found:
            platform_mentions[qr.platform]["mentions"] += 1

    for pd_item in platform_data:
        k = pd_item["key"]
        if k in platform_mentions:
            pm = platform_mentions[k]
            pd_item["mention_rate"] = f"{pm['mentions']}/{pm['total']}"
        else:
            pd_item["mention_rate"] = "0/0"

    # Brand comparison data (using brand_map dict instead of N+1)
    brand_stats = {}
    for qr in query_results:
        bname = brand_map.get(qr.brand_id)
        if not bname:
            continue
        if bname not in brand_stats:
            brand_stats[bname] = {"total": 0, "mentions": 0, "recommended": 0}
        brand_stats[bname]["total"] += 1
        if qr.mention_found:
            brand_stats[bname]["mentions"] += 1
        if qr.is_recommended:
            brand_stats[bname]["recommended"] += 1

    brand_comparison = []
    for name, stats in brand_stats.items():
        rate = (stats["mentions"] / stats["total"] * 100) if stats["total"] > 0 else 0
        brand_comparison.append({
            "name": name,
            "mentions": stats["mentions"],
            "recommended": stats["recommended"],
            "rate": f"{rate:.1f}%",
        })
    brand_comparison.sort(key=lambda x: float(x["rate"].rstrip("%")), reverse=True)

    # Render HTML
    template = _env.get_template("report.html")
    html = template.render(
        project_name=project.name if project else "Unknown",
        report_date=datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
        overall_score=report.overall_score,
        mention_rate=f"{report.mention_rate:.1%}",
        competitor_rank=f"第{report.competitor_rank}名" if report.competitor_rank else "-",
        platform_data=platform_data,
        brand_comparison=brand_comparison,
        insights=report.insights or [],
    )

    # Convert to PDF
    try:
        from weasyprint import HTML
        pdf_bytes = HTML(string=html).write_pdf()
    except Exception:
        return StreamingResponse(
            io.BytesIO(html.encode("utf-8")),
            media_type="text/html",
            headers={"Content-Disposition": f"attachment; filename=report-{report_id}.html"},
        )

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=report-{report_id}.pdf"},
    )
