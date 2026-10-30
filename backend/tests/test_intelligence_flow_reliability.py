import json
from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.config import settings
from app.intelligence.capture_contract import CaptureV2
from app.intelligence.dashboard import audit_processing
from app.intelligence.ingestion import prepare_run
from app.intelligence.models import (
    AnalysisJob,
    AuditIntelligenceState,
    AuditSample,
    CaptureAttempt,
    IntelligenceRun,
    SourceDocument,
    SourceFetch,
)
from app.intelligence.pipeline import Pipeline
from app.intelligence.queue import JobQueue
from app.intelligence.refresh import (
    recover_settled_refreshes,
    request_refresh,
    settled_inputs,
    state_for_audit,
)
from app.intelligence.semantics import (
    SemanticOutputError,
    checked_result,
    decode_semantic,
)
from app.models.device_gateway import DeviceTask
from app.models.models import QueryStatus
from app.services.device_gateway_service import claim_task, fail_task
from app.utils.timezone import utcnow
from tests.conftest import TestSessionLocal
from tests.test_intelligence_foundation import fixture_audit
from tests.test_intelligence_sampling import new_audit, result_for


def test_invalid_model_output_has_only_safe_field_diagnostics():
    secret = "private-model-text-and-token"
    with pytest.raises(SemanticOutputError) as raised:
        decode_semantic(
            "原文",
            json.dumps(
                {
                    "entities": [{"name": secret}],
                    "claims": [],
                    "topics": [],
                    "brand_signals": [],
                    "information_units": [],
                }
            ),
        )
    diagnostic = json.dumps(raised.value.fields)
    assert "entities" in diagnostic and secret not in diagnostic
    assert "input" not in diagnostic and "url" not in diagnostic
    for content in (None, "not json"):
        with pytest.raises(SemanticOutputError):
            decode_semantic("原文", content)
    assert (
        decode_semantic(
            "原文",
            '```json\n{"entities": [], "claims": [], "topics": [], "brand_signals": [], "information_units": []}\n```',
        )["entities"]
        == []
    )


@pytest.mark.asyncio
async def test_one_invalid_segment_does_not_abort_other_segments(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "analysis_llm_api_key", "fake")
    text = "甲" * 900 + "后续有效文本"
    audit = await fixture_audit(db_session, text=text)
    run = await prepare_run(db_session, "p1", audit.id)
    await db_session.commit()
    calls = []

    async def analyze(value, *_):
        calls.append(value)
        if value.startswith("甲"):
            return decode_semantic(value, '{"entities": "invalid-private-text"}'), {}
        return checked_result(value, {"topics": ["保留后续结果"]}), {}

    queue = JobQueue(TestSessionLocal, "worker")
    await Pipeline(queue, None, analyze=analyze).analyze_run(await queue.claim())
    await db_session.refresh(run)
    assert len(calls) == 2 and run.status == "partial"
    assert run.coverage["segment_states"] == {"failed": 1, "completed": 1}
    assert run.coverage["semantic_error_count"] == 1
    assert "invalid-private-text" not in json.dumps(run.coverage)
    state = await db_session.scalar(select(AuditIntelligenceState))
    assert state.published_run_id == run.id


@pytest.mark.asyncio
async def test_new_capture_attempt_retains_retry_delay(db_session):
    await new_audit(db_session, rounds=1)
    task, token = await claim_task(db_session, "CHAO", ["browser.prompt"])
    before = utcnow()
    await fail_task(db_session, "CHAO", task.id, token, "timeout", "timeout", True, 90)
    attempts = (
        await db_session.scalars(
            select(CaptureAttempt).order_by(CaptureAttempt.attempt_no)
        )
    ).all()
    replacement = await db_session.get(DeviceTask, attempts[-1].device_task_id)
    assert len(attempts) == 2 and replacement.available_at >= before + timedelta(
        seconds=89
    )
    assert await claim_task(db_session, "CHAO", ["browser.prompt"]) == (None, None)


