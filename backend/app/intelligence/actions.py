"""Evidence-bound task plans and signed approvals; no publication side effects."""

import hashlib
import hmac
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.config import settings
from app.intelligence.aliases import alias_snapshot
from app.intelligence.identity import identity_snapshot
from app.intelligence.mention_bindings import binding_snapshot
from app.intelligence.models import (
    ActionApproval,
    ActionPlan,
    AnalysisJob,
    IntelligenceRun,
    PlatformDiagnosis,
)
from app.intelligence.project_context import context_snapshot
from app.intelligence.refresh import state_for_audit
from app.intelligence.semantics import analysis_signature, stable_hash


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    diagnosis_id: str = Field(alias="diagnosisId", pattern=r"^[a-f0-9]{32}$")
    idempotency_key: str = Field(
        alias="idempotencyKey", pattern=r"^[a-zA-Z0-9_-]{8,100}$"
    )


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    expected_revision: int = Field(alias="expectedRevision", ge=1)
    reason: str = Field(min_length=5, max_length=2000)
    idempotency_key: str = Field(
        alias="idempotencyKey", pattern=r"^[a-zA-Z0-9_-]{8,100}$"
    )
    scope: Literal["investigation_plan"]


async def persist_diagnoses(db, run, summary):
    for diagnosis in summary.get("diagnoses", []):
        if not diagnosis.get("id") or not diagnosis.get("action") and not diagnosis.get("task_key"):
            continue
        record = await db.scalar(
            select(PlatformDiagnosis).where(
                PlatformDiagnosis.project_id == run.project_id,
                PlatformDiagnosis.run_id == run.id,
                PlatformDiagnosis.diagnosis_hash == diagnosis["id"],
            )
        )
        if record is None:
            record = PlatformDiagnosis(
                project_id=run.project_id,
                run_id=run.id,
                diagnosis_hash=diagnosis["id"],
                payload=dict(diagnosis),
            )
            db.add(record)
            await db.flush()
        elif diagnosis.get("task_key"):
            frozen = {key: value for key, value in record.payload.items() if key != "record_id"}
            candidate = {key: value for key, value in diagnosis.items() if key != "record_id"}
            if stable_hash(frozen) != stable_hash(candidate):
                raise HTTPException(409, "Published opportunity is immutable; create a new Run revision")
        diagnosis["record_id"] = record.id


async def stamp(db, run, state):
    from app.intelligence.reviews import fact_snapshot, review_snapshot

    reviews = await review_snapshot(db, run.project_id, run.id)
    facts = await fact_snapshot(db, run.project_id)
    _, base_url, model = settings.get_analysis_llm_config()
    current_signature = analysis_signature(model, base_url)
    newest = await db.scalar(
        select(IntelligenceRun.id)
        .where(
            IntelligenceRun.project_id == run.project_id,
            IntelligenceRun.audit_id == run.audit_id,
            IntelligenceRun.status.in_(["ready", "partial"]),
        )
        .order_by(IntelligenceRun.revision.desc())
        .limit(1)
    )
    fresh = (
        newest == run.id
        and state.input_revision == state.handled_revision
        and run.signature == current_signature
    )
    fresh = fresh and await context_snapshot(db, run.project_id) == run.manifest.get(
        "brand_context"
    )
    identities = await identity_snapshot(db, run.project_id)
    fresh = fresh and identities == run.manifest.get("entity_resolutions", {})
    fresh = fresh and await binding_snapshot(db, run.project_id) == run.manifest.get(
        "mention_bindings", {}
    )
    fresh = fresh and await alias_snapshot(db, run.project_id) == run.manifest.get(
        "entity_aliases", {}
    )
    value = stable_hash(
        [
            run.id,
            run.input_hash,
            run.signature,
            current_signature,
            state.input_revision,
            newest,
            identities,
            reviews,
            facts,
        ]
    )
    return value, fresh


