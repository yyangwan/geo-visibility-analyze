"""Repair missing answers inside one frozen retest; never repeat a complete answer."""

import hashlib
import hmac
from copy import deepcopy

from fastapi import HTTPException
from pydantic import Field
from sqlalchemy import select

from app.config import settings
from app.intelligence.capture_contract import CaptureConditions, StrictFact
from app.intelligence.measurement import checked_spec
from app.intelligence.models import (
    AnalysisJob,
    AuditIntelligenceState,
    AuditSample,
    CaptureAttempt,
    CapturedResponse,
    CohortRetest,
    MeasurementCohort,
)
from app.intelligence.sampling import dispatch_attempt, refresh_event
from app.intelligence.semantics import stable_hash
from app.models.models import Audit, AuditStage, QueryStatus

RECEIPT_KIND = "retest_retry_receipt"
MAX_REPAIRS = 5


class RetryRequest(StrictFact):
    expected_retry_stamp: str = Field(
        alias="expectedRetryStamp", pattern=r"^[a-f0-9]{64}$"
    )
    idempotency_key: str = Field(
        alias="idempotencyKey", pattern=r"^[a-zA-Z0-9_-]{8,100}$"
    )


async def scoped_retest(db, cohort, retest_id):
    row = await db.scalar(
        select(CohortRetest).where(
            CohortRetest.project_id == cohort.project_id,
            CohortRetest.cohort_id == cohort.id,
            CohortRetest.id == retest_id,
        )
    )
    if row is None:
        raise HTTPException(404, "Retest not found in this cohort")
    return row


async def sample_states(db, cohort, retest, *, lock=False):
    query = (
        select(AuditSample)
        .where(
            AuditSample.project_id == cohort.project_id,
            AuditSample.audit_id == retest.audit_id,
        )
        .order_by(AuditSample.id)
    )
    samples = list(
        await db.scalars(
            query.with_for_update().execution_options(populate_existing=True)
            if lock
            else query
        )
    )
    ids = [row.id for row in samples]
    attempt_query = (
        select(CaptureAttempt)
        .where(
            CaptureAttempt.project_id == cohort.project_id,
            CaptureAttempt.sample_id.in_(ids),
        )
        .order_by(CaptureAttempt.sample_id, CaptureAttempt.attempt_no.desc())
    )
    attempts = list(
        await db.scalars(
            attempt_query.with_for_update().execution_options(populate_existing=True)
            if lock
            else attempt_query
        )
    )
    latest = {}
    for row in attempts:
        latest.setdefault(row.sample_id, row)
    selected = [row.selected_response_id for row in samples if row.selected_response_id]
    selected_query = select(
        CapturedResponse.id, CapturedResponse.payload["answer"]["status"].as_string()
    ).where(
        CapturedResponse.project_id == cohort.project_id,
        CapturedResponse.id.in_(selected),
    )
    statuses = dict(
        (
            await db.execute(
                selected_query.with_for_update() if lock else selected_query
            )
        ).all()
    )
    states = [
        {
            "sample": row,
            "attempt": latest.get(row.id),
            "answer_status": statuses.get(row.selected_response_id),
        }
        for row in samples
    ]
    return states


def retryable(item):
    sample = item["sample"]
    return (
        sample.status == "failed"
        or sample.status == "sealed"
        and item["answer_status"] == "partial"
    )