@pytest.mark.asyncio
async def test_device_unavailable_waits_without_consuming_attempt_and_times_out(
    db_session,
):
    await new_audit(db_session, rounds=1)
    task, token = await claim_task(db_session, "CHAO", ["browser.prompt"])
    await fail_task(
        db_session,
        "CHAO",
        task.id,
        token,
        "gateway_device_unavailable",
        "busy",
        True,
        30,
    )
    assert task.status == "pending" and task.attempt_count == 0
    assert len((await db_session.scalars(select(CaptureAttempt))).all()) == 1
    task.available_at = utcnow() - timedelta(seconds=1)
    task.created_at = utcnow() - timedelta(hours=2)
    await db_session.commit()
    task, token = await claim_task(db_session, "CHAO", ["browser.prompt"])
    await fail_task(
        db_session,
        "CHAO",
        task.id,
        token,
        "gateway_device_unavailable",
        "busy",
        True,
        30,
    )
    assert task.status == "failed" and task.error_code == "gateway_device_wait_timeout"
    assert (await db_session.scalar(select(AuditSample))).status == "failed"
    assert len((await db_session.scalars(select(CaptureAttempt))).all()) == 1


@pytest.mark.asyncio
async def test_worker_terminates_resource_wait_even_if_gateway_goes_offline(db_session):
    from app.intelligence.sampling import reconcile_capture_failures

    await new_audit(db_session, rounds=1)
    task, token = await claim_task(db_session, "CHAO", ["browser.prompt"])
    await fail_task(
        db_session,
        "CHAO",
        task.id,
        token,
        "gateway_device_unavailable",
        "busy",
        True,
        30,
    )
    task.created_at = utcnow() - timedelta(hours=2)
    await db_session.commit()
    await reconcile_capture_failures(db_session)
    await db_session.flush()
    assert task.status == "failed" and task.error_code == "gateway_device_wait_timeout"
    assert (await db_session.scalar(select(AuditSample))).status == "failed"
    assert len((await db_session.scalars(select(CaptureAttempt))).all()) == 1


def test_progress_label_is_rejected_even_with_correct_hash():
    class Task:
        id, platform, surface = "task", "qwen", "app"

    with pytest.raises(ValidationError, match="UI progress"):
        CaptureV2.model_validate(result_for(Task(), "已完成思考，参考了10篇资料"))
    assert CaptureV2.model_validate(result_for(Task(), "已完成思考，以下是正文结论。"))


@pytest.mark.asyncio
async def test_existing_status_only_response_is_preserved_but_excluded(db_session):
    audit = await fixture_audit(db_session, text="已完成思考，参考了10篇资料")
    run = await prepare_run(db_session, "p1", audit.id)
    assert run.manifest["responses"][0]["error"] is True
    assert run.manifest["responses"][0]["text"] == "已完成思考，参考了10篇资料"


@pytest.mark.asyncio
async def test_final_capture_bypasses_quota_but_running_capture_does_not(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "intelligence_refresh_debounce_seconds", 0)
    audit = await fixture_audit(db_session)
    audit.status = QueryStatus.RUNNING
    state = await state_for_audit(db_session, "p1", audit.id)
    state.window_start, state.automatic_runs = (
        utcnow(),
        settings.intelligence_auto_refresh_limit,
    )
    await request_refresh(db_session, "p1", audit.id, "sample_sealed", "early")
    pending = await db_session.get(AnalysisJob, state.pending_job_id)
    assert pending.available_at > utcnow() + timedelta(hours=23)
    assert not await audit_processing(db_session, await fixture_audit(db_session))
    audit.status = QueryStatus.COMPLETED
    await request_refresh(db_session, "p1", audit.id, "capture_terminal", "last")
    assert pending.available_at <= utcnow()
    await db_session.commit()
    queue = JobQueue(TestSessionLocal, "worker")
    await Pipeline(queue, None).refresh_run(await queue.claim())
    await db_session.refresh(state)
    assert state.handled_revision == state.input_revision
    assert state.automatic_runs == settings.intelligence_auto_refresh_limit
    assert await db_session.scalar(
        select(IntelligenceRun.id).where(IntelligenceRun.audit_id == audit.id)
    )


