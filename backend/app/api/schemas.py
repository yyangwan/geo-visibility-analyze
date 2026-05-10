from datetime import datetime

from pydantic import BaseModel

from app.adapters.registry import available_platforms
from app.models.models import PromptCategory, QueryStatus


# --- Auth ---
class UserRegister(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: int
    username: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Projects ---
class ProjectCreate(BaseModel):
    name: str
    industry: str = "insurance"


class ProjectOut(BaseModel):
    id: int
    name: str
    industry: str
    user_id: int
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
class PromptCreate(BaseModel):
    text: str
    category: PromptCategory = PromptCategory.RECOMMEND
    is_auto_generated: bool = True


class PromptOut(BaseModel):
    id: int
    text: str
    category: PromptCategory
    is_auto_generated: bool

    model_config = {"from_attributes": True}


# --- Audits ---
_ALL_PLATFORMS = available_platforms()


class AuditCreate(BaseModel):
    project_id: int
    platforms: list[str] = _ALL_PLATFORMS


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


# --- Scheduled Jobs ---
class ScheduledJobCreate(BaseModel):
    project_id: int
    cron_expression: str  # e.g. "0 22 * * *" = every day at 22:00
    platforms: list[str] = _ALL_PLATFORMS


class ScheduledJobOut(BaseModel):
    id: int
    project_id: int
    cron_expression: str
    platforms_json: list[str]
    is_active: bool
    last_run_at: datetime | None = None
    last_audit_id: int | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Suggestions ---
class SuggestionOut(BaseModel):
    id: int
    project_id: int
    report_id: int
    category: str
    title: str
    description: str
    priority: str
    is_resolved: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Prompt Generation ---
class PromptGenerateRequest(BaseModel):
    project_id: int
    industry: str = ""
    brand_name: str = ""
    count: int = 10
