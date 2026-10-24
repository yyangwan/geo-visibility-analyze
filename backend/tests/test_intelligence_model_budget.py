from copy import deepcopy

import pytest
from sqlalchemy import select

from app.config import settings
from app.intelligence.blob import LocalBlobStore
from app.intelligence.ingestion import prepare_run
from app.intelligence.model_budget import (
    allocate,
    budget_status,
    freeze_budget,
    freeze_run_budget,
    recover_reservations,
    reserve,
    settle,
    usage_fields,
)
from app.intelligence.models import AnalysisJob
from app.intelligence.pipeline import Pipeline
from app.intelligence.queue import JobQueue, LeaseLost
from app.intelligence.semantics import checked_result
from tests.conftest import TestSessionLocal
from tests.test_intelligence_foundation import fixture_audit


def test_quotas_keep_the_total_and_small_budgets_prioritize_answers():
    for total in (0, 1, 2, 3, 4, 7, 99, 500, 10000):
        quotas = allocate(total)
        assert sum(quotas.values()) == total
        assert min(quotas.values()) >= 0
    assert allocate(1)["answer_extraction"] == 1
    assert allocate(500) == {
        "answer_extraction": 175,
        "source_extraction": 125,
        "context": 125,
        "verification": 75,
    }


def test_restarted_worker_keeps_ambiguous_calls_charged_but_not_in_flight():
    policy = freeze_budget(20, 10000)
    ledger, receipt, _ = reserve(None, policy, "answer_extraction", 2000, "segment")
    ledger["receipts"][receipt]["lease_version"] = 1
    assert recover_reservations(ledger, 1) == ledger
    recovered = recover_reservations(ledger, 2)
    assert budget_status(recovered)["charged_tokens"] == 2000
    assert (
        budget_status(recovered)["phases"]["answer_extraction"][
            "in_flight_reserved_tokens"
        ]
        == 0
    )
    assert recovered["receipts"][receipt]["state"] == "unknown"
    assert ledger["receipts"][receipt]["state"] == "reserved"


def test_provider_usage_retains_only_integer_counters_and_flags_overreporting():
    assert usage_fields(
        {"prompt_tokens": 20, "completion_tokens": 30, "private": "discard"}
    ) == {"prompt_tokens": 20, "completion_tokens": 30}
    assert usage_fields({"prompt_tokens": "20", "completion_tokens": 30}) == {}
    policy = freeze_budget(20, 10000)
    ledger, receipt, _ = reserve(None, policy, "answer_extraction", 2000, "segment")
    ledger = settle(ledger, receipt, {"prompt_tokens": 3000, "completion_tokens": 1000})
    assert budget_status(ledger)["phases"]["answer_extraction"]["over_limit"]
    assert reserve(ledger, policy, "answer_extraction", 1, "next")[1] is None


def test_source_quota_cannot_spend_context_or_verification_reserves():
    policy = freeze_budget(4, 1000)
    ledger, source, _ = reserve(None, policy, "source_extraction", 200, "source1")
    ledger = settle(ledger, source, {"prompt_tokens": 20, "completion_tokens": 30})
    ledger, denied, reason = reserve(
        ledger, policy, "source_extraction", 200, "source2"
    )
    assert denied is None and reason == "phase_request_limit"
    ledger, context, _ = reserve(ledger, policy, "context", 200, "context1")
    ledger, verification, _ = reserve(ledger, policy, "verification", 100, "pair1")
    assert context and verification
    assert budget_status(ledger)["requests"] == 3


def test_answer_reservation_tracks_workload_instead_of_wasting_source_budget():
    answers = [
        {"error": False, "text": "真实保存回答"},
        {"error": False, "text": "真实保存回答"},
    ]
    policy = freeze_run_budget(500, 10_000_000, answers)
    assert policy["phases"]["answer_extraction"]["requests"] == 1
    assert policy["phases"]["source_extraction"]["requests"] == 299
    assert policy["phases"]["context"]["requests"] == 125
    assert policy["phases"]["verification"]["requests"] == 75
    assert sum(row["tokens"] for row in policy["phases"].values()) == policy["tokens"]
    assert policy["phases"]["answer_extraction"]["tokens"] > 8192
    assert (
        freeze_run_budget(500, 10_000_000, [{"error": True, "text": "not an answer"}])[
            "phases"
        ]["answer_extraction"]["requests"]
        == 0
    )