@pytest.mark.asyncio
async def test_final_source_can_expedite_while_its_job_is_still_leased(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "intelligence_refresh_debounce_seconds", 0)
    audit = await fixture_audit(db_session)
    document = SourceDocument(
        project_id="p1", url="https://example.com/a", url_hash="d" * 64
    )
    db_session.add(document)
    await db_session.flush()
    fetch = AnalysisJob(
        project_id="p1",
        kind="fetch_document",
        subject_id=document.id,
        payload={"audit_id": audit.id},
        status="leased",
        idempotency_hash="f" * 64,
    )
    db_session.add(fetch)
    await db_session.flush()
    state = await state_for_audit(db_session, "p1", audit.id)
    state.window_start, state.automatic_runs = (
        utcnow(),
        settings.intelligence_auto_refresh_limit,
    )
    await request_refresh(db_session, "p1", audit.id, "sample_sealed", "one")
    pending = await db_session.get(AnalysisJob, state.pending_job_id)
    assert not await settled_inputs(db_session, "p1", audit.id)
    assert pending.available_at > utcnow()
    # A recorded 503 is a retry observation, not a terminal source event.
    db_session.add(
        SourceFetch(
            project_id="p1",
            document_id=document.id,
            audit_id=audit.id,
            job_id=fetch.id,
            status="http_error",
            http_status=503,
        )
    )
    await db_session.flush()
    assert not await settled_inputs(db_session, "p1", audit.id)
    db_session.add(
        SourceFetch(
            project_id="p1",
            document_id=document.id,
            audit_id=audit.id,
            job_id=fetch.id,
            status="blocked",
        )
    )
    await db_session.flush()
    await request_refresh(db_session, "p1", audit.id, "source_ready", "terminal")
    assert await settled_inputs(db_session, "p1", audit.id)
    assert pending.available_at <= utcnow()


@pytest.mark.asyncio
async def test_restart_recovers_existing_quota_wait_once(db_session):
    audit = await fixture_audit(db_session)
    audit.status = QueryStatus.RUNNING
    state = await state_for_audit(db_session, "p1", audit.id)
    state.window_start, state.automatic_runs = (
        utcnow(),
        settings.intelligence_auto_refresh_limit,
    )
    await request_refresh(db_session, "p1", audit.id, "sample_sealed", "one")
    audit.status = QueryStatus.PARTIAL
    await db_session.flush()
    assert await recover_settled_refreshes(db_session) == 1
    assert await recover_settled_refreshes(db_session) == 0
    pending = await db_session.get(AnalysisJob, state.pending_job_id)
    assert pending.available_at <= utcnow()
    assert await audit_processing(db_session, audit)
    pending.available_at = utcnow() + timedelta(hours=1)
    await db_session.flush()
    assert not await audit_processing(db_session, audit, due_only=True)
    assert await audit_processing(db_session, audit)


@pytest.mark.asyncio
async def test_editorial_changes_keep_enhancement_quota_after_capture_settles(
    db_session,
):
    audit = await fixture_audit(db_session)
    state = await state_for_audit(db_session, "p1", audit.id)
    state.window_start, state.automatic_runs = (
        utcnow(),
        settings.intelligence_auto_refresh_limit,
    )
    await request_refresh(
        db_session, "p1", audit.id, "approved_fact_changed", "editorial"
    )
    job = await db_session.get(AnalysisJob, state.pending_job_id)
    assert job.available_at > utcnow() + timedelta(hours=23)
    assert job.payload["waiting_reason"] == "enhancement_quota"
    assert await recover_settled_refreshes(db_session) == 0


@pytest.mark.asyncio
async def test_capture_status_reports_waits_failures_and_source_gaps(db_session):
    from app.api.intelligence import capture_status
    from app.intelligence.models import CitationObservation

    audit, _, _ = await new_audit(db_session, rounds=1)
    run = await prepare_run(db_session, "p1", audit.id)
    run.status, run.coverage = "failed", {"semantic_error_count": 2}
    state = await state_for_audit(db_session, "p1", audit.id)
    state.window_start, state.automatic_runs = (
        utcnow(),
        settings.intelligence_auto_refresh_limit,
    )
    await request_refresh(db_session, "p1", audit.id, "sample_sealed", "one")
    document = SourceDocument(
        project_id="p1", url="https://example.com/a", url_hash="d" * 64
    )
    db_session.add(document)
    await db_session.flush()
    db_session.add_all(
        [
            SourceFetch(
                project_id="p1",
                audit_id=audit.id,
                document_id=document.id,
                status="blocked",
            ),
            SourceFetch(
                project_id="p1",
                audit_id=audit.id,
                document_id=document.id,
                status="blocked",
            ),
            CitationObservation(
                project_id="p1",
                audit_id=audit.id,
                response_id="response",
                ordinal=1,
                platform="qwen",
                kind="unknown",
                url_resolution="unavailable",
            ),
        ]
    )
    await db_session.flush()
    value = await capture_status(
        audit.id, {"scope": "project", "pid": "p1"}, db_session
    )
    assert value["refresh_waiting"] is True
    assert value["refresh_wait_reason"] == "enhancement_quota"
    assert value["refresh_available_at"].endswith("Z")
    assert value["analysis_status"] == "failed" and value["semantic_error_count"] == 2
    assert value["source_failed_documents"] == 1 and value["source_unresolved"] == 1
