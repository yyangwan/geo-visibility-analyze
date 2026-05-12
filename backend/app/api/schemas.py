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
    product_category: str = ""


class ProjectUpdate(BaseModel):
    name: str | None = None
    industry: str | None = None
    product_category: str | None = None


class ProjectOut(BaseModel):
    id: int
    name: str
    industry: str
    product_category: str
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
    detail: dict | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Prompt Generation ---
class PromptGenerateRequest(BaseModel):
    project_id: int
    industry: str = ""
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
