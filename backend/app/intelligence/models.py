"""Immutable inputs and fenced execution; IDs are project-scoped at every boundary."""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.mysql import DATETIME, LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.utils.timezone import utcnow


def new_id() -> str:
    return uuid4().hex


class Scoped:
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime().with_variant(DATETIME(fsp=6), "mysql"), default=utcnow
    )


class WorkerHeartbeat(Base):
    """Service metadata only: never store hostnames, credentials or project work."""

    __tablename__ = "intelligence_worker_heartbeats"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    state: Mapped[str] = mapped_column(String(16))
    storage_status: Mapped[str] = mapped_column(String(24))
    storage_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    disk_status: Mapped[str] = mapped_column(String(16))
    disk_checked_at: Mapped[datetime | None] = mapped_column(DateTime)


class IntelligenceRun(Scoped, Base):
    __tablename__ = "intelligence_runs"
    __table_args__ = (
        UniqueConstraint("project_id", "id"),
        UniqueConstraint("project_id", "audit_id", "revision"),
        UniqueConstraint("project_id", "audit_id", "input_hash", "signature"),
    )
    audit_id: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer)
    input_hash: Mapped[str] = mapped_column(String(64))
    signature: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="queued")
    coverage: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class AnalysisJob(Scoped, Base):
    __tablename__ = "intelligence_jobs"
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_hash"),
        Index("ix_intelligence_jobs_queue", "status", "available_at", "created_at"),
        ForeignKeyConstraint(
            ["project_id", "run_id"],
            ["intelligence_runs.project_id", "intelligence_runs.id"],
        ),
    )
    run_id: Mapped[str | None] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32))
    subject_id: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="queued")
    available_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.current_timestamp()
    )
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_token: Mapped[str | None] = mapped_column(String(32))
    lease_version: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    error_code: Mapped[str | None] = mapped_column(String(50))


class SourceDocument(Scoped, Base):
    __tablename__ = "intelligence_documents"
    __table_args__ = (
        UniqueConstraint("project_id", "id"),
        UniqueConstraint("project_id", "url_hash"),
    )
    url: Mapped[str] = mapped_column(Text)
    url_hash: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text, default="")


class SourceSnapshot(Scoped, Base):
    __tablename__ = "intelligence_snapshots"
    __table_args__ = (
        UniqueConstraint("project_id", "id"),
        UniqueConstraint("project_id", "document_id", "representation_hash"),
        ForeignKeyConstraint(
            ["project_id", "document_id"],
            ["intelligence_documents.project_id", "intelligence_documents.id"],
        ),
    )
    document_id: Mapped[str] = mapped_column(String(32))
    body_hash: Mapped[str] = mapped_column(String(64))
    representation_hash: Mapped[str] = mapped_column(String(64))
    text_key: Mapped[str] = mapped_column(String(500))
    text_sha256: Mapped[str] = mapped_column(String(64))
    raw_key: Mapped[str | None] = mapped_column(String(500))
    raw_sha256: Mapped[str | None] = mapped_column(String(64))
    completeness: Mapped[str] = mapped_column(String(20), default="unknown")
    extractor_version: Mapped[str] = mapped_column(String(50))
    integrity_status: Mapped[str] = mapped_column(String(24), default="verified")


class SourceFetch(Scoped, Base):
    __tablename__ = "intelligence_fetches"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "document_id"],
            ["intelligence_documents.project_id", "intelligence_documents.id"],
        ),
        ForeignKeyConstraint(
            ["project_id", "snapshot_id"],
            ["intelligence_snapshots.project_id", "intelligence_snapshots.id"],
        ),
    )
    document_id: Mapped[str] = mapped_column(String(32))
    snapshot_id: Mapped[str | None] = mapped_column(String(32))
    final_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24))
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(50))
    audit_id: Mapped[int | None] = mapped_column(Integer)
    job_id: Mapped[str | None] = mapped_column(String(32))