async def retest_state(db, cohort, retest, *, states=None, lock=False):
    spec = checked_spec(cohort)
    states = states if states is not None else await sample_states(db, cohort, retest)
    batch_query = select(AnalysisJob.id).where(
        AnalysisJob.project_id == cohort.project_id,
        AnalysisJob.kind == RECEIPT_KIND,
        AnalysisJob.subject_id == retest.id,
    )
    batches = len(
        list(await db.scalars(batch_query.with_for_update() if lock else batch_query))
    )
    audit = await db.scalar(
        select(Audit).where(
            Audit.project_id == cohort.project_id, Audit.id == retest.audit_id
        )
    )
    state = await db.scalar(
        select(AuditIntelligenceState).where(
            AuditIntelligenceState.project_id == cohort.project_id,
            AuditIntelligenceState.audit_id == retest.audit_id,
        )
    )
    pending = sum(item["sample"].status == "pending" for item in states)
    missing = sum(retryable(item) for item in states)
    complete = sum(
        item["sample"].status == "sealed" and item["answer_status"] == "complete"
        for item in states
    )
    planned = {
        (
            row["prompt_version_id"],
            row["platform"],
            row["surface"],
            row["sample_index"],
            stable_hash(
                CaptureConditions.model_validate(
                    row["requested_conditions"]
                ).model_dump(by_alias=True)
            ),
        )
        for row in spec["slots"]
    }
    actual = [
        (
            item["sample"].prompt_version_id,
            item["sample"].platform,
            item["sample"].surface,
            item["sample"].sample_index,
            stable_hash(item["sample"].conditions),
        )
        for item in states
    ]
    scope_valid = len(actual) == len(planned) and set(actual) == planned
    stamp = stable_hash(
        [
            cohort.specification_hash,
            retest.id,
            batches,
            [
                [
                    item["sample"].id,
                    item["sample"].status,
                    item["sample"].selected_response_id,
                    item["answer_status"],
                    item["attempt"].attempt_no if item["attempt"] else 0,
                ]
                for item in states
            ],
        ]
    )
    platforms = []
    for platform in sorted({slot["platform"] for slot in spec["slots"]}):
        rows = [item for item in states if item["sample"].platform == platform]
        platforms.append(
            {
                "platform": platform,
                "expected": sum(slot["platform"] == platform for slot in spec["slots"]),
                "complete": sum(
                    item["sample"].status == "sealed"
                    and item["answer_status"] == "complete"
                    for item in rows
                ),
                "pending": sum(item["sample"].status == "pending" for item in rows),
                "missing": sum(retryable(item) for item in rows),
            }
        )
    analysis_current = bool(
        state
        and state.published_run_id
        and state.input_revision == state.handled_revision
        and state.pending_job_id is None
    )
    return {
        "id": retest.id,
        "cohort_id": cohort.id,
        "audit_id": str(retest.audit_id),
        "capture_status": audit.status.value if audit else "unknown",
        "sample_scope_valid": scope_valid,
        "expected_samples": len(spec["slots"]),
        "complete_samples": complete,
        "pending_samples": pending,
        "missing_samples": missing,
        "platforms": platforms,
        "published_run_id": state.published_run_id if analysis_current else None,
        "analysis_pending": not analysis_current
        and bool(state and state.input_revision > state.handled_revision),
        "retry_stamp": stamp,
        "can_retry": bool(
            scope_valid and missing and not pending and batches < MAX_REPAIRS
        ),
        "retry_limit_reached": batches >= MAX_REPAIRS,
    }


def receipt_signature(payload):
    return hmac.new(
        settings.secret_key.encode(),
        stable_hash(
            {key: value for key, value in payload.items() if key != "signature"}
        ).encode(),
        hashlib.sha256,
    ).hexdigest()


async def repair_retest(db, cohort, retest_id, data, actor):
    cohort = await db.scalar(
        select(MeasurementCohort)
        .where(
            MeasurementCohort.project_id == cohort.project_id,
            MeasurementCohort.id == cohort.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    checked_spec(cohort)
    retest = await scoped_retest(db, cohort, retest_id)
    key = stable_hash([RECEIPT_KIND, cohort.id, retest.id, data.idempotency_key])
    request_hash = stable_hash([actor, data.model_dump(mode="json", by_alias=True)])
    old = await db.scalar(
        select(AnalysisJob)
        .where(
            AnalysisJob.project_id == cohort.project_id,
            AnalysisJob.idempotency_hash == key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if old:
        if (
            old.kind != RECEIPT_KIND
            or old.payload.get("request_hash") != request_hash
            or not hmac.compare_digest(
                old.payload.get("signature", ""), receipt_signature(old.payload)
            )
        ):
            raise HTTPException(409, "Repair operation key conflict or receipt changed")
        return {**deepcopy(old.payload["receipt"]), "replayed": True}
    states = await sample_states(db, cohort, retest, lock=True)
    # Capture acceptance locks samples before audit; preserve that order.
    audit = await db.scalar(
        select(Audit)
        .where(Audit.project_id == cohort.project_id, Audit.id == retest.audit_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    current = await retest_state(db, cohort, retest, states=states, lock=True)
    if current["retry_stamp"] != data.expected_retry_stamp:
        raise HTTPException(
            409, "Retest changed; refresh before repairing missing answers"
        )
    if not current["can_retry"] or audit is None:
        raise HTTPException(
            409, "No repairable terminal answers or repair budget exhausted"
        )
    repaired = []
    for item in states:
        if not retryable(item):
            continue
        sample = item["sample"]
        attempt_no = item["attempt"].attempt_no + 1 if item["attempt"] else 1
        sample.status, sample.selected_response_id = "pending", None
        await dispatch_attempt(db, sample, attempt_no)
        repaired.append(
            {
                "sample_id": sample.id,
                "attempt_no": attempt_no,
                "platform": sample.platform,
            }
        )
    audit.status, audit.stage, audit.completed_at = (
        QueryStatus.RUNNING,
        AuditStage.QUERYING,
        None,
    )
    receipt = {
        "cohort_id": cohort.id,
        "retest_id": retest.id,
        "audit_id": str(retest.audit_id),
        "repaired_samples": repaired,
        "new_independent_rounds": 0,
    }
    payload = {"request_hash": request_hash, "actor_id": actor, "receipt": receipt}
    payload["signature"] = receipt_signature(payload)
    db.add(
        AnalysisJob(
            project_id=cohort.project_id,
            run_id=None,
            kind=RECEIPT_KIND,
            subject_id=retest.id,
            payload=payload,
            idempotency_hash=key,
            status="completed",
            max_attempts=1,
        )
    )
    await refresh_event(db, cohort.project_id, retest.audit_id, "retest_repair", key)
    await db.flush()
    return {**receipt, "replayed": False}
