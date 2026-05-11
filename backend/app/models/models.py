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


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    projects: Mapped[list["Project"]] = relationship(back_populates="user")


class QueryStatus(str, PyEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"


class PromptCategory(str, PyEnum):
    RECOMMEND = "recommend"
    COMPARE = "compare"
    EVALUATE = "evaluate"
    SCENARIO = "scenario"


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        Index("ix_projects_user_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    industry: Mapped[str] = mapped_column(String(100), default="insurance")
    product_category: Mapped[str] = mapped_column(String(200), default="")
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="projects")
    brands: Mapped[list["Brand"]] = relationship(back_populates="project")
    prompts: Mapped[list["Prompt"]] = relationship(back_populates="project")
    reports: Mapped[list["Report"]] = relationship(back_populates="project")


class Brand(Base):
    __tablename__ = "brands"
    __table_args__ = (
        Index("ix_brands_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    is_competitor: Mapped[bool] = mapped_column(Boolean, default=False)

    project: Mapped["Project"] = relationship(back_populates="brands")


class Prompt(Base):
    __tablename__ = "prompts"
    __table_args__ = (
        Index("ix_prompts_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id"))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[PromptCategory] = mapped_column(
        Enum(PromptCategory, values_callable=lambda obj: [e.value for e in obj]),
        default=PromptCategory.RECOMMEND,
    )
    is_auto_generated: Mapped[bool] = mapped_column(Boolean, default=True)

    project: Mapped["Project"] = relationship(back_populates="prompts")


class Audit(Base):
    __tablename__ = "audits"
    __table_args__ = (
        Index("ix_audits_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id"))
    status: Mapped[QueryStatus] = mapped_column(
        Enum(QueryStatus, values_callable=lambda obj: [e.value for e in obj]),
        default=QueryStatus.PENDING,
    )
    platforms_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    results: Mapped[list["QueryResult"]] = relationship(back_populates="audit")
    response_records: Mapped[list["PlatformResponseRecord"]] = relationship(back_populates="audit_rel")


class PlatformResponseRecord(Base):
    """Stores raw AI platform response once, referenced by multiple QueryResults.

    One record per (audit, prompt, platform) — deduplicates the same response_text
    across multiple brand results. Analysis runs per-record, not per-QueryResult.
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

    # relationships
    audit_rel: Mapped["Audit"] = relationship(back_populates="response_records")
    query_results: Mapped[list["QueryResult"]] = relationship(back_populates="response_record")


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
    brand_id: Mapped[int] = mapped_column(Integer, ForeignKey("brands.id"))
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

    # FK to deduplicated response record (nullable for backward compat)
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
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id"))
    audit_id: Mapped[int] = mapped_column(Integer, ForeignKey("audits.id"))
    overall_score: Mapped[float] = mapped_column(Float, default=0)
    mention_rate: Mapped[float] = mapped_column(Float, default=0)
    competitor_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sentiment_positive_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    platform_scores: Mapped[dict] = mapped_column(JSON, default=dict)
    insights: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    project: Mapped["Project"] = relationship(back_populates="reports")


class Suggestion(Base):
    __tablename__ = "suggestions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id"))
    report_id: Mapped[int] = mapped_column(Integer, ForeignKey("reports.id"))
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(String(20), default="medium")
    is_resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    project: Mapped["Project"] = relationship()
    report: Mapped["Report"] = relationship()


class ScheduledJob(Base):
    __tablename__ = "scheduled_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id"))
    cron_expression: Mapped[str] = mapped_column(String(100), nullable=False)
    platforms_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_audit_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SourceCitation(Base):
    """Tracks which domains/sources are cited by AI platforms per audit.

    Denormalized (has project_id directly) for efficient trend queries.
    audit_id is SET NULL on audit deletion to preserve trend data (D9).
    """
    __tablename__ = "source_citations"
    __table_args__ = (
        Index("ix_source_domain", "domain"),
        Index("ix_source_project_audit", "project_id", "audit_id"),
        Index("ix_source_unique", "project_id", "audit_id", "domain", "platform",
              unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id"))
    audit_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("audits.id", ondelete="SET NULL"), nullable=True
    )
    domain: Mapped[str] = mapped_column(String(200), nullable=False)
    urls: Mapped[list] = mapped_column(JSON, default=list)
    citation_count: Mapped[int] = mapped_column(Integer, default=1)
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