def test_reported_usage_settles_once_and_unknown_usage_keeps_its_reservation():
    policy = freeze_budget(20, 10000)
    ledger, receipt, _ = reserve(None, policy, "answer_extraction", 2000, "segment")
    original = deepcopy(ledger)
    known = settle(ledger, receipt, {"prompt_tokens": 40, "completion_tokens": 60})
    assert ledger == original  # Input snapshots are never mutated in place.
    assert budget_status(known)["charged_tokens"] == 100
    assert (
        settle(known, receipt, {"prompt_tokens": 40, "completion_tokens": 60}) == known
    )
    with pytest.raises(ValueError, match="Conflicting"):
        settle(known, receipt, {})
    unknown = settle(ledger, receipt, {})
    assert (
        budget_status(unknown)["phases"]["answer_extraction"][
            "unknown_usage_reserved_tokens"
        ]
        == 2000
    )
    assert budget_status(unknown)["charged_tokens"] == 2000


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"prompt_tokens": True, "completion_tokens": 20},
        {"prompt_tokens": -1, "completion_tokens": 20},
        {"prompt_tokens": "1", "completion_tokens": 20},
    ],
)
def test_missing_or_malformed_usage_is_not_refunded_as_zero(usage):
    policy = freeze_budget(20, 10000)
    ledger, receipt, _ = reserve(None, policy, "verification", 1000, "pair")
    assert budget_status(settle(ledger, receipt, usage))["charged_tokens"] == 1000


def test_token_reservation_prevents_a_call_and_never_drops_a_receipt_on_denial():
    policy = freeze_budget(20, 10000)
    ledger, receipt, reason = reserve(
        None, policy, "source_extraction", 3000, "large-source"
    )
    assert receipt is None and reason == "phase_token_limit"
    assert ledger["receipts"] == {}
    ledger, receipt, _ = reserve(
        ledger, policy, "source_extraction", 2000, "small-source"
    )
    ledger = settle(ledger, receipt, None)
    ledger, denied, reason = reserve(ledger, policy, "source_extraction", 1000, "next")
    assert denied is None and reason == "phase_token_limit"
    assert len(ledger["receipts"]) == 1
    with pytest.raises(ValueError, match="Frozen"):
        reserve(ledger, freeze_budget(100, 100000), "context", 100, "new")


@pytest.mark.asyncio
async def test_cached_answers_are_applied_per_owner_without_new_reservations(
    db_session, monkeypatch, tmp_path
):
    from app.models.models import PlatformResponseRecord

    monkeypatch.setattr(settings, "analysis_llm_api_key", "fake")
    audit = await fixture_audit(db_session)
    first = await db_session.scalar(select(PlatformResponseRecord))
    db_session.add(
        PlatformResponseRecord(
            audit_id=audit.id,
            prompt_id=first.prompt_id,
            platform="kimi",
            response_text=first.response_text,
        )
    )
    await db_session.commit()
    run = await prepare_run(db_session, "p1", audit.id)
    await db_session.commit()
    calls = []

    async def analyze(text, *_):
        calls.append(text)
        return checked_result(text, {}), {"prompt_tokens": 50, "completion_tokens": 20}

    queue = JobQueue(TestSessionLocal, "worker")
    await Pipeline(queue, LocalBlobStore(tmp_path), analyze=analyze).analyze_run(
        await queue.claim()
    )
    await db_session.refresh(run)
    assert len(calls) == 1
    assert run.coverage["responses"] == 2
    assert run.coverage["model_budget"]["requests"] == 1
    assert run.coverage["model_budget"]["charged_tokens"] == 70


