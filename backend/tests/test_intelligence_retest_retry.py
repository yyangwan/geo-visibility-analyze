"""Missing-answer repair retains complete facts, selected sample scope and exact replay."""

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.intelligence.blob import digest
from app.intelligence.measurement import RetestRequest, create_cohort, start_retest
from app.intelligence.models import AuditSample, CaptureAttempt, CapturedResponse
from app.intelligence.retest_retry import (
    MAX_REPAIRS,
    RetryRequest,
    repair_retest,
    retest_state,
)
from app.intelligence.semantics import stable_hash
from app.models.device_gateway import DeviceTask
from app.models.models import Audit, AuditStage, QueryStatus
from app.utils.timezone import utcnow
from tests.test_intelligence_measurement import CONDITIONS, fixture_cohort


async def fixture_retest(db, *, partial=False, project="p1"):
    _original, baseline, version, data = await fixture_cohort(db, project)
    db.add(
        AuditSample(
            project_id=project,
            audit_id=baseline.audit_id,
            prompt_version_id=version.id,
            platform="doubao",
            surface="app",
            sample_index=1,
            conditions=CONDITIONS,
            condition_hash=stable_hash(CONDITIONS),
            status="sealed",
        )
    )
    await db.flush()
    cohort = await create_cohort(
        db,
        project,
        data.model_copy(update={"idempotency_key": "repair-two-platform-plan"}),
        "owner",
    )
    retest = await start_retest(
        db, cohort, RetestRequest(idempotencyKey="repair-retake-key"), "owner"
    )
    samples = list(
        await db.scalars(
            select(AuditSample)
            .where(AuditSample.audit_id == retest.audit_id)
            .order_by(AuditSample.platform)
        )
    )
    by_platform = {row.platform: row for row in samples}
    for platform, sample in by_platform.items():
        attempt = await db.scalar(
            select(CaptureAttempt).where(CaptureAttempt.sample_id == sample.id)
        )
        task = await db.get(DeviceTask, attempt.device_task_id)
        if platform == "qwen" or partial:
            status = "complete" if platform == "qwen" else "partial"
            text = "完整原回答" if status == "complete" else "尚未完整返回的原回答"
            response = CapturedResponse(
                project_id=project,
                sample_id=sample.id,
                attempt_no=1,
                text=text,
                content_hash=digest(text.encode()),
                observed_at=utcnow(),
                payload={
                    "answer": {"status": status, "text": text},
                    "sourcePanel": {"status": "partial"},
                },
            )
            db.add(response)
            await db.flush()
            sample.status, sample.selected_response_id = "sealed", response.id
            attempt.status, attempt.response_id = "accepted", response.id
            task.status = "completed"
        else:
            sample.status, attempt.status, task.status = "failed", "failed", "failed"
    audit = await db.get(Audit, retest.audit_id)
    audit.status, audit.stage = QueryStatus.PARTIAL, AuditStage.PARTIAL
    await db.commit()
    return cohort, retest, by_platform


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_only_missing_or_partial_answers_get_new_attempts_without_new_rounds(
    db_session, partial
):
    cohort, retest, samples = await fixture_retest(db_session, partial=partial)
    before = await retest_state(db_session, cohort, retest)
    assert before["sample_scope_valid"] and before["can_retry"]
    assert before["complete_samples"] == 1 and before["missing_samples"] == 1
    complete_id = samples["qwen"].selected_response_id
    old_partial_id = samples["doubao"].selected_response_id
    data = RetryRequest(
        expectedRetryStamp=before["retry_stamp"], idempotencyKey="repair-missing-once"
    )
    receipt = await repair_retest(db_session, cohort, retest.id, data, "owner")
    await db_session.commit()
    replay = await repair_retest(db_session, cohort, retest.id, data, "owner")
    assert (
        replay["replayed"] and receipt["repaired_samples"] == replay["repaired_samples"]
    )
    assert (
        receipt["audit_id"] == str(retest.audit_id)
        and receipt["new_independent_rounds"] == 0
    )
    assert [row["sample_id"] for row in receipt["repaired_samples"]] == [
        samples["doubao"].id
    ]
    assert samples["qwen"].selected_response_id == complete_id
    assert samples["doubao"].selected_response_id is None
    assert (
        await db_session.scalar(select(func.count()).select_from(CaptureAttempt)) == 3
    )
    if old_partial_id:
        assert await db_session.get(CapturedResponse, old_partial_id) is not None
    after = await retest_state(db_session, cohort, retest)
    assert (
        after["pending_samples"] == 1
        and not after["can_retry"]
        and after["capture_status"] == "running"
    )
    with pytest.raises(HTTPException) as error:
        await repair_retest(
            db_session,
            cohort,
            retest.id,
            data.model_copy(update={"idempotency_key": "another-repair-key"}),
            "owner",
        )
    assert error.value.status_code == 409
    assert (
        await db_session.scalar(select(func.count()).select_from(CaptureAttempt)) == 3
    )


@pytest.mark.asyncio
async def test_changed_scope_wrong_retest_or_actor_does_not_replay_or_dispatch(
    db_session,
):
    cohort, retest, _samples = await fixture_retest(db_session)
    before = await retest_state(db_session, cohort, retest)
    data = RetryRequest(
        expectedRetryStamp=before["retry_stamp"], idempotencyKey="repair-scoped-key"
    )
    with pytest.raises(HTTPException) as error:
        await repair_retest(db_session, cohort, "f" * 32, data, "owner")
    assert error.value.status_code == 404
    await repair_retest(db_session, cohort, retest.id, data, "owner")
    with pytest.raises(HTTPException) as error:
        await repair_retest(db_session, cohort, retest.id, data, "another")
    assert error.value.status_code == 409
    assert (
        await db_session.scalar(select(func.count()).select_from(CaptureAttempt)) == 3
    )


@pytest.mark.asyncio
async def test_manual_repair_budget_is_bounded_and_complete_answer_never_recollected(
    db_session,
):
    cohort, retest, samples = await fixture_retest(db_session)
    for index in range(MAX_REPAIRS):
        current = await retest_state(db_session, cohort, retest)
        await repair_retest(
            db_session,
            cohort,
            retest.id,
            RetryRequest(
                expectedRetryStamp=current["retry_stamp"],
                idempotencyKey=f"repair-budget-{index}",
            ),
            "owner",
        )
        await db_session.commit()
        failed = samples["doubao"]
        failed.status = "failed"
        attempt = await db_session.scalar(
            select(CaptureAttempt)
            .where(CaptureAttempt.sample_id == failed.id)
            .order_by(CaptureAttempt.attempt_no.desc())
            .limit(1)
        )
        attempt.status = "failed"
        task = await db_session.get(DeviceTask, attempt.device_task_id)
        task.status = "failed"
        audit = await db_session.get(Audit, retest.audit_id)
        audit.status, audit.stage = QueryStatus.PARTIAL, AuditStage.PARTIAL
        await db_session.commit()
    final = await retest_state(db_session, cohort, retest)
    assert not final["can_retry"] and final["retry_limit_reached"]
    with pytest.raises(HTTPException) as error:
        await repair_retest(
            db_session,
            cohort,
            retest.id,
            RetryRequest(
                expectedRetryStamp=final["retry_stamp"],
                idempotencyKey="over-repair-budget",
            ),
            "owner",
        )
    assert error.value.status_code == 409
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(CaptureAttempt)
            .where(CaptureAttempt.sample_id == samples["qwen"].id)
        )
        == 1
    )