class CitationObservation(Scoped, Base):
    __tablename__ = "intelligence_citations"
    __table_args__ = (
        UniqueConstraint("project_id", "response_id", "ordinal"),
        ForeignKeyConstraint(
            ["project_id", "document_id"],
            ["intelligence_documents.project_id", "intelligence_documents.id"],
        ),
    )
    audit_id: Mapped[int] = mapped_column(Integer)
    response_id: Mapped[str] = mapped_column(String(64))
    ordinal: Mapped[int] = mapped_column(Integer)
    platform: Mapped[str] = mapped_column(String(50))
    kind: Mapped[str] = mapped_column(String(24), default="unknown")
    url_resolution: Mapped[str] = mapped_column(String(24), default="unknown")
    url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, default="")
    snippet: Mapped[str] = mapped_column(Text, default="")
    document_id: Mapped[str | None] = mapped_column(String(32))
    anchor: Mapped[dict | None] = mapped_column(JSON)


class ContentSegment(Scoped, Base):
    __tablename__ = "intelligence_segments"
    __table_args__ = (
        UniqueConstraint("project_id", "id"),
        UniqueConstraint(
            "project_id", "owner_kind", "owner_id", "ordinal", "extractor_version"
        ),
    )
    owner_kind: Mapped[str] = mapped_column(String(24))
    owner_id: Mapped[str] = mapped_column(String(64))
    ordinal: Mapped[int] = mapped_column(Integer)
    start_offset: Mapped[int] = mapped_column(Integer)
    end_offset: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT(), "mysql"))
    segment_hash: Mapped[str] = mapped_column(String(64))
    extractor_version: Mapped[str] = mapped_column(String(50))


class SemanticCache(Scoped, Base):
    __tablename__ = "intelligence_semantic_cache"
    __table_args__ = (
        UniqueConstraint("project_id", "id"),
        UniqueConstraint("project_id", "segment_hash", "signature"),
    )
    segment_hash: Mapped[str] = mapped_column(String(64))
    signature: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)


class SegmentAnalysis(Scoped, Base):
    __tablename__ = "intelligence_segment_analyses"
    __table_args__ = (
        UniqueConstraint("project_id", "run_id", "segment_id"),
        ForeignKeyConstraint(
            ["project_id", "run_id"],
            ["intelligence_runs.project_id", "intelligence_runs.id"],
        ),
        ForeignKeyConstraint(
            ["project_id", "segment_id"],
            ["intelligence_segments.project_id", "intelligence_segments.id"],
        ),
        ForeignKeyConstraint(
            ["project_id", "cache_id"],
            [
                "intelligence_semantic_cache.project_id",
                "intelligence_semantic_cache.id",
            ],
        ),
    )
    run_id: Mapped[str] = mapped_column(String(32))
    segment_id: Mapped[str] = mapped_column(String(32))
    cache_id: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24))


class IntelligenceEntity(Scoped, Base):
    __tablename__ = "intelligence_entities"
    __table_args__ = (
        UniqueConstraint("project_id", "id"),
        UniqueConstraint("project_id", "identity_hash"),
    )
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(24))
    identity_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="provisional")


class EntityMention(Scoped, Base):
    __tablename__ = "intelligence_mentions"
    __table_args__ = (
        UniqueConstraint("project_id", "id"),
        UniqueConstraint(
            "project_id",
            "run_id",
            "segment_id",
            "start_offset",
            "end_offset",
            "entity_id",
        ),
        ForeignKeyConstraint(
            ["project_id", "run_id"],
            ["intelligence_runs.project_id", "intelligence_runs.id"],
        ),
        ForeignKeyConstraint(
            ["project_id", "segment_id"],
            ["intelligence_segments.project_id", "intelligence_segments.id"],
        ),
        ForeignKeyConstraint(
            ["project_id", "entity_id"],
            ["intelligence_entities.project_id", "intelligence_entities.id"],
        ),
    )
    run_id: Mapped[str] = mapped_column(String(32))
    segment_id: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(32))
    start_offset: Mapped[int] = mapped_column(Integer)
    end_offset: Mapped[int] = mapped_column(Integer)
    quote: Mapped[str] = mapped_column(Text)
    sentiment: Mapped[str] = mapped_column(String(24), default="unknown")
    attributes: Mapped[list] = mapped_column(JSON, default=list)


