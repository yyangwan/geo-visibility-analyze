from datetime import datetime

from pydantic import BaseModel

from app.models.models import PromptCategory, QueryStatus


# --- Projects ---
class ProjectCreate(BaseModel):
    name: str
    industry: str = "insurance"


class ProjectOut(BaseModel):
    id: int
    name: str
    industry: str
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Brands ---
class BrandCreate(BaseModel):
    name: str
    aliases: list[str] = []
    is_competitor: bool = False


class BrandOut(BaseModel):
    id: int
    name: str
    aliases: list[str]
    is_competitor: bool

    model_config = {"from_attributes": True}


# --- Prompts ---
class PromptOut(BaseModel):
    id: int
    text: str
    category: PromptCategory
    is_auto_generated: bool

    model_config = {"from_attributes": True}


# --- Audits ---
class AuditCreate(BaseModel):
    project_id: int
    platforms: list[str] = ["deepseek", "qwen"]


class AuditOut(BaseModel):
    id: int
    project_id: int
    status: QueryStatus
    platforms_json: list[str]
    created_at: datetime
    completed_at: datetime | None = None
    error_message: str | None = None

    model_config = {"from_attributes": True}


# --- Query Results ---
class QueryResultOut(BaseModel):
    id: int
    platform: str
    prompt_text: str | None = None
    brand_name: str | None = None
    mention_found: bool
    mention_position: int | None = None
    mention_context: str | None = None
    mention_confidence: float | None = None
    is_recommended: bool
    recommendation_rank: int | None = None
    error: str | None = None

    model_config = {"from_attributes": True}


# --- Reports ---
class ReportOut(BaseModel):
    id: int
    project_id: int
    audit_id: int
    overall_score: float
    mention_rate: float
    competitor_rank: int | None = None
    sentiment_positive_rate: float | None = None
    platform_scores: dict
    insights: list
    created_at: datetime

    model_config = {"from_attributes": True}