async def create_plan(db, run, data, actor):
    await state_for_audit(db, run.project_id, run.audit_id)
    diagnosis = await db.scalar(
        select(PlatformDiagnosis).where(
            PlatformDiagnosis.project_id == run.project_id,
            PlatformDiagnosis.run_id == run.id,
            PlatformDiagnosis.id == data.diagnosis_id,
        )
    )
    if diagnosis is None or not diagnosis.payload.get("action"):
        if diagnosis and diagnosis.payload.get("task_key"):
            raise HTTPException(409, "Opportunity is not actionable; inspect scope and task contract")
        raise HTTPException(404, "Diagnosis not in this run")
    if diagnosis.payload.get("task_key"):
        from pydantic import ValidationError

        from app.intelligence.value_contracts import TaskPackV2

        try:
            pack = TaskPackV2.model_validate(diagnosis.payload["task_pack"])
            valid = (pack.project_id == run.project_id and pack.task_key() == diagnosis.payload["task_key"]
                     and pack.revision_signature() == diagnosis.payload["task_signature"]
                     and all(ref.run_id == run.id for ref in pack.diagnostic_evidence))
        except (ValidationError, KeyError):
            valid = False
        if not valid:
            raise HTTPException(409, "Frozen task package integrity changed")
    previous = await db.scalar(
        select(ActionPlan)
        .where(
            ActionPlan.project_id == run.project_id,
            ActionPlan.idempotency_key == data.idempotency_key,
        )
        .with_for_update()
    )
    if previous:
        if previous.run_id != run.id or previous.diagnosis_id != diagnosis.id:
            raise HTTPException(409, "Action request key conflict")
        return previous
    existing = await db.scalar(
        select(ActionPlan)
        .where(
            ActionPlan.project_id == run.project_id,
            ActionPlan.run_id == run.id,
            ActionPlan.diagnosis_id == diagnosis.id,
        )
        .with_for_update()
    )
    receipt_key = stable_hash(["action_request", data.idempotency_key])
    receipt = await db.scalar(
        select(AnalysisJob).where(
            AnalysisJob.project_id == run.project_id,
            AnalysisJob.idempotency_hash == receipt_key,
        )
    )
    if receipt:
        if (
            receipt.payload["run_id"] != run.id
            or receipt.payload["diagnosis_id"] != diagnosis.id
        ):
            raise HTTPException(409, "Action request key conflict")
        return await db.scalar(
            select(ActionPlan).where(
                ActionPlan.project_id == run.project_id,
                ActionPlan.id == receipt.payload["action_id"],
            )
        )
    if existing:
        db.add(
            AnalysisJob(
                project_id=run.project_id,
                kind="action_request_receipt",
                subject_id=existing.id,
                status="succeeded",
                idempotency_hash=receipt_key,
                payload={
                    "run_id": run.id,
                    "diagnosis_id": diagnosis.id,
                    "action_id": existing.id,
                },
            )
        )
        await db.flush()
        return existing
    action = ActionPlan(
        project_id=run.project_id,
        run_id=run.id,
        diagnosis_id=diagnosis.id,
        title=diagnosis.payload["action"]["task"],
        target=dict(diagnosis.payload),
        actor_id=actor,
        idempotency_key=data.idempotency_key,
    )
    db.add(action)
    await db.flush()
    return action


def signature(action, approval):
    content = stable_hash(
        [
            action.id,
            action.revision,
            action.title,
            action.target,
            approval.input_stamp,
            approval.actor_id,
            approval.role,
            approval.reason,
            "investigation_plan",
        ]
    )
    return hmac.new(
        settings.secret_key.encode(), content.encode(), hashlib.sha256
    ).hexdigest()


async def approve_plan(db, action, run, data, actor, role):
    state = await state_for_audit(db, run.project_id, run.audit_id)
    action = await db.scalar(
        select(ActionPlan)
        .where(ActionPlan.project_id == run.project_id, ActionPlan.id == action.id)
        .with_for_update()
    )
    request_hash = stable_hash([action.id, data.model_dump(), actor, role])
    previous = await db.scalar(
        select(ActionApproval)
        .where(
            ActionApproval.project_id == run.project_id,
            ActionApproval.idempotency_key == data.idempotency_key,
        )
        .with_for_update()
    )
    if previous:
        if previous.request_hash != request_hash:
            raise HTTPException(409, "Approval request key conflict")
        return previous
    if action.revision != data.expected_revision or action.status != "proposed":
        raise HTTPException(409, "Action revision changed or already approved")
    value, fresh = await stamp(db, run, state)
    if not fresh:
        raise HTTPException(409, "Evidence changed; create a plan from the latest run")
    if len(data.reason.strip()) < 5:
        raise HTTPException(422, "Approval requires review rationale")
    approval = ActionApproval(
        project_id=run.project_id,
        action_id=action.id,
        revision=action.revision,
        input_stamp=value,
        signature="",
        actor_id=actor,
        role=role,
        reason=data.reason.strip(),
        idempotency_key=data.idempotency_key,
        request_hash=request_hash,
    )
    approval.signature = signature(action, approval)
    db.add(approval)
    action.status = "approved"
    await db.flush()
    return approval


async def plan_view(db, action, run, *, state=None):
    if state is None:
        state = await state_for_audit(db, run.project_id, run.audit_id)
    value, fresh = await stamp(db, run, state)
    approval = await db.scalar(
        select(ActionApproval).where(
            ActionApproval.project_id == run.project_id,
            ActionApproval.action_id == action.id,
            ActionApproval.revision == action.revision,
        )
    )
    valid = bool(
        approval
        and approval.input_stamp == value
        and hmac.compare_digest(approval.signature, signature(action, approval))
    )
    return {
        "id": action.id,
        "run_id": action.run_id,
        "revision": action.revision,
        "title": action.title,
        "target": action.target,
        "status": action.status if fresh and (not approval or valid) else "stale",
        "approval_scope": "investigation_plan",
        "approval_valid": valid and fresh,
        "publication_allowed": False,
    }
