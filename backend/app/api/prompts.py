"""Prompt API endpoints — manage AI query prompts per project."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.access import require_project_scope
from app.api.auth import get_current_user
from app.api.schemas import PromptCreate, PromptOut, PromptGenerateRequest
from app.database import get_db
from app.models.models import Prompt
from app.services.prompt_gen_service import generate_prompts

router = APIRouter()


@router.post("", response_model=list[PromptOut])
async def create_prompt(
    data: PromptCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_project_scope(current_user, data.project_id)
    prompt = Prompt(
        project_id=data.project_id,
        text=data.text,
        category=data.category,
        is_auto_generated=data.is_auto_generated,
    )
    db.add(prompt)
    await db.commit()
    await db.refresh(prompt)
    return [prompt]


@router.get("", response_model=list[PromptOut])
async def list_prompts(
    project_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_project_scope(current_user, project_id)
    result = await db.execute(
        select(Prompt).where(Prompt.project_id == project_id)
    )
    return result.scalars().all()


@router.delete("/{prompt_id}", status_code=204)
async def delete_prompt(
    prompt_id: int,
    project_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_project_scope(current_user, project_id)
    prompt = await db.get(Prompt, prompt_id)
    if not prompt or prompt.project_id != project_id:
        raise HTTPException(status_code=404, detail="Prompt not found")
    await db.delete(prompt)
    await db.commit()


@router.post("/generate", response_model=list[PromptOut])
async def generate_prompts_endpoint(
    data: PromptGenerateRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Auto-generate prompts using AI."""
    require_project_scope(current_user, data.project_id)
    generated = await generate_prompts(
        project_name=data.project_name or "",
        project_url=data.project_url or "",
        product_category=data.product_category or "",
        industry=data.industry or "",
        product_name=data.product_name or "",
        product_description=data.product_description or "",
        product_url=data.product_url or "",
        product_keywords=data.product_keywords or [],
        brand_names=data.brand_names or [],
        count=data.count,
    )

    if not generated:
        raise HTTPException(status_code=500, detail="Failed to generate prompts")

    prompts = []
    for item in generated:
        p = Prompt(
            project_id=data.project_id,
            text=item.get("text", ""),
            category=item.get("category", "recommend"),
            is_auto_generated=True,
        )
        db.add(p)
        prompts.append(p)

    await db.commit()
    for p in prompts:
        await db.refresh(p)
    return prompts
