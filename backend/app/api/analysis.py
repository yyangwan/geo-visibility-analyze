"""Content Intelligence API — analysis trigger, listing, and aggregated dashboard data."""

import asyncio
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.access import get_audit_for_project, require_project_scope
from app.api.auth import get_current_user
from app.api.schemas import ContentIntelligenceOut, ResponseAnalysisOut
from app.database import get_db
from app.models.models import (
    Audit,
    PlatformResponseRecord,
    Prompt,
    ResponseAnalysis,
)
from app.services.response_analysis_service import retry_failed_analyses, run_analysis_for_audit

router = APIRouter()


@router.get("/audits/{audit_id}/analysis", response_model=list[ResponseAnalysisOut])
async def get_audit_analysis(
    audit_id: int,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all ResponseAnalysis records for an audit."""
    await get_audit_for_project(db, current_user, audit_id)

    result = await db.execute(
        select(ResponseAnalysis)
        .join(PlatformResponseRecord, ResponseAnalysis.response_record_id == PlatformResponseRecord.id)
        .where(PlatformResponseRecord.audit_id == audit_id)
    )
    analyses = result.scalars().all()

    # Bulk load PRRs and prompts for joined info
    prr_ids = {a.response_record_id for a in analyses}
    prr_result = await db.execute(
        select(PlatformResponseRecord).where(PlatformResponseRecord.id.in_(prr_ids))
    )
    prr_map = {p.id: p for p in prr_result.scalars().all()}

    # Load prompts for historical context (include deleted)
    prompt_ids = {prr.prompt_id for prr in prr_map.values()}
    prompt_result = await db.execute(
        select(Prompt).where(Prompt.id.in_(prompt_ids))
    )
    prompt_map = {p.id: p for p in prompt_result.scalars().all()}

    out = []
    for a in analyses:
        prr = prr_map.get(a.response_record_id)
        prompt = prompt_map.get(prr.prompt_id) if prr else None
        out.append(
            ResponseAnalysisOut(
                id=a.id,
                response_record_id=a.response_record_id,
                platform=prr.platform if prr else None,
                prompt_text=prompt.text if prompt else None,
                cited_sources=a.cited_sources or [],
                brand_sentiment=a.brand_sentiment,
                brand_attributes=a.brand_attributes or [],
                topics_covered=a.topics_covered or [],
                answer_structure=a.answer_structure,
                competitor_refs=a.competitor_refs or [],
                analysis_model=a.analysis_model or "",
                status=a.status,
                created_at=a.created_at,
            )
        )
    return out


@router.post("/audits/{audit_id}/analyze")
async def trigger_analysis(
    audit_id: int,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Manually trigger analysis for all PRRs in an audit."""
    audit = await get_audit_for_project(db, current_user, audit_id)

    if audit.status.value not in ("completed", "partial"):
        raise HTTPException(status_code=400, detail="Audit must be completed or partial before analysis")

    asyncio.create_task(run_analysis_for_audit(audit_id))
    return {"message": "Analysis started", "audit_id": audit_id}


@router.post("/audits/{audit_id}/analyze/retry")
async def retry_analysis(
    audit_id: int,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Retry all failed analyses for an audit."""
    await get_audit_for_project(db, current_user, audit_id)

    count = await retry_failed_analyses(audit_id)
    return {"message": f"Retrying {count} failed analyses", "count": count}


@router.get("/projects/{project_id}/content-intelligence", response_model=ContentIntelligenceOut)
async def get_content_intelligence(
    project_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get aggregated content intelligence data for a project's latest audit."""
    require_project_scope(current_user, project_id)

    # Find latest completed/partial audit
    audit_result = await db.execute(
        select(Audit)
        .where(Audit.project_id == project_id)
        .where(Audit.status.in_(["completed", "partial"]))
        .order_by(Audit.created_at.desc())
        .limit(1)
    )
    audit = audit_result.scalar_one_or_none()
    if not audit:
        return ContentIntelligenceOut()

    # Load all PRRs for this audit
    prr_result = await db.execute(
        select(PlatformResponseRecord)
        .where(PlatformResponseRecord.audit_id == audit.id)
    )
    prrs = prr_result.scalars().all()

    # Load all analyses for these PRRs
    analyses_result = await db.execute(
        select(ResponseAnalysis)
        .join(PlatformResponseRecord, ResponseAnalysis.response_record_id == PlatformResponseRecord.id)
        .where(PlatformResponseRecord.audit_id == audit.id)
    )
    analyses = analyses_result.scalars().all()

    # Aggregate server-side
    topic_counter: Counter = Counter()
    sentiment_counter: Counter = Counter()
    structure_counter: Counter = Counter()
    source_agg: dict[str, dict] = {}  # domain -> {count, authority_sum}
    heatmap: dict[str, dict[str, str]] = {}  # platform -> {topic: sentiment}
    status_counter: Counter = Counter()
    total_prompt_tokens = 0
    total_completion_tokens = 0

    for prr in prrs:
        total_prompt_tokens += prr.prompt_tokens or 0
        total_completion_tokens += prr.completion_tokens or 0

    for a in analyses:
        status_counter[a.status] += 1

        if a.status != "completed":
            continue

        # Sentiment
        if a.brand_sentiment:
            sentiment_counter[a.brand_sentiment] += 1

        # Topics
        for topic in (a.topics_covered or []):
            topic_counter[topic] += 1

        # Answer structure
        if a.answer_structure:
            structure_counter[a.answer_structure] += 1

        # Cited sources
        for source in (a.cited_sources or []):
            domain = source.get("domain", "")
            if not domain:
                continue
            if domain not in source_agg:
                source_agg[domain] = {"count": 0, "authority_sum": 0}
            source_agg[domain]["count"] += 1
            source_agg[domain]["authority_sum"] += source.get("authority_score", 3)

        # Heatmap: platform -> topic -> sentiment
        prr = next((p for p in prrs if p.id == a.response_record_id), None)
        if prr and a.brand_sentiment:
            platform = prr.platform
            for topic in (a.topics_covered or []):
                if platform not in heatmap:
                    heatmap[platform] = {}
                heatmap[platform][topic] = a.brand_sentiment

    # Build top cited sources
    top_sources = sorted(
        [
            {
                "domain": domain,
                "total_count": data["count"],
                "authority_avg": round(data["authority_sum"] / data["count"], 1),
            }
            for domain, data in source_agg.items()
        ],
        key=lambda x: x["total_count"],
        reverse=True,
    )[:20]

    return ContentIntelligenceOut(
        topic_distribution=dict(topic_counter.most_common(20)),
        sentiment_breakdown=dict(sentiment_counter),
        answer_structure_distribution=dict(structure_counter),
        top_cited_sources=top_sources,
        brand_positioning_heatmap=heatmap,
        token_cost_summary={
            "total_prompt_tokens": total_prompt_tokens,
            "total_completion_tokens": total_completion_tokens,
        },
        analysis_status=dict(status_counter),
        total_responses=len(prrs),
        analyzed_responses=sum(1 for a in analyses if a.status == "completed"),
    )