class PromptVersion(Scoped, Base):
    __tablename__ = "intelligence_prompt_versions"
    __table_args__ = (
        UniqueConstraint("project_id", "id"),
        UniqueConstraint("project_id", "prompt_id", "text_hash"),
    )
    prompt_id: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT(), "mysql"))
    text_hash: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(50))


class AuditSample(Scoped, Base):
    __tablename__ = "intelligence_samples"
    __table_args__ = (
        UniqueConstraint("project_id", "id"),
        UniqueConstraint(
            "project_id", "audit_id", "prompt_version_id", "platform", "sample_index"
        ),
        ForeignKeyConstraint(
            ["project_id", "prompt_version_id"],
            [
                "intelligence_prompt_versions.project_id",
                "intelligence_prompt_versions.id",
            ],
        ),
    )
    audit_id: Mapped[int] = mapped_column(Integer)
    prompt_version_id: Mapped[str] = mapped_column(String(32))
    platform: Mapped[str] = mapped_column(String(50))
    surface: Mapped[str] = mapped_column(String(20))
    sample_index: Mapped[int] = mapped_column(Integer)
    conditions: Mapped[dict] = mapped_column(JSON)
    condition_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    selected_response_id: Mapped[str | None] = mapped_column(String(32))


class CaptureAttempt(Scoped, Base):
    __tablename__ = "intelligence_capture_attempts"
    __table_args__ = (
        UniqueConstraint("project_id", "sample_id", "attempt_no"),
        UniqueConstraint("device_task_id"),
        ForeignKeyConstraint(
            ["project_id", "sample_id"],
            ["intelligence_samples.project_id", "intelligence_samples.id"],
        ),
    )
    sample_id: Mapped[str] = mapped_column(String(32))
    attempt_no: Mapped[int] = mapped_column(Integer)
    device_task_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    accepted_hash: Mapped[str | None] = mapped_column(String(64))
    accepted_token_hash: Mapped[str | None] = mapped_column(String(64))
    response_id: Mapped[str | None] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(100))


class CapturedResponse(Scoped, Base):
    __tablename__ = "intelligence_responses"
    __table_args__ = (
        UniqueConstraint("project_id", "id"),
        UniqueConstraint("project_id", "sample_id", "attempt_no"),
        ForeignKeyConstraint(
            ["project_id", "sample_id"],
            ["intelligence_samples.project_id", "intelligence_samples.id"],
        ),
    )
    sample_id: Mapped[str] = mapped_column(String(32))
    attempt_no: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT(), "mysql"))
    content_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime().with_variant(DATETIME(fsp=6), "mysql")
    )


class RunRefreshRequest(Scoped, Base):
    __tablename__ = "intelligence_refresh_requests"
    __table_args__ = (UniqueConstraint("project_id", "event_hash"),)
    audit_id: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(50))
    event_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="pending")


class ClaimEvidence(Scoped, Base):
    __tablename__ = "intelligence_claim_evidence"
    __table_args__ = (
        UniqueConstraint("project_id", "id"),
        UniqueConstraint(
            "project_id", "run_id", "claim_hash", "citation_id", "source_segment_id"
        ),
        ForeignKeyConstraint(
            ["project_id", "run_id"],
            ["intelligence_runs.project_id", "intelligence_runs.id"],
        ),
    )
    run_id: Mapped[str] = mapped_column(String(32))
    claim_hash: Mapped[str] = mapped_column(String(64))
    answer_segment_id: Mapped[str] = mapped_column(String(32))
    citation_id: Mapped[str] = mapped_column(String(32))
    source_segment_id: Mapped[str] = mapped_column(String(32))
    answer_quote: Mapped[str] = mapped_column(Text)
    source_quote: Mapped[str] = mapped_column(Text)
    relation: Mapped[str] = mapped_column(String(24))
    method: Mapped[str] = mapped_column(String(24))
    review_state: Mapped[str] = mapped_column(String(24), default="needs_review")


