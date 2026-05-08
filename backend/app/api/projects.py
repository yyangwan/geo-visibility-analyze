from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import BrandCreate, BrandOut, ProjectCreate, ProjectOut
from app.database import get_db
from app.models.models import Brand, Project

router = APIRouter()


@router.post("", response_model=ProjectOut)
async def create_project(data: ProjectCreate, db: AsyncSession = Depends(get_db)):
    project = Project(name=data.name, industry=data.industry)
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


@router.get("", response_model=list[ProjectOut])
async def list_projects(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Project).order_by(Project.created_at.desc()))
    return result.scalars().all()


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(project_id: int, db: AsyncSession = Depends(get_db)):
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.post("/{project_id}/brands", response_model=BrandOut)
async def add_brand(
    project_id: int, data: BrandCreate, db: AsyncSession = Depends(get_db)
):
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
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
async def list_brands(project_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Brand).where(Brand.project_id == project_id)
    )
    return result.scalars().all()
