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
        Enum(PromptCategory), default=PromptCategory.RECOMMEND
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
        Enum(QueryStatus), default=QueryStatus.PENDING
    )
    platforms_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    results: Mapped[list["QueryResult"]] = relationship(back_populates="audit")


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

    audit: Mapped["Audit"] = relationship(back_populates="results")


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