@pytest.mark.asyncio
async def test_budget_change_creates_new_revision_without_rebilling_a_cached_answer(
    db_session, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "analysis_llm_api_key", "fake")
    monkeypatch.setattr(settings, "intelligence_model_request_limit", 20)
    audit = await fixture_audit(db_session)
    first = await prepare_run(db_session, "p1", audit.id)
    await db_session.commit()
    original = deepcopy(first.manifest)
    calls = []

    async def analyze(text, *_):
        calls.append(text)
        return checked_result(text, {}), {"prompt_tokens": 50, "completion_tokens": 20}

    queue = JobQueue(TestSessionLocal, "worker")
    pipeline = Pipeline(queue, LocalBlobStore(tmp_path), analyze=analyze)
    await pipeline.analyze_run(await queue.claim())
    monkeypatch.setattr(settings, "intelligence_model_request_limit", 40)
    second = await prepare_run(db_session, "p1", audit.id)
    await db_session.commit()
    assert first.id != second.id and second.revision == first.revision + 1
    assert first.manifest == original
    assert first.manifest["responses"] == second.manifest["responses"]
    assert first.manifest["brand_targets"] == second.manifest["brand_targets"]
    await pipeline.analyze_run(await queue.claim())
    await db_session.refresh(second)
    assert len(calls) == 1
    assert second.coverage["model_budget"]["request_limit"] == 40
    assert second.coverage["model_budget"]["requests"] == 0
    assert second.coverage["model_budget"]["charged_tokens"] == 0


@pytest.mark.asyncio
async def test_denied_segments_have_scope_and_no_model_calls(
    db_session, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "analysis_llm_api_key", "fake")
    monkeypatch.setattr(settings, "intelligence_model_token_limit", 100)
    audit = await fixture_audit(db_session)
    run = await prepare_run(db_session, "p1", audit.id)
    await db_session.commit()

    async def forbidden(*_):
        raise AssertionError("Token denial must happen before provider call")

    queue = JobQueue(TestSessionLocal, "worker")
    await Pipeline(queue, LocalBlobStore(tmp_path), analyze=forbidden).analyze_run(
        await queue.claim()
    )
    await db_session.refresh(run)
    assert run.status == "partial"
    assert run.coverage["model_budget"]["requests"] == 0
    assert run.coverage["deferred_operations"][0]["phase"] == "answer_extraction"
    assert run.coverage["deferred_operations"][0]["reason"] == "phase_token_limit"
    from app.intelligence.readiness import result_readiness

    semantics = next(
        row
        for row in result_readiness(
            run.manifest, run.coverage, run.summary, run.status
        )["items"]
        if row["key"] == "semantics"
    )
    assert semantics["reason_codes"] == ["analysis_budget_exhausted"]
    assert "分析额度不足" in semantics["detail"]


@pytest.mark.asyncio
async def test_unknown_request_usage_persists_across_retry_and_stale_lease_is_fenced(
    db_session,
):
    audit = await fixture_audit(db_session)
    run = await prepare_run(db_session, "p1", audit.id)
    await db_session.commit()
    queue = JobQueue(TestSessionLocal, "worker")
    lease = await queue.claim()
    pipeline = Pipeline(queue, None)
    receipt = await pipeline.reserve_model_request(
        lease, phase="context", messages=["input"], max_output=100, subject_id="context"
    )
    await pipeline.settle_model_request(lease, receipt, None)
    await pipeline.settle_model_request(lease, receipt, None)
    job = await db_session.scalar(
        select(AnalysisJob).where(AnalysisJob.run_id == run.id)
    )
    await db_session.refresh(job)
    assert len(job.payload["model_budget"]["receipts"]) == 1
    await queue.fail(lease, "provider_timeout")
    with pytest.raises(LeaseLost):
        await pipeline.settle_model_request(
            lease, receipt, {"prompt_tokens": 1, "completion_tokens": 1}
        )
