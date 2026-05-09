"""Audit API endpoints — create, monitor, and retrieve audit results."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.api.projects import get_user_project
from app.api.schemas import AuditCreate, AuditOut, QueryResultOut, ReportOut
from app.database import get_db
from app.models.models import Audit, Brand, Project, Prompt, QueryResult, Report, User
from app.services.audit_service import run_audit
from app.services.report_service import generate_report

router = APIRouter()


@router.post("", response_model=AuditOut)
async def create_audit(
    data: AuditCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new audit and start execution in the background."""
    await get_user_project(data.project_id, current_user, db)

    audit = Audit(
        project_id=data.project_id,
        platforms_json=data.platforms,
    )
    db.add(audit)
    await db.commit()
    await db.refresh(audit)

    asyncio.create_task(run_audit(audit.id))
    return audit


@router.get("/{audit_id}", response_model=AuditOut)
async def get_audit(
    audit_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    audit = await db.get(Audit, audit_id)
    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found")
    # Verify ownership via project
    await get_user_project(audit.project_id, current_user, db)
    return audit


@router.get("/{audit_id}/results", response_model=list[QueryResultOut])
async def get_audit_results(
    audit_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    audit = await db.get(Audit, audit_id)
    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found")
    await get_user_project(audit.project_id, current_user, db)

    result = await db.execute(
        select(QueryResult).where(QueryResult.audit_id == audit_id)
    )
    results = result.scalars().all()

    # Bulk load prompts and brands to avoid N+1
    prompt_ids = {r.prompt_id for r in results}
    brand_ids = {r.brand_id for r in results}

    prompts_result = await db.execute(
        select(Prompt).where(Prompt.id.in_(prompt_ids))
    )
    prompt_map = {p.id: p for p in prompts_result.scalars().all()}

    brands_result = await db.execute(
        select(Brand).where(Brand.id.in_(brand_ids))
    )
    brand_map = {b.id: b for b in brands_result.scalars().all()}

    out = []
    for r in results:
        prompt = prompt_map.get(r.prompt_id)
        brand = brand_map.get(r.brand_id)
        out.append(
            QueryResultOut(
                id=r.id,
                platform=r.platform,
                prompt_text=prompt.text if prompt else None,
                brand_name=brand.name if brand else None,
                mention_found=r.mention_found,
                mention_position=r.mention_position,
                mention_context=r.mention_context,
                mention_confidence=r.mention_confidence,
                is_recommended=r.is_recommended,
                recommendation_rank=r.recommendation_rank,
                error=r.error,
            )
        )
    return out


@router.post("/{audit_id}/report", response_model=ReportOut)
async def create_report(
    audit_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate a report from a completed audit."""
    audit = await db.get(Audit, audit_id)
    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found")
    await get_user_project(audit.project_id, current_user, db)

    if audit.status.value not in ("completed", "partial"):
        raise HTTPException(
            status_code=400,
            detail=f"Audit status is '{audit.status.value}', must be completed or partial",
        )

    # Check if report already exists (idempotent)
    existing = await db.execute(
        select(Report).where(Report.audit_id == audit_id)
    )
    existing_report = existing.scalar_one_or_none()
    if existing_report:
        return existing_report

    report = await generate_report(db, audit)
    return report


@router.get("/{audit_id}/report", response_model=ReportOut)
async def get_report(
    audit_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    audit = await db.get(Audit, audit_id)
    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found")
    await get_user_project(audit.project_id, current_user, db)

    result = await db.execute(
        select(Report).where(Report.audit_id == audit_id)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return report