class SamplingAudit(Scoped, Base):
    __tablename__ = "intelligence_sampling_audits"
    __table_args__ = (
        UniqueConstraint("audit_id"),
        UniqueConstraint("project_id", "idempotency_key"),
    )
    audit_id: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    expected_samples: Mapped[int] = mapped_column(Integer)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)


class AuditIntelligenceState(Scoped, Base):
    __tablename__ = "intelligence_audit_state"
    __table_args__ = (UniqueConstraint("project_id", "audit_id"),)
    audit_id: Mapped[int] = mapped_column(Integer)
    input_revision: Mapped[int] = mapped_column(Integer, default=0)
    handled_revision: Mapped[int] = mapped_column(Integer, default=0)
    refresh_generation: Mapped[int] = mapped_column(Integer, default=0)
    pending_job_id: Mapped[str | None] = mapped_column(String(32))
    window_start: Mapped[datetime | None] = mapped_column(DateTime)
    automatic_runs: Mapped[int] = mapped_column(Integer, default=0)
    published_revision: Mapped[int] = mapped_column(Integer, default=0)
    published_run_id: Mapped[str | None] = mapped_column(String(32))


class EntityResolutionEvent(Scoped, Base):
    __tablename__ = "intelligence_entity_resolution_events"
    __table_args__ = (
        UniqueConstraint("project_id", "entity_id", "version"),
        UniqueConstraint("project_id", "idempotency_key"),
        ForeignKeyConstraint(
            ["project_id", "entity_id"],
            ["intelligence_entities.project_id", "intelligence_entities.id"],
        ),
        ForeignKeyConstraint(
            ["project_id", "run_id"],
            ["intelligence_runs.project_id", "intelligence_runs.id"],
        ),
    )
    entity_id: Mapped[str] = mapped_column(String(32))
    run_id: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer)
    decision: Mapped[str] = mapped_column(String(24))
    canonical_name: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[str] = mapped_column(String(200))
    idempotency_key: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))


class PlatformDiagnosis(Scoped, Base):
    __tablename__ = "intelligence_diagnoses"
    __table_args__ = (
        UniqueConstraint("project_id", "id"),
        UniqueConstraint("project_id", "run_id", "diagnosis_hash"),
        ForeignKeyConstraint(
            ["project_id", "run_id"],
            ["intelligence_runs.project_id", "intelligence_runs.id"],
        ),
    )
    run_id: Mapped[str] = mapped_column(String(32))
    diagnosis_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)


class ActionPlan(Scoped, Base):
    __tablename__ = "intelligence_action_plans"
    __table_args__ = (
        UniqueConstraint("project_id", "id"),
        UniqueConstraint("project_id", "run_id", "diagnosis_id"),
        UniqueConstraint("project_id", "idempotency_key"),
        ForeignKeyConstraint(
            ["project_id", "run_id"],
            ["intelligence_runs.project_id", "intelligence_runs.id"],
        ),
        ForeignKeyConstraint(
            ["project_id", "diagnosis_id"],
            ["intelligence_diagnoses.project_id", "intelligence_diagnoses.id"],
        ),
    )
    run_id: Mapped[str] = mapped_column(String(32))
    diagnosis_id: Mapped[str] = mapped_column(String(32))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default="proposed")
    title: Mapped[str] = mapped_column(Text)
    target: Mapped[dict] = mapped_column(JSON)
    actor_id: Mapped[str] = mapped_column(String(200))
    idempotency_key: Mapped[str] = mapped_column(String(100))


