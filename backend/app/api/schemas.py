from datetime import datetime

from pydantic import BaseModel, Field

from app.adapters.registry import available_platforms
from app.models.models import AuditStage, PromptCategory, QueryStatus, RunStatus


# --- Prompts ---
class PromptCreate(BaseModel):
    project_id: str
    text: str
    category: PromptCategory = PromptCategory.RECOMMEND
    is_auto_generated: bool = True


class PromptOut(BaseModel):
    id: int
    project_id: str
    text: str
    category: PromptCategory
    is_auto_generated: bool

    model_config = {"from_attributes": True}


# --- Audits ---
_ALL_PLATFORMS = available_platforms()


class AuditCreate(BaseModel):
    project_id: str
    platforms: list[str] = _ALL_PLATFORMS
    brands: list[dict] = []


class AuditOut(BaseModel):
    id: int
    project_id: str
    status: QueryStatus
    stage: AuditStage = AuditStage.QUEUED
    stage_status: RunStatus = RunStatus.PENDING
    platforms_json: list[str]
    brands_json: list[dict] = []
    created_at: datetime
    completed_at: datetime | None = None
    stage_started_at: datetime | None = None
    stage_updated_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    attempt_count: int = 0
    error_code: str | None = None
    error_message: str | None = None
    recoverable_error: bool = False
    next_retry_at: datetime | None = None
    locked_by_worker: str | None = None
    locked_until: datetime | None = None

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
    project_id: str
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
    project_id: str
    cron_expression: str  # e.g. "0 22 * * *" = every day at 22:00
    platforms: list[str] = _ALL_PLATFORMS


class ScheduledJobOut(BaseModel):
    id: int
    project_id: str
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
    project_id: str
    report_id: int
    category: str
    title: str
    description: str
    priority: str
    is_resolved: bool
    detail: dict | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Prompt Generation ---
class PromptGenerateRequest(BaseModel):
    project_id: str
    project_name: str = ""
    project_url: str = ""
    industry: str = ""
    product_category: str = ""
    product_name: str = ""
    product_description: str = ""
    product_url: str = ""
    product_keywords: list[str] = Field(default_factory=list)
    brand_names: list[str] = Field(default_factory=list)
    brand_name: str = ""
    count: int = 10


# --- Response Analysis ---
class ResponseAnalysisOut(BaseModel):
    id: int
    response_record_id: int
    platform: str | None = None
    prompt_text: str | None = None
    cited_sources: list = []
    brand_sentiment: str | None = None
    brand_attributes: list = []
    topics_covered: list = []
    answer_structure: str | None = None
    competitor_refs: list = []
    analysis_model: str = ""
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ContentIntelligenceOut(BaseModel):
    """Aggregated content intelligence for a project's latest audit."""
    topic_distribution: dict[str, int] = {}
    sentiment_breakdown: dict[str, int] = {}
    answer_structure_distribution: dict[str, int] = {}
    top_cited_sources: list[dict] = []
    brand_positioning_heatmap: dict[str, dict[str, str]] = {}
    token_cost_summary: dict[str, int] = {}
    analysis_status: dict[str, int] = {}
    total_responses: int = 0
    analyzed_responses: int = 0


# --- Strategic Intelligence ---

# P3.1: Source Authority Trends
class SourceAuthorityAuditPoint(BaseModel):
    audit_id: int
    date: str
    total_sources: int

class DomainTrendPoint(BaseModel):
    audit_id: int
    count: int
    authority_avg: float

class DomainTrend(BaseModel):
    domain: str
    data: list[DomainTrendPoint]

class PlatformSourcePreference(BaseModel):
    platform: str
    top_domains: list[dict]

class SourceAuthorityTrendsOut(BaseModel):
    audits: list[SourceAuthorityAuditPoint] = []
    domain_trends: list[DomainTrend] = []
    platform_preferences: list[PlatformSourcePreference] = []
    authority_trend: dict[str, list[str]] = {}


# P3.2: Competitor Positioning Map
class BrandPositioningTrajectoryPoint(BaseModel):
    audit_id: int
    date: str
    mention_rate: float
    sentiment_positive_rate: float

class BrandPositioning(BaseModel):
    name: str
    is_competitor: bool
    mention_frequency: float
    sentiment_positive_rate: float
    avg_authority: float
    mention_count: int
    trajectory: list[BrandPositioningTrajectoryPoint]

class CompetitorPositioningOut(BaseModel):
    brands: list[BrandPositioning] = []
    quadrant_labels: dict[str, str] = {}


# P3.3: Answer Structure Evolution
class StructureEvolutionPoint(BaseModel):
    audit_id: int
    count: int
    pct: float

class StructureTransition(BaseModel):
    audit_id: int
    platform: str
    prev_structure: str | None
    new_structure: str

class StructureCorrelation(BaseModel):
    mention_rate: float
    avg_position: float | None

class AnswerStructureEvolutionOut(BaseModel):
    audits: list[dict] = []
    structure_distribution: dict[str, list[StructureEvolutionPoint]] = {}
    platform_structure: dict[str, dict[str, int]] = {}
    correlation: dict[str, StructureCorrelation] = {}
    transitions: list[StructureTransition] = []


# P3.4: Multi-Audit Comparison
class MultiAuditComparisonRequest(BaseModel):
    audit_ids: list[int]

class AuditComparisonSnapshot(BaseModel):
    audit_id: int
    date: str
    overall_score: float
    mention_rate: float
    sentiment_breakdown: dict[str, int]
    top_sources: list[dict]
    competitor_mention_rates: list[dict]
    structure_distribution: dict[str, int]
    topic_distribution: dict[str, int]

class MultiAuditComparisonOut(BaseModel):
    audits: list[AuditComparisonSnapshot] = []
    diffs: dict = {}
