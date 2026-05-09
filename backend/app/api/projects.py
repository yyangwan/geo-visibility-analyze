import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    BrandCreate,
    BrandOut,
    ProjectCreate,
    ProjectOut,
    PromptCreate,
    PromptOut,
    PromptGenerateRequest,
)
from app.api.auth import get_current_user
from app.database import get_db
from app.models.models import Brand, Project, Prompt, User
from app.services.prompt_gen_service import generate_prompts

router = APIRouter()


async def get_user_project(
    project_id: int, user: User, db: AsyncSession
) -> Project:
    """Get a project that belongs to the current user, or 404."""
    project = await db.get(Project, project_id)
    if not project or project.user_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.post("", response_model=ProjectOut)
async def create_project(
    data: ProjectCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = Project(name=data.name, industry=data.industry, user_id=current_user.id)
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


@router.get("", response_model=list[ProjectOut])
async def list_projects(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Project)
        .where(Project.user_id == current_user.id)
        .order_by(Project.created_at.desc())
    )
    return result.scalars().all()


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(
    project_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await get_user_project(project_id, current_user, db)


@router.post("/{project_id}/brands", response_model=BrandOut)
async def add_brand(
    project_id: int,
    data: BrandCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await get_user_project(project_id, current_user, db)
    brand = Brand(
        project_id=project_id,
        name=data.name,
        aliases=data.aliases,
        is_competitor=data.is_competitor,
    )
    db.add(brand)
    await db.commit()
    await db.refresh(brand)
    return brand


@router.get("/{project_id}/brands", response_model=list[BrandOut])
async def list_brands(
    project_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await get_user_project(project_id, current_user, db)
    result = await db.execute(
        select(Brand).where(Brand.project_id == project_id)
    )
    return result.scalars().all()


# --- Prompts ---


@router.post("/{project_id}/prompts", response_model=PromptOut)
async def add_prompt(
    project_id: int,
    data: PromptCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await get_user_project(project_id, current_user, db)
    prompt = Prompt(
        project_id=project_id,
        text=data.text,
        category=data.category,
        is_auto_generated=data.is_auto_generated,
    )
    db.add(prompt)
    await db.commit()
    await db.refresh(prompt)
    return prompt


@router.get("/{project_id}/prompts", response_model=list[PromptOut])
async def list_prompts(
    project_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await get_user_project(project_id, current_user, db)
    result = await db.execute(
        select(Prompt).where(Prompt.project_id == project_id)
    )
    return result.scalars().all()


@router.delete("/{project_id}/prompts/{prompt_id}", status_code=204)
async def delete_prompt(
    project_id: int,
    prompt_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await get_user_project(project_id, current_user, db)
    prompt = await db.get(Prompt, prompt_id)
    if not prompt or prompt.project_id != project_id:
        raise HTTPException(status_code=404, detail="Prompt not found")
    await db.delete(prompt)
    await db.commit()


@router.post("/{project_id}/prompts/generate", response_model=list[PromptOut])
async def generate_prompts_endpoint(
    project_id: int,
    data: PromptGenerateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Auto-generate prompts using AI."""
    project = await get_user_project(project_id, current_user, db)

    # Get primary brand name
    result = await db.execute(
        select(Brand).where(
            Brand.project_id == project_id,
            Brand.is_competitor == False,  # noqa: E712
        ).limit(1)
    )
    brand = result.scalar_one_or_none()
    brand_name = brand.name if brand else project.name

    generated = await generate_prompts(
        brand_name=brand_name,
        industry=project.industry,
        count=data.count,
    )

    if not generated:
        raise HTTPException(status_code=500, detail="Failed to generate prompts")

    prompts = []
    for item in generated:
        p = Prompt(
            project_id=project_id,
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