class ActionApproval(Scoped, Base):
    __tablename__ = "intelligence_action_approvals"
    __table_args__ = (
        UniqueConstraint("project_id", "action_id", "revision"),
        UniqueConstraint("project_id", "idempotency_key"),
        ForeignKeyConstraint(
            ["project_id", "action_id"],
            ["intelligence_action_plans.project_id", "intelligence_action_plans.id"],
        ),
    )
    action_id: Mapped[str] = mapped_column(String(32))
    revision: Mapped[int] = mapped_column(Integer)
    input_stamp: Mapped[str] = mapped_column(String(64))
    signature: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))


class ClaimReview(Scoped, Base):
    __tablename__ = "intelligence_claim_reviews"
    __table_args__ = (
        UniqueConstraint("project_id", "id"),
        UniqueConstraint("project_id", "evidence_id", "version"),
        UniqueConstraint("project_id", "idempotency_key"),
        ForeignKeyConstraint(
            ["project_id", "run_id"],
            ["intelligence_runs.project_id", "intelligence_runs.id"],
        ),
        ForeignKeyConstraint(
            ["project_id", "evidence_id"],
            [
                "intelligence_claim_evidence.project_id",
                "intelligence_claim_evidence.id",
            ],
        ),
    )
    run_id: Mapped[str] = mapped_column(String(32))
    evidence_id: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer)
    relation: Mapped[str] = mapped_column(String(24))
    source_quote: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[str] = mapped_column(String(200))
    evidence_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))


class BrandFact(Scoped, Base):
    __tablename__ = "intelligence_brand_facts"
    __table_args__ = (
        UniqueConstraint("project_id", "fact_key", "version"),
        UniqueConstraint("project_id", "idempotency_key"),
        ForeignKeyConstraint(
            ["project_id", "entity_id"],
            ["intelligence_entities.project_id", "intelligence_entities.id"],
        ),
        ForeignKeyConstraint(
            ["project_id", "review_id"],
            ["intelligence_claim_reviews.project_id", "intelligence_claim_reviews.id"],
        ),
    )
    fact_key: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    entity_id: Mapped[str] = mapped_column(String(32))
    identity_event_id: Mapped[str] = mapped_column(String(32))
    review_id: Mapped[str] = mapped_column(String(32))
    statement: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(Text)
    valid_from: Mapped[datetime] = mapped_column(DateTime)
    valid_until: Mapped[datetime] = mapped_column(DateTime)
    state: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[str] = mapped_column(String(200))
    idempotency_key: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))


class DraftApproval(Scoped, Base):
    __tablename__ = "intelligence_draft_approvals"
    __table_args__ = (
        UniqueConstraint("project_id", "action_id", "version"),
        UniqueConstraint("project_id", "idempotency_key"),
        ForeignKeyConstraint(
            ["project_id", "action_id"],
            ["intelligence_action_plans.project_id", "intelligence_action_plans.id"],
        ),
    )
    action_id: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    input_stamp: Mapped[str] = mapped_column(String(64))
    signature: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime().with_variant(DATETIME(fsp=6), "mysql")
    )
    idempotency_key: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))


class MeasurementCohort(Scoped, Base):
    __tablename__ = "intelligence_measurement_cohorts"
    __table_args__ = (
        UniqueConstraint("project_id", "id"),
        UniqueConstraint("project_id", "idempotency_key"),
        ForeignKeyConstraint(
            ["project_id", "baseline_run_id"],
            ["intelligence_runs.project_id", "intelligence_runs.id"],
        ),
    )
    baseline_run_id: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(200))
    specification: Mapped[dict] = mapped_column(JSON)
    specification_hash: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[str] = mapped_column(String(200))
    idempotency_key: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))


class CohortRetest(Scoped, Base):
    __tablename__ = "intelligence_cohort_retests"
    __table_args__ = (
        UniqueConstraint("project_id", "cohort_id", "idempotency_key"),
        UniqueConstraint("project_id", "audit_id"),
        ForeignKeyConstraint(
            ["project_id", "cohort_id"],
            [
                "intelligence_measurement_cohorts.project_id",
                "intelligence_measurement_cohorts.id",
            ],
        ),
    )
    cohort_id: Mapped[str] = mapped_column(String(32))
    audit_id: Mapped[int] = mapped_column(Integer)
    actor_id: Mapped[str] = mapped_column(String(200))
    idempotency_key: Mapped[str] = mapped_column(String(100))


