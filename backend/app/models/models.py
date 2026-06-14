from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class QueryStatus(str, PyEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"


class AuditStage(str, PyEnum):
    QUEUED = "queued"
    QUERYING = "querying"
    PERSISTING = "persisting"
    CALCULATING = "calculating"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    STALLED = "stalled"


class RunStatus(str, PyEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"


class PromptCategory(str, PyEnum):
    RECOMMEND = "recommend"
    COMPARE = "compare"
    EVALUATE = "evaluate"
    SCENARIO = "scenario"
    PROBLEM_SOLUTION = "problem_solution"
    ALTERNATIVE_FINDING = "alternative_finding"
    DECISION_HELP = "decision_help"
    REGRET_AVOIDANCE = "regret_avoidance"
    PERFORMANCE_SPECS = "performance_specs"


class Prompt(Base):
    __tablename__ = "prompts"
    __table_args__ = (
        Index("ix_prompts_project_id", "project_id"),
        Index("ix_prompts_deleted_at", "deleted_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[str] = mapped_column(String(50), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[PromptCategory] = mapped_column(
        Enum(PromptCategory, values_callable=lambda obj: [e.value for e in obj]),
        default=PromptCategory.RECOMMEND,
    )
    is_auto_generated: Mapped[bool] = mapped_column(Boolean, default=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Audit(Base):
    __tablename__ = "audits"
    __table_args__ = (
        Index("ix_audits_project_id", "project_id"),
        Index("ix_audits_analysis_run_id", "analysis_run_id", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[QueryStatus] = mapped_column(
        Enum(QueryStatus, values_callable=lambda obj: [e.value for e in obj]),
        default=QueryStatus.PENDING,
    )
    stage: Mapped[AuditStage] = mapped_column(
        Enum(AuditStage, values_callable=lambda obj: [e.value for e in obj]),
        default=AuditStage.QUEUED,
    )
    stage_status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, values_callable=lambda obj: [e.value for e in obj]),
        default=RunStatus.PENDING,
    )
    platforms_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    brands_json: Mapped[list[dict]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stage_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stage_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    analysis_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    recoverable_error: Mapped[bool] = mapped_column(Boolean, default=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked_by_worker: Mapped[str | None] = mapped_column(String(100), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    results: Mapped[list["QueryResult"]] = relationship(back_populates="audit")
    response_records: Mapped[list["PlatformResponseRecord"]] = relationship(back_populates="audit_rel")
    stage_runs: Mapped[list["AuditStageRun"]] = relationship(
        back_populates="audit",
        cascade="all, delete-orphan",
    )
    platform_runs: Mapped[list["AuditPlatformRun"]] = relationship(
        back_populates="audit",
        cascade="all, delete-orphan",
    )
    event_logs: Mapped[list["AuditEventLog"]] = relationship(
        back_populates="audit",
        cascade="all, delete-orphan",
    )


class PlatformResponseRecord(Base):
    """Stores raw AI platform response once, referenced by multiple QueryResults.

    Issue 1.2: Extended to support complete response archiving:
    - raw_response: Full platform API response as JSON
    - raw_response_text: Text representation for search/indexing
    - search_metadata: Search query, reasoning, and triggered status
    - request_params: Request parameters sent to the platform
    - parse_error: Error message if parsing failed (non-blocking)
    """
    __tablename__ = "platform_response_records"
    __table_args__ = (
        Index("ix_prr_audit_id", "audit_id"),
        Index("ix_prr_unique", "audit_id", "prompt_id", "platform", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    audit_id: Mapped[int] = mapped_column(Integer, ForeignKey("audits.id"))
    prompt_id: Mapped[int] = mapped_column(Integer, ForeignKey("prompts.id"))
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    response_text: Mapped[str] = mapped_column(Text, nullable=True)
    citations: Mapped[list] = mapped_column(JSON, default=list)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    response_model: Mapped[str] = mapped_column(String(100), default="")
    finish_reason: Mapped[str] = mapped_column(String(20), default="")
    search_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Issue 1.2: Raw response archiving fields
    raw_response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    raw_response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    search_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    request_params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    parse_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_snapshot_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    audit_rel: Mapped["Audit"] = relationship(back_populates="response_records")
    query_results: Mapped[list["QueryResult"]] = relationship(back_populates="response_record")
    analysis: Mapped["ResponseAnalysis | None"] = relationship(back_populates="response_record_rel")
    platform_run: Mapped["AuditPlatformRun | None"] = relationship(back_populates="response_record", uselist=False)


class QueryResult(Base):
    __tablename__ = "query_results"
    __table_args__ = (
        Index("ix_query_results_audit_id", "audit_id"),
        Index("ix_query_results_brand_id", "brand_id"),
        Index("ix_query_results_platform", "platform"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    audit_id: Mapped[int] = mapped_column(Integer, ForeignKey("audits.id"))
    prompt_id: Mapped[int] = mapped_column(Integer, ForeignKey("prompts.id"))
    brand_id: Mapped[str] = mapped_column(String(50), nullable=False)
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    response_text: Mapped[str] = mapped_column(Text, nullable=True)
    mention_found: Mapped[bool] = mapped_column(Boolean, default=False)
    mention_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mention_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    mention_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_recommended: Mapped[bool] = mapped_column(Boolean, default=False)
    recommendation_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    response_record_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("platform_response_records.id"), nullable=True
    )

    audit: Mapped["Audit"] = relationship(back_populates="results")
    response_record: Mapped["PlatformResponseRecord | None"] = relationship(back_populates="query_results")

    @property
    def text(self) -> str | None:
        """Get response text via response_record, falling back to local column."""
        if self.response_record is not None:
            return self.response_record.response_text
        return self.response_text


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (
        Index("ix_reports_project_id", "project_id"),
        Index("ix_reports_audit_id", "audit_id", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[str] = mapped_column(String(50), nullable=False)
    audit_id: Mapped[int] = mapped_column(Integer, ForeignKey("audits.id"))
    overall_score: Mapped[float] = mapped_column(Float, default=0)
    mention_rate: Mapped[float] = mapped_column(Float, default=0)
    competitor_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sentiment_positive_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    platform_scores: Mapped[dict] = mapped_column(JSON, default=dict)
    insights: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Suggestion(Base):
    __tablename__ = "suggestions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[str] = mapped_column(String(50), nullable=False)
    report_id: Mapped[int] = mapped_column(Integer, ForeignKey("reports.id"))
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(String(20), default="medium")
    is_resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ScheduledJob(Base):
    __tablename__ = "scheduled_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    cron_expression: Mapped[str] = mapped_column(String(100), nullable=False)
    platforms_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_audit_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ResponseAnalysis(Base):
    """Per-response analysis results."""
    __tablename__ = "response_analyses"
    __table_args__ = (
        Index("ix_ra_response_record_id", "response_record_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    response_record_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("platform_response_records.id"), unique=True
    )
    cited_sources: Mapped[list] = mapped_column(JSON, default=list)
    brand_sentiment: Mapped[str | None] = mapped_column(String(20), nullable=True)
    brand_attributes: Mapped[list] = mapped_column(JSON, default=list)
    topics_covered: Mapped[list] = mapped_column(JSON, default=list)
    answer_structure: Mapped[str | None] = mapped_column(String(20), nullable=True)
    competitor_refs: Mapped[list] = mapped_column(JSON, default=list)
    analysis_model: Mapped[str] = mapped_column(String(100), default="")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    response_record_rel: Mapped["PlatformResponseRecord"] = relationship(back_populates="analysis")


class SourceCitation(Base):
    """Tracks which domains/sources are cited by AI platforms per audit."""
    __tablename__ = "source_citations"
    __table_args__ = (
        Index("ix_source_domain", "domain"),
        Index("ix_source_project_audit", "project_id", "audit_id"),
        Index("ix_source_unique", "project_id", "audit_id", "domain", "platform",
              unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    audit_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("audits.id", ondelete="SET NULL"), nullable=True
    )
    domain: Mapped[str] = mapped_column(String(200), nullable=False)
    urls: Mapped[list] = mapped_column(JSON, default=list)
    citation_count: Mapped[int] = mapped_column(Integer, default=1)
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AuditStageRun(Base):
    __tablename__ = "audit_stage_runs"
    __table_args__ = (
        Index("ix_asr_audit_id", "audit_id"),
        Index("ix_asr_audit_stage", "audit_id", "stage_name"),
        Index("ix_asr_status", "status"),
        Index("ix_asr_unique", "audit_id", "stage_name", "attempt_no", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    audit_id: Mapped[int] = mapped_column(Integer, ForeignKey("audits.id", ondelete="CASCADE"))
    stage_name: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, values_callable=lambda obj: [e.value for e in obj]),
        default=RunStatus.PENDING,
    )
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    output_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    audit: Mapped["Audit"] = relationship(back_populates="stage_runs")


class AuditPlatformRun(Base):
    __tablename__ = "audit_platform_runs"
    __table_args__ = (
        Index("ix_apr_audit_id", "audit_id"),
        Index("ix_apr_audit_platform", "audit_id", "platform"),
        Index("ix_apr_status", "status"),
        Index("ix_apr_unique", "audit_id", "prompt_id", "platform", "attempt_no", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    audit_id: Mapped[int] = mapped_column(Integer, ForeignKey("audits.id", ondelete="CASCADE"))
    stage_run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("audit_stage_runs.id", ondelete="SET NULL"), nullable=True
    )
    prompt_id: Mapped[int] = mapped_column(Integer, ForeignKey("prompts.id"))
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, values_callable=lambda obj: [e.value for e in obj]),
        default=RunStatus.PENDING,
    )
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_record_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("platform_response_records.id", ondelete="SET NULL"), nullable=True, unique=True
    )
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    retry_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    audit: Mapped["Audit"] = relationship(back_populates="platform_runs")
    response_record: Mapped["PlatformResponseRecord | None"] = relationship(
        back_populates="platform_run"
    )


class AuditEventLog(Base):
    __tablename__ = "audit_events_log"
    __table_args__ = (
        Index("ix_ael_audit_id", "audit_id"),
        Index("ix_ael_stage_name", "stage_name"),
        Index("ix_ael_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    audit_id: Mapped[int] = mapped_column(Integer, ForeignKey("audits.id", ondelete="CASCADE"))
    stage_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    audit: Mapped["Audit"] = relationship(back_populates="event_logs")


class PlatformConfig(Base):
    """Platform-specific configuration for search behavior and parsing rules.

    Stores:
    - Search parameters (enable_search, search_mode, forced_search, etc.)
    - Request defaults (model, temperature, max_tokens, etc.)
    - Parsing rules (citation extraction patterns, search detection rules)
    - Response mapping (how to map platform-specific fields to standard fields)

    Supports versioning: each update increments config_version.
    """
    __tablename__ = "platform_configs"
    __table_args__ = (
        Index("ix_platform_configs_platform", "platform"),
        Index("ix_platform_configs_is_active", "is_active"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    config_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    config_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
