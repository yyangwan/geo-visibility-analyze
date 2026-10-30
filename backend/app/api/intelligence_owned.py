"""Project-scoped material management; reads never fetch remote sources."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.access import require_project_scope
from app.api.auth import get_current_user
from app.database import get_db
from app.intelligence.models import (
    BrandFact,
    ClaimEvidence,
    ClaimReview,
    IntelligenceRun,
    OwnedContentAsset,
)
from app.intelligence.owned_assets import (
    RegisterAsset,
    RetryAsset,
    VerifyAsset,
    asset_view,
    get_asset,
    list_assets,
    read_text,
    register_asset,
    retry_asset,
    verify_asset,
)
from app.intelligence.owned_facts import (
    ApproveOwnedFact,
    approve_owned_fact,
    owned_fact_fresh,
)
from app.intelligence.reviews import citation_fact_source, fact_view

router = APIRouter()


def authorize(user, project_id, *, write=False):
    require_project_scope(user, project_id)
    if write:
        from app.api.intelligence import action_writer

        action_writer(user)


@router.get("/projects/{project_id}/owned-assets")
async def assets(
    project_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    authorize(user, project_id)
    return await list_assets(db, project_id, offset=offset, limit=limit)


@router.post("/projects/{project_id}/owned-assets", status_code=202)
async def register(
    project_id: str,
    data: RegisterAsset,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    authorize(user, project_id, write=True)
    asset = await register_asset(db, project_id, data, str(user["sub"]))
    await db.commit()
    return await asset_view(db, asset)


@router.get("/projects/{project_id}/owned-assets/{asset_id}")
async def detail(
    project_id: str,
    asset_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    authorize(user, project_id)
    return await asset_view(db, await get_asset(db, project_id, asset_id))


@router.get("/projects/{project_id}/owned-assets/{asset_id}/text")
async def text(
    project_id: str,
    asset_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    authorize(user, project_id)
    asset = await get_asset(db, project_id, asset_id)
    return {"asset": await asset_view(db, asset), "text": await read_text(asset)}


@router.get("/projects/{project_id}/owned-assets/{asset_id}/facts")
async def material_facts(
    project_id: str,
    asset_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    authorize(user, project_id)
    asset = await get_asset(db, project_id, asset_id)
    revisions = select(OwnedContentAsset.id).where(
        OwnedContentAsset.project_id == project_id,
        OwnedContentAsset.asset_key == asset.asset_key,
    )
    rows = (
        await db.scalars(
            select(BrandFact)
            .where(
                BrandFact.project_id == project_id,
                BrandFact.source_kind == "owned_asset_revision",
                BrandFact.owned_asset_revision_id.in_(revisions),
            )
            .order_by(BrandFact.created_at, BrandFact.version)
        )
    ).all()
    return {
        "asset_key": asset.asset_key,
        "facts": [await fact_view(db, row) for row in rows],
    }


@router.post("/projects/{project_id}/owned-assets/{asset_id}/verify", status_code=202)
async def verify(
    project_id: str,
    asset_id: str,
    data: VerifyAsset,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    authorize(user, project_id, write=True)
    asset = await verify_asset(
        db, await get_asset(db, project_id, asset_id), data, str(user["sub"])
    )
    await db.commit()
    return await asset_view(db, asset)


@router.post("/projects/{project_id}/owned-assets/{asset_id}/retry", status_code=202)
async def retry(
    project_id: str,
    asset_id: str,
    data: RetryAsset,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    authorize(user, project_id, write=True)
    job = await retry_asset(
        db, await get_asset(db, project_id, asset_id), data, str(user["sub"])
    )
    await db.commit()
    return {"job_id": job.id, "status": job.status}


@router.post("/projects/{project_id}/owned-assets/{asset_id}/facts", status_code=202)
async def approve(
    project_id: str,
    asset_id: str,
    data: ApproveOwnedFact,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    authorize(user, project_id, write=True)
    fact = await approve_owned_fact(
        db, await get_asset(db, project_id, asset_id), data, str(user["sub"])
    )
    await db.commit()
    return await fact_view(db, fact)


@router.get("/facts/{fact_id}/source")
async def source(
    fact_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project_id = user.get("pid", "")
    authorize(user, project_id)
    fact = await db.scalar(
        select(BrandFact).where(
            BrandFact.project_id == project_id, BrandFact.id == fact_id
        )
    )
    if not fact:
        raise HTTPException(404, "Fact not found")
    if (await fact_view(db, fact))["status"] != "valid":
        raise HTTPException(409, "Only a current valid fact can be exported")
    if fact.source_kind == "owned_asset_revision":
        if not await owned_fact_fresh(db, fact, read=True):
            raise HTTPException(409, "Material evidence changed")
        return {"fact_id": fact.id, "version": fact.version, "source": fact.source_ref}
    review = await db.get(ClaimReview, fact.review_id)
    evidence = await db.scalar(
        select(ClaimEvidence).where(
            ClaimEvidence.project_id == project_id,
            ClaimEvidence.id == review.evidence_id,
        )
    )
    run = await db.scalar(
        select(IntelligenceRun).where(
            IntelligenceRun.project_id == project_id,
            IntelligenceRun.id == review.run_id,
        )
    )
    return {
        "fact_id": fact.id,
        "version": fact.version,
        "source": await citation_fact_source(db, run, evidence, review),
    }