class MeasurementResult(Scoped, Base):
    __tablename__ = "intelligence_measurement_results"
    __table_args__ = (
        UniqueConstraint("project_id", "cohort_id", "followup_run_id"),
        ForeignKeyConstraint(
            ["project_id", "cohort_id"],
            [
                "intelligence_measurement_cohorts.project_id",
                "intelligence_measurement_cohorts.id",
            ],
        ),
        ForeignKeyConstraint(
            ["project_id", "followup_run_id"],
            ["intelligence_runs.project_id", "intelligence_runs.id"],
        ),
    )
    cohort_id: Mapped[str] = mapped_column(String(32))
    followup_run_id: Mapped[str] = mapped_column(String(32))
    comparison_signature: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)
    actor_id: Mapped[str] = mapped_column(String(200))


class PublishedAsset(Scoped, Base):
    __tablename__ = "intelligence_published_assets"
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key"),
        ForeignKeyConstraint(
            ["project_id", "action_id"],
            ["intelligence_action_plans.project_id", "intelligence_action_plans.id"],
        ),
    )
    action_id: Mapped[str] = mapped_column(String(32))
    url: Mapped[str] = mapped_column(String(4096))
    channel: Mapped[str] = mapped_column(String(50))
    content_hash: Mapped[str] = mapped_column(String(64))
    published_at: Mapped[datetime] = mapped_column(
        DateTime().with_variant(DATETIME(fsp=6), "mysql")
    )
    reason: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[str] = mapped_column(String(200))
    idempotency_key: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))


class EntityAliasEvent(Scoped, Base):
    __tablename__ = "intelligence_entity_alias_events"
    __table_args__ = (
        UniqueConstraint("project_id", "source_entity_id", "version"),
        UniqueConstraint("project_id", "idempotency_key"),
        ForeignKeyConstraint(
            ["project_id", "source_entity_id"],
            ["intelligence_entities.project_id", "intelligence_entities.id"],
        ),
        ForeignKeyConstraint(
            ["project_id", "target_entity_id"],
            ["intelligence_entities.project_id", "intelligence_entities.id"],
        ),
        ForeignKeyConstraint(
            ["project_id", "run_id"],
            ["intelligence_runs.project_id", "intelligence_runs.id"],
        ),
    )
    source_entity_id: Mapped[str] = mapped_column(String(32))
    target_entity_id: Mapped[str | None] = mapped_column(String(32))
    target_name: Mapped[str | None] = mapped_column(String(200))
    run_id: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer)
    decision: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[str] = mapped_column(String(200))
    idempotency_key: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))


class MentionBindingEvent(Scoped, Base):
    __tablename__ = "intelligence_mention_binding_events"
    __table_args__ = (
        UniqueConstraint("project_id", "binding_key", "version"),
        UniqueConstraint("project_id", "idempotency_key"),
        ForeignKeyConstraint(
            ["project_id", "target_entity_id"],
            ["intelligence_entities.project_id", "intelligence_entities.id"],
        ),
        ForeignKeyConstraint(
            ["project_id", "mention_id"],
            ["intelligence_mentions.project_id", "intelligence_mentions.id"],
        ),
    )
    binding_key: Mapped[str] = mapped_column(String(64))
    mention_id: Mapped[str] = mapped_column(String(32))
    target_entity_id: Mapped[str | None] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer)
    decision: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[str] = mapped_column(String(200))
    idempotency_key: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))


class ProjectContextEvent(Scoped, Base):
    __tablename__ = "intelligence_project_context_events"
    __table_args__ = (UniqueConstraint("project_id", "version"),)
    version: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    brands: Mapped[list] = mapped_column(JSON)
    received_by: Mapped[str] = mapped_column(String(200))


class ProjectContextHead(Base):
    __tablename__ = "intelligence_project_context_heads"
    project_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
