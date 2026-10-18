"""Project-scoped immutable analysis views; no blobs/keys or raw HTML exposed."""

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.access import get_audit_for_project, require_project_scope
from app.api.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.intelligence.capture_contract import SamplingStart, SourceRetry
from app.intelligence.ingestion import prepare_run
from app.intelligence.models import (
    AnalysisJob,
    AuditIntelligenceState,
    AuditSample,
    CaptureAttempt,
    CitationObservation,
    ContentSegment,
    IntelligenceRun,
    SamplingAudit,
    SourceFetch,
)
from app.intelligence.sampling import create_sampling_audit
from app.utils.timezone import utc_isoformat, utcnow

router = APIRouter()

from app.intelligence.project_context import (
    ContextRequest,
    context_snapshot,
    receive_context,
)


@router.get("/projects/{project_id}/dashboard")
async def intelligence_dashboard(
    project_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.intelligence.dashboard import project_dashboard

    require_project_scope(user, project_id)
    return await project_dashboard(db, project_id)


@router.get("/projects/{project_id}/workspace")
async def intelligence_workspace(
    project_id: str,
    audit_id: int | None = Query(None, ge=1),
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.intelligence.workspace import analysis_workspace

    require_project_scope(user, project_id)
    return await analysis_workspace(db, project_id, audit_id)


@router.get("/projects/{project_id}/entities/{entity_id}/binding-proof")
async def entity_binding_proof(
    project_id: str,
    entity_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.intelligence.project_context import binding_proof

    require_project_scope(user, project_id)
    return await binding_proof(db, project_id, entity_id)


@router.get("/projects/{project_id}/brand-context")
async def project_brand_context(
    project_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_project_scope(user, project_id)
    return {"context": await context_snapshot(db, project_id)}


@router.post("/projects/{project_id}/brand-context")
async def sync_project_brand_context(
    project_id: str,
    data: ContextRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_project_scope(user, project_id)
    action_writer(user)
    event = await receive_context(db, project_id, data, str(user["sub"]))
    await db.commit()
    return {"version": event.version, "content_hash": event.content_hash}


from app.intelligence.aliases import AliasRequest, alias_snapshot, decide_alias
from app.intelligence.mention_bindings import (
    MentionBindingRequest,
    binding_snapshot,
    decide_binding,
)


@router.get("/projects/{project_id}/mention-bindings")
async def mention_bindings_view(
    project_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_project_scope(user, project_id)
    return {"bindings": await binding_snapshot(db, project_id)}


@router.post("/runs/{run_id}/mention-bindings", status_code=202)
async def review_mention_binding(
    run_id: str,
    data: MentionBindingRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    run = await action_run(db, user, run_id)
    action_writer(user)
    if run.status not in ("ready", "partial"):
        raise HTTPException(409, "Wait for published analysis")
    event = await decide_binding(db, run, data, str(user["sub"]))
    await db.commit()
    return {
        "id": event.id,
        "version": event.version,
        "target_entity_id": event.target_entity_id,
        "refresh_pending": True,
    }


@router.get("/projects/{project_id}/aliases")
async def aliases_view(
    project_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_project_scope(user, project_id)
    return {"aliases": await alias_snapshot(db, project_id)}


@router.post("/runs/{run_id}/aliases", status_code=202)
async def review_alias(
    run_id: str,
    data: AliasRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    run = await action_run(db, user, run_id)
    action_writer(user)
    if run.status not in ("ready", "partial"):
        raise HTTPException(409, "Wait for published analysis")
    event = await decide_alias(db, run, data, str(user["sub"]))
    await db.commit()
    return {
        "id": event.id,
        "version": event.version,
        "decision": event.decision,
        "refresh_pending": True,
    }


from app.intelligence.measurement import (
    AssetRequest,
    CohortRequest,
    CompareRequest,
    RetestRequest,
    asset_view,
    checked_spec,
    compare_cohort,
    create_cohort,
    record_asset,
    start_retest,
)
from app.intelligence.models import (
    CohortRetest,
    MeasurementCohort,
    MeasurementResult,
    PublishedAsset,
)


async def measurement_cohort(db, user, cohort_id):
    require_project_scope(user, user.get("pid", ""))
    if not settings.intelligence_enabled:
        raise HTTPException(503, "Citation intelligence is not enabled")
    row = await db.scalar(
        select(MeasurementCohort).where(
            MeasurementCohort.project_id == user["pid"],
            MeasurementCohort.id == cohort_id,
        )
    )
    if row is None:
        raise HTTPException(404, "Cohort not found")
    return row


def cohort_view(row):
    spec = checked_spec(row)
    return {
        "id": row.id,
        "title": row.title,
        "baseline_run_id": row.baseline_run_id,
        "created_at": utc_isoformat(row.created_at),
        "expected_samples": len(spec["slots"]),
        "platforms": sorted({slot["platform"] for slot in spec["slots"]}),
        "entities": spec["entities"],
        "metric": spec["metric"],
        "minimum_prompt_clusters": spec["min_prompts"],
        "minimum_rounds": spec["min_rounds"],
        "specification_hash": row.specification_hash,
    }


@router.get("/projects/{project_id}/cohorts")
async def list_cohorts(
    project_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_project_scope(user, project_id)
    rows = (
        await db.scalars(
            select(MeasurementCohort)
            .where(MeasurementCohort.project_id == project_id)
            .order_by(MeasurementCohort.created_at.desc())
            .limit(50)
        )
    ).all()
    return {"cohorts": [cohort_view(row) for row in rows]}


@router.post("/projects/{project_id}/cohorts", status_code=202)
async def freeze_cohort(
    project_id: str,
    data: CohortRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_project_scope(user, project_id)
    action_writer(user)
    row = await create_cohort(db, project_id, data, str(user["sub"]))
    await db.commit()
    return cohort_view(row)


@router.post("/cohorts/{cohort_id}/retests", status_code=202)
async def retest_cohort(
    cohort_id: str,
    data: RetestRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cohort = await measurement_cohort(db, user, cohort_id)
    action_writer(user)
    row = await start_retest(db, cohort, data, str(user["sub"]))
    await db.commit()
    return {
        "id": row.id,
        "audit_id": str(row.audit_id),
        "expected_samples": len(cohort.specification["slots"]),
        "cohort_id": cohort.id,
    }


@router.get("/cohorts/{cohort_id}/retests")
async def list_retests(
    cohort_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cohort = await measurement_cohort(db, user, cohort_id)
    rows = (
        await db.scalars(
            select(CohortRetest)
            .where(
                CohortRetest.project_id == cohort.project_id,
                CohortRetest.cohort_id == cohort.id,
            )
            .order_by(CohortRetest.created_at.desc())
            .limit(50)
        )
    ).all()
    items = []
    for row in rows:
        state = await db.scalar(
            select(AuditIntelligenceState).where(
                AuditIntelligenceState.project_id == cohort.project_id,
                AuditIntelligenceState.audit_id == row.audit_id,
            )
        )
        audit = await get_audit_for_project(db, user, row.audit_id)
        items.append(
            {
                "id": row.id,
                "audit_id": str(row.audit_id),
                "capture_status": audit.status.value,
                "published_run_id": state.published_run_id if state else None,
            }
        )
    return {"retests": items}


@router.post("/cohorts/{cohort_id}/results", status_code=202)
async def measure_cohort(
    cohort_id: str,
    data: CompareRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cohort = await measurement_cohort(db, user, cohort_id)
    action_writer(user)
    row = await compare_cohort(db, cohort, data, str(user["sub"]))
    await db.commit()
    return {
        "id": row.id,
        "comparison_signature": row.comparison_signature,
        **row.result,
    }


@router.get("/cohorts/{cohort_id}/results")
async def measurement_results(
    cohort_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cohort = await measurement_cohort(db, user, cohort_id)
    rows = (
        await db.scalars(
            select(MeasurementResult)
            .where(
                MeasurementResult.project_id == cohort.project_id,
                MeasurementResult.cohort_id == cohort.id,
            )
            .order_by(MeasurementResult.created_at.desc())
            .limit(10)
        )
    ).all()
    return {
        "results": [
            {
                "id": row.id,
                "comparison_signature": row.comparison_signature,
                **row.result,
            }
            for row in rows
        ]
    }


@router.post("/projects/{project_id}/assets", status_code=202)
async def register_asset(
    project_id: str,
    data: AssetRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_project_scope(user, project_id)
    action_writer(user)
    row = await record_asset(db, project_id, data, str(user["sub"]))
    await db.commit()
    return asset_view(row)


@router.get("/projects/{project_id}/assets")
async def list_assets(
    project_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_project_scope(user, project_id)
    rows = (
        await db.scalars(
            select(PublishedAsset)
            .where(PublishedAsset.project_id == project_id)
            .order_by(PublishedAsset.created_at.desc())
            .limit(50)
        )
    ).all()
    return {"assets": [asset_view(row) for row in rows]}


from app.intelligence.drafts import DraftRequest, authorize_draft, export_brief
from app.intelligence.models import DraftApproval


@router.post("/actions/{action_id}/draft-approvals", status_code=202)
async def approve_content_draft(
    action_id: str,
    data: DraftRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_project_scope(user, user.get("pid", ""))
    action_writer(user)
    if user.get("role") not in ("owner", "admin", "editor"):
        raise HTTPException(403, "Content editor approval required")
    action = await db.scalar(
        select(ActionPlan).where(
            ActionPlan.project_id == user["pid"], ActionPlan.id == action_id
        )
    )
    if action is None:
        raise HTTPException(404, "Action not found")
    run = await action_run(db, user, action.run_id)
    grant = await authorize_draft(db, action, run, data, str(user["sub"]))
    await db.commit()
    return {
        "approval_id": grant.id,
        "version": grant.version,
        "scope": "content_draft",
        "publication_allowed": False,
    }


@router.get("/draft-approvals/{approval_id}/brief")
async def evidence_brief(
    approval_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_project_scope(user, user.get("pid", ""))
    if not settings.intelligence_enabled:
        raise HTTPException(503, "Citation intelligence is not enabled")
    grant = await db.scalar(
        select(DraftApproval).where(
            DraftApproval.project_id == user["pid"], DraftApproval.id == approval_id
        )
    )
    if grant is None:
        raise HTTPException(404, "Draft approval not found")
    action = await db.scalar(
        select(ActionPlan).where(
            ActionPlan.project_id == user["pid"], ActionPlan.id == grant.action_id
        )
    )
    run = await action_run(db, user, action.run_id)
    return await export_brief(db, grant, action, run)


from app.intelligence.models import BrandFact, ClaimReview
from app.intelligence.reviews import (
    FactRequest,
    ReviewRequest,
    WithdrawRequest,
    approve_fact,
    fact_snapshot,
    fact_view,
    review_claim,
    review_snapshot,
    withdraw_fact,
)


@router.post("/runs/{run_id}/evidence/{evidence_id}/review", status_code=202)
async def review_evidence(
    run_id: str,
    evidence_id: str,
    data: ReviewRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    run = await action_run(db, user, run_id)
    action_writer(user)
    if run.status not in ("ready", "partial"):
        raise HTTPException(409, "Wait for published evidence")
    review = await review_claim(db, run, evidence_id, data, str(user["sub"]))
    await db.commit()
    return {"id": review.id, "version": review.version, "relation": review.relation}


@router.post("/runs/{run_id}/facts", status_code=202)
async def create_fact(
    run_id: str,
    data: FactRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    run = await action_run(db, user, run_id)
    action_writer(user)
    fact = await approve_fact(db, run, data, str(user["sub"]))
    await db.commit()
    return await fact_view(db, fact)


@router.get("/projects/{project_id}/facts")
async def list_facts(
    project_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_project_scope(user, project_id)
    snapshot = await fact_snapshot(db, project_id)
    rows = []
    for item in snapshot.values():
        fact = await db.scalar(
            select(BrandFact).where(
                BrandFact.project_id == project_id, BrandFact.id == item["id"]
            )
        )
        rows.append(await fact_view(db, fact))
    return {"facts": rows}


@router.post("/facts/{fact_id}/withdraw", status_code=202)
async def retract_fact(
    fact_id: str,
    data: WithdrawRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_project_scope(user, user.get("pid", ""))
    action_writer(user)
    fact = await db.scalar(
        select(BrandFact).where(
            BrandFact.project_id == user["pid"], BrandFact.id == fact_id
        )
    )
    if fact is None:
        raise HTTPException(404, "Fact not found")
    review = await db.scalar(
        select(ClaimReview).where(
            ClaimReview.project_id == fact.project_id, ClaimReview.id == fact.review_id
        )
    )
    run = await action_run(db, user, review.run_id)
    new = await withdraw_fact(db, run, fact, data, str(user["sub"]))
    await db.commit()
    return await fact_view(db, new)


from app.intelligence.actions import (
    ApprovalRequest,
    PlanRequest,
    approve_plan,
    create_plan,
    plan_view,
)
from app.intelligence.models import ActionPlan


async def action_run(db, user, run_id):
    require_project_scope(user, user.get("pid", ""))
    run = await db.scalar(
        select(IntelligenceRun).where(
            IntelligenceRun.project_id == user["pid"], IntelligenceRun.id == run_id
        )
    )
    if run is None:
        raise HTTPException(404, "Run not found")
    return run


def action_writer(user):
    require_writer(user)
    if not settings.intelligence_enabled:
        raise HTTPException(503, "Citation intelligence is not enabled")
    if not user.get("sub"):
        raise HTTPException(403, "Named reviewer required")


@router.get("/runs/{run_id}/actions")
async def list_action_plans(
    run_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    run = await action_run(db, user, run_id)
    actions = (
        await db.scalars(
            select(ActionPlan)
            .where(ActionPlan.project_id == run.project_id, ActionPlan.run_id == run.id)
            .order_by(ActionPlan.created_at)
        )
    ).all()
    return {"actions": [await plan_view(db, action, run) for action in actions]}


@router.post("/runs/{run_id}/actions", status_code=202)
async def start_action_plan(
    run_id: str,
    data: PlanRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    run = await action_run(db, user, run_id)
    action_writer(user)
    if run.status not in ("ready", "partial"):
        raise HTTPException(409, "Wait for published analysis")
    action = await create_plan(db, run, data, str(user["sub"]))
    await db.commit()
    return await plan_view(db, action, run)


@router.post("/actions/{action_id}/approve", status_code=202)
async def approve_action_plan(
    action_id: str,
    data: ApprovalRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_project_scope(user, user.get("pid", ""))
    action_writer(user)
    action = await db.scalar(
        select(ActionPlan).where(
            ActionPlan.project_id == user["pid"], ActionPlan.id == action_id
        )
    )
    if action is None:
        raise HTTPException(404, "Action not found")
    run = await action_run(db, user, action.run_id)
    await approve_plan(db, action, run, data, str(user["sub"]), user["role"])
    await db.commit()
    return await plan_view(db, action, run)


from app.intelligence.identity import (
    IdentityDecision,
    decide_identity,
    identity_snapshot,
)


@router.post("/runs/{run_id}/identity-decisions", status_code=202)
async def review_identity(
    run_id: str,
    data: IdentityDecision,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_project_scope(user, user.get("pid", ""))
    require_writer(user)
    if not settings.intelligence_enabled:
        raise HTTPException(503, "Citation intelligence is not enabled")
    if not user.get("sub"):
        raise HTTPException(403, "Named reviewer required")
    run = await db.scalar(
        select(IntelligenceRun).where(
            IntelligenceRun.project_id == user["pid"], IntelligenceRun.id == run_id
        )
    )
    if run is None:
        raise HTTPException(404, "Run not found")
    if run.status not in ("ready", "partial"):
        raise HTTPException(409, "Wait for analysis before reviewing identity")
    event = await decide_identity(db, run, data, str(user["sub"]))
    await db.commit()
    return {
        "event_id": event.id,
        "version": event.version,
        "decision": event.decision,
        "refresh_pending": True,
    }


@router.post("/sources/{document_id}/retry", status_code=202)
async def retry_source(
    document_id: str,
    data: SourceRetry,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.intelligence.refresh import state_for_audit
    from app.intelligence.semantics import stable_hash

    audit = await get_audit_for_project(db, user, data.audit_id)
    require_writer(user)
    if not settings.intelligence_enabled:
        raise HTTPException(503, "Citation intelligence is not enabled")
    await state_for_audit(db, audit.project_id, audit.id)
    observation = await db.scalar(
        select(CitationObservation.id)
        .where(
            CitationObservation.project_id == audit.project_id,
            CitationObservation.audit_id == audit.id,
            CitationObservation.document_id == document_id,
        )
        .limit(1)
    )
    if observation is None:
        raise HTTPException(404, "Source is not an observation of this audit")
    key = stable_hash(["source_retry", audit.id, document_id, data.idempotency_key])
    job = await db.scalar(
        select(AnalysisJob).where(
            AnalysisJob.project_id == audit.project_id,
            AnalysisJob.idempotency_hash == key,
        )
    )
    if job is not None and job.kind == "source_retry_receipt":
        job = await db.scalar(
            select(AnalysisJob).where(
                AnalysisJob.project_id == audit.project_id,
                AnalysisJob.id == job.payload["job_id"],
            )
        )
    if job is None:
        active = (
            await db.scalars(
                select(AnalysisJob)
                .where(
                    AnalysisJob.project_id == audit.project_id,
                    AnalysisJob.kind == "fetch_document",
                    AnalysisJob.subject_id == document_id,
                    AnalysisJob.status.in_(["queued", "leased", "retry_wait"]),
                )
                .with_for_update()
            )
        ).all()
        job = next(
            (row for row in active if row.payload.get("audit_id") == audit.id), None
        )
        if job is not None:
            db.add(
                AnalysisJob(
                    project_id=audit.project_id,
                    kind="source_retry_receipt",
                    subject_id=document_id,
                    payload={"audit_id": audit.id, "job_id": job.id},
                    idempotency_hash=key,
                    status="succeeded",
                )
            )
    if job is None:
        job = AnalysisJob(
            project_id=audit.project_id,
            kind="fetch_document",
            subject_id=document_id,
            payload={"audit_id": audit.id},
            idempotency_hash=key,
        )
        db.add(job)
    await db.commit()
    return {"job_id": job.id, "status": job.status}


@router.post("/projects/{project_id}/audits", status_code=202)
async def start_sampling(
    project_id: str,
    data: SamplingStart,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_project_scope(user, project_id)
    require_writer(user)
    if not settings.intelligence_enabled:
        raise HTTPException(503, "Citation intelligence is not enabled")
    audit, count = await create_sampling_audit(
        db, project_id, data.sampling, [brand.model_dump() for brand in data.brands]
    )
    await db.commit()
    return {
        "audit_id": str(audit.id),
        "expected_samples": count,
        "status": audit.status.value,
    }


@router.get("/audits/{audit_id}/capture")
async def capture_status(
    audit_id: int,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    audit = await get_audit_for_project(db, user, audit_id)
    config = await db.scalar(
        select(SamplingAudit).where(
            SamplingAudit.project_id == audit.project_id,
            SamplingAudit.audit_id == audit_id,
        )
    )
    if config is None:
        raise HTTPException(404, "Sampling audit not found")
    samples = (
        await db.scalars(
            select(AuditSample).where(
                AuditSample.project_id == audit.project_id,
                AuditSample.audit_id == audit_id,
            )
        )
    ).all()
    attempts = (
        await db.scalars(
            select(CaptureAttempt).where(
                CaptureAttempt.project_id == audit.project_id,
                CaptureAttempt.sample_id.in_([sample.id for sample in samples]),
            )
        )
    ).all()
    state = await db.scalar(
        select(AuditIntelligenceState).where(
            AuditIntelligenceState.project_id == audit.project_id,
            AuditIntelligenceState.audit_id == audit_id,
        )
    )
    source_pending = await db.scalar(
        select(func.count(AnalysisJob.id)).where(
            AnalysisJob.project_id == audit.project_id,
            AnalysisJob.kind == "fetch_document",
            AnalysisJob.payload["audit_id"].as_integer() == audit_id,
            AnalysisJob.status.in_(["queued", "leased", "retry_wait"]),
        )
    )
    analysis_pending = await db.scalar(
        select(func.count(AnalysisJob.id))
        .join(IntelligenceRun, IntelligenceRun.id == AnalysisJob.run_id)
        .where(
            AnalysisJob.project_id == audit.project_id,
            IntelligenceRun.project_id == audit.project_id,
            IntelligenceRun.audit_id == audit_id,
            AnalysisJob.kind == "analyze_run",
            AnalysisJob.status.in_(["queued", "leased", "retry_wait"]),
        )
    )
    latest_run = await db.scalar(
        select(IntelligenceRun)
        .where(
            IntelligenceRun.project_id == audit.project_id,
            IntelligenceRun.audit_id == audit_id,
        )
        .order_by(IntelligenceRun.revision.desc())
        .limit(1)
    )
    refresh_job = (
        await db.get(AnalysisJob, state.pending_job_id)
        if state and state.pending_job_id
        else None
    )
    now = utcnow()
    refresh_waiting = bool(
        refresh_job
        and refresh_job.status in ("queued", "retry_wait")
        and refresh_job.available_at > now
    )
    source_failures = await db.scalar(
        select(func.count(func.distinct(SourceFetch.document_id))).where(
            SourceFetch.project_id == audit.project_id,
            SourceFetch.audit_id == audit_id,
            SourceFetch.snapshot_id.is_(None),
            SourceFetch.document_id.not_in(
                select(SourceFetch.document_id).where(
                    SourceFetch.project_id == audit.project_id,
                    SourceFetch.audit_id == audit_id,
                    SourceFetch.snapshot_id.is_not(None),
                )
            ),
        )
    )
    unresolved = await db.scalar(
        select(func.count(CitationObservation.id)).where(
            CitationObservation.project_id == audit.project_id,
            CitationObservation.audit_id == audit_id,
            CitationObservation.url_resolution != "exact",
        )
    )
    return {
        "audit_id": str(audit_id),
        "analysis_status": latest_run.status if latest_run else None,
        "semantic_error_count": (latest_run.coverage or {}).get(
            "semantic_error_count", 0
        )
        if latest_run
        else 0,
        "source_failed_documents": source_failures or 0,
        "source_unresolved": unresolved or 0,
        "refresh_waiting": refresh_waiting,
        "refresh_status": refresh_job.status if refresh_job else None,
        "refresh_available_at": utc_isoformat(refresh_job.available_at)
        if refresh_waiting
        else None,
        "refresh_wait_reason": refresh_job.payload.get("waiting_reason")
        if refresh_waiting
        else None,
        "analysis_pending": analysis_pending or 0,
        "source_pending": source_pending or 0,
        "status": audit.status.value,
        "expected_samples": config.expected_samples,
        "sealed": sum(sample.status == "sealed" for sample in samples),
        "failed": sum(sample.status == "failed" for sample in samples),
        "published_run_id": state.published_run_id if state else None,
        "refresh_pending": bool(
            state and state.input_revision > state.handled_revision
        ),
        "samples": [
            {
                "id": sample.id,
                "platform": sample.platform,
                "sample_index": sample.sample_index,
                "prompt_version_id": sample.prompt_version_id,
                "status": sample.status,
                "attempt_count": sum(
                    attempt.sample_id == sample.id for attempt in attempts
                ),
            }
            for sample in samples
        ],
    }


def require_writer(user: dict):
    if user.get("role") not in ("owner", "admin", "editor", "member"):
        raise HTTPException(403, "Project write permission required")


def view(run):
    return {
        "id": run.id,
        "audit_id": str(run.audit_id),
        "revision": run.revision,
        "status": run.status,
        "created_at": utc_isoformat(run.created_at),
        "finished_at": utc_isoformat(run.finished_at),
        "signature": run.signature,
        "input_hash": run.input_hash,
        "coverage": run.coverage,
        "summary": run.summary,
        "sources": run.manifest.get("sources", []),
        "responses": [
            {
                key: value
                for key, value in item.items()
                if key not in ("text", "conditions", "search_metadata")
            }
            for item in run.manifest.get("responses", [])
        ],
    }


@router.get("/audits/{audit_id}/report")
async def intelligence_report(
    audit_id: int,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.intelligence.reporting import audit_report

    audit = await get_audit_for_project(db, user, audit_id)
    if not settings.intelligence_enabled:
        raise HTTPException(503, "Citation intelligence is not enabled")
    return await audit_report(db, audit)


@router.get("/audits/{audit_id}/report/pdf")
async def intelligence_report_pdf(
    audit_id: int,
    run_id: str = Query(pattern=r"^[a-f0-9]{32}$"),
    detail: str = Query("basic", pattern=r"^(basic|full)$"),
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from fastapi.responses import Response

    from app.intelligence.report_pdf import export_scope, render_report
    from app.intelligence.reporting import audit_report
    from app.intelligence.semantics import stable_hash

    audit = await get_audit_for_project(db, user, audit_id)
    if not settings.intelligence_enabled:
        raise HTTPException(503, "Citation intelligence is not enabled")
    report = await audit_report(db, audit)
    if (
        report.get("run_id") != run_id
        or not report.get("snapshot_current")
        or report["status"] not in ("ready", "partial")
    ):
        raise HTTPException(
            409, "Refresh the current completed analysis before exporting"
        )
    try:
        body = await render_report(report, detail)
    except ValueError as error:
        if str(error) == "report_too_large":
            raise HTTPException(
                422, "Report exceeds PDF size limit; no content was truncated"
            ) from error
        raise HTTPException(503, "PDF rendering failed") from None
    except Exception:
        # Do not put source paragraphs or renderer internals into error responses/logs.
        raise HTTPException(503, "PDF rendering failed") from None
    # This GET made no writes. End the read transaction, not just the ORM cache:
    # a repeatable-read snapshot could otherwise hide changes made during rendering.
    await db.rollback()
    current = await audit_report(db, await get_audit_for_project(db, user, audit_id))
    if (
        current.get("run_id") != run_id
        or not current.get("snapshot_current")
        or stable_hash(export_scope(report, detail))
        != stable_hash(export_scope(current, detail))
    ):
        raise HTTPException(
            409, "Analysis changed during export; refresh before retrying"
        )
    return Response(
        body,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="audit-{audit_id}-{run_id}.pdf"',
            "X-Report-Run-Id": run_id,
            "Cache-Control": "no-store",
        },
    )


@router.post("/audits/{audit_id}/runs", status_code=202)
async def create_run(
    audit_id: int,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    audit = await get_audit_for_project(db, user, audit_id)
    require_writer(user)
    if not settings.intelligence_enabled:
        raise HTTPException(503, "Citation intelligence is not enabled")
    if audit.status.value not in ("completed", "partial"):
        raise HTTPException(409, "Capture must finish before freezing analysis inputs")
    project_id = audit.project_id
    # End the authorization read snapshot before waiting for the audit lock.
    # MySQL REPEATABLE READ must see a concurrent retry/newer run after that
    # lock is acquired. Otherwise it can revive an obsolete failed revision.
    await db.rollback()
    run = await prepare_run(db, project_id, audit_id, retry_failed=True)
    await db.commit()
    return view(run)


@router.get("/projects/{project_id}/runs")
async def list_runs(
    project_id: str,
    audit_id: int | None = None,
    limit: int = Query(30, ge=1, le=100),
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_project_scope(user, project_id)
    query = select(IntelligenceRun).where(IntelligenceRun.project_id == project_id)
    if audit_id is not None:
        query = query.where(IntelligenceRun.audit_id == audit_id)
    rows = (
        await db.scalars(
            query.order_by(
                IntelligenceRun.created_at.desc(), IntelligenceRun.revision.desc()
            ).limit(limit)
        )
    ).all()
    return {
        "enabled": settings.intelligence_enabled,
        "runs": [
            {
                "id": run.id,
                "audit_id": str(run.audit_id),
                "revision": run.revision,
                "status": run.status,
                "created_at": run.created_at,
                "coverage": run.coverage,
            }
            for run in rows
        ],
    }


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_project_scope(user, user.get("pid", ""))
    run = await db.scalar(
        select(IntelligenceRun).where(
            IntelligenceRun.id == run_id, IntelligenceRun.project_id == user["pid"]
        )
    )
    if not run:
        raise HTTPException(404, "Run not found")
    result = view(run)
    reviews = await identity_snapshot(db, run.project_id)
    observed = {entity["id"] for entity in run.summary.get("entities", [])}
    result["identity_reviews"] = {
        key: value for key, value in reviews.items() if key in observed
    }
    result["claim_reviews"] = await review_snapshot(db, run.project_id, run.id)
    return result


@router.get("/runs/{run_id}/content")
async def content(
    run_id: str,
    owner_kind: str,
    owner_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_project_scope(user, user.get("pid", ""))
    run = await db.scalar(
        select(IntelligenceRun).where(
            IntelligenceRun.id == run_id, IntelligenceRun.project_id == user["pid"]
        )
    )
    if not run:
        raise HTTPException(404, "Run not found")
    permitted = {
        "answer": {item["id"] for item in run.manifest["responses"]},
        "citation": {item["id"] for item in run.manifest["sources"]},
        "snapshot": {
            item["snapshot_id"]
            for item in run.manifest["sources"]
            if item["snapshot_id"]
        },
    }
    if owner_id not in permitted.get(owner_kind, set()):
        raise HTTPException(404, "Content not in frozen run")
    rows = (
        await db.scalars(
            select(ContentSegment)
            .where(
                ContentSegment.project_id == run.project_id,
                ContentSegment.owner_kind == owner_kind,
                ContentSegment.owner_id == owner_id,
            )
            .order_by(ContentSegment.ordinal)
            .offset(offset)
            .limit(limit + 1)
        )
    ).all()
    return {
        "items": [
            {
                "id": row.id,
                "ordinal": row.ordinal,
                "start": row.start_offset,
                "end": row.end_offset,
                "text": row.text,
            }
            for row in rows[:limit]
        ],
        "next_offset": offset + limit if len(rows) > limit else None,
    }


@router.get("/projects/{project_id}/jobs")
async def jobs(
    project_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_project_scope(user, project_id)
    rows = (
        await db.scalars(
            select(AnalysisJob)
            .where(AnalysisJob.project_id == project_id)
            .order_by(AnalysisJob.created_at.desc())
            .limit(100)
        )
    ).all()
    return [
        {
            "id": row.id,
            "kind": row.kind,
            "status": row.status,
            "attempt": row.attempt,
            "max_attempts": row.max_attempts,
            "lease_until": row.lease_until,
            "error_code": row.error_code,
        }
        for row in rows
    ]


@router.get("/projects/{project_id}/operations")
async def intelligence_operations(
    project_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.intelligence.operations import operations_view

    require_project_scope(user, project_id)
    return await operations_view(db, project_id)


@router.get("/projects/{project_id}/content-workbench")
async def draft_material_inbox(
    project_id: str,
    cursor: str | None = Query(None, pattern=r"^[a-f0-9]{32}$"),
    approval_id: str | None = Query(None, pattern=r"^[a-f0-9]{32}$"),
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.intelligence.content_workbench import content_workbench

    require_project_scope(user, project_id)
    if cursor and approval_id:
        raise HTTPException(422, "Choose a page or an exact approval")
    return await content_workbench(db, project_id, cursor, approval_id)


@router.get("/projects/{project_id}/measurement-history")
async def saved_measurement_history(
    project_id: str,
    cohort_id: str | None = Query(None, pattern=r"^[a-f0-9]{32}$"),
    history_days: int = Query(365, ge=1, le=36500),
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.intelligence.measurement_history import measurement_history

    require_project_scope(user, project_id)
    return await measurement_history(
        db, project_id, cohort_id, utcnow() - timedelta(days=history_days)
    )
