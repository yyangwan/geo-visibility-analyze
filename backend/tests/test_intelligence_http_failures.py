from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.intelligence import evidence, semantics, worker
from app.intelligence.failures import classify_failure
from app.intelligence.ingestion import prepare_run
from app.intelligence.models import AnalysisJob
from app.intelligence.queue import JobQueue, LeaseLost
from tests.conftest import TestSessionLocal
from tests.test_intelligence_foundation import fixture_audit

SECRET = "private-sentinel-do-not-log"


def http_error(status):
    request = httpx.Request(
        "POST",
        f"https://user:{SECRET}@example.test/{SECRET}?token={SECRET}",
        headers={"authorization": f"Bearer {SECRET}", "x-request-id": SECRET},
        content=SECRET,
    )
    response = httpx.Response(
        status,
        request=request,
        headers={"x-request-id": SECRET, "retry-after": SECRET},
        json={"error": {"code": SECRET, "message": SECRET}},
    )
    return httpx.HTTPStatusError(SECRET, request=request, response=response)


def test_http_diagnostics_are_bounded_for_all_statuses_and_malformed_values():
    for status in range(100, 600):
        failure = classify_failure(http_error(status))
        assert failure.http_status == status
        assert failure.code == f"http_{status}"
        assert len(failure.code) <= 50
        assert failure.retry is (status in (408, 425, 429) or status >= 500)
    for status in (SECRET, None, True, 600, -1):
        error = http_error(500)
        error.response.status_code = status
        failure = classify_failure(error)
        assert failure.code == "http_error"
        assert failure.http_status is None
        assert SECRET not in repr(failure)


def test_diagnostics_do_not_read_http_body_headers_or_request():
    class UnreadableResponse:
        status_code = 402

        def __getattr__(self, name):
            raise AssertionError(f"Unsafe response access: {name}")

    error = httpx.HTTPStatusError(
        SECRET, request=object(), response=UnreadableResponse()
    )
    failure = classify_failure(error)
    assert failure.code == "http_402"
    assert failure.classification == "http_billing"
    assert failure.retry is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,classification,retry",
    [(301, "http_unexpected_status", False)]
    + [(s, "http_permanent", False) for s in (400, 401, 403, 404, 405, 409, 422)]
    + [(402, "http_billing", False)]
    + [(s, "http_transient", True) for s in (408, 425)]
    + [(429, "http_rate_limited", True)]
    + [(s, "http_server_error", True) for s in (500, 502, 503, 504)],
)
async def test_http_failure_is_safe_and_classified(
    caplog, status, classification, retry
):
    item = SimpleNamespace(id="test-job", kind="analyze_run")
    queue = SimpleNamespace(heartbeat=AsyncMock(), fail=AsyncMock())
    pipeline = SimpleNamespace(analyze_run=AsyncMock(side_effect=http_error(status)))
    await worker.execute(queue, pipeline, item)
    queue.fail.assert_awaited_once_with(item, f"http_{status}", retry=retry)
    record = next(r for r in caplog.records if r.name == "intelligence.worker")
    assert record.http_status == status
    assert record.failure_classification == classification
    assert record.retryable is retry
    assert f"status={status}" in record.getMessage()
    assert classification in record.getMessage()
    assert record.exc_info is None and record.stack_info is None
    assert SECRET not in repr(record.__dict__)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type,code,classification",
    [
        (t, "http_timeout", "http_timeout")
        for t in (
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
        )
    ]
    + [
        (httpx.ConnectError, "http_transport_error", "http_transport_error"),
        (httpx.RemoteProtocolError, "http_transport_error", "http_transport_error"),
        (TimeoutError, "timeout", "timeout"),
    ],
)
async def test_transient_transport_failure_retries(
    caplog, error_type, code, classification
):
    item = SimpleNamespace(id="test-job", kind="analyze_run")
    queue = SimpleNamespace(heartbeat=AsyncMock(), fail=AsyncMock())
    await worker.execute(
        queue,
        SimpleNamespace(analyze_run=AsyncMock(side_effect=error_type(SECRET))),
        item,
    )
    queue.fail.assert_awaited_once_with(item, code, retry=True)
    record = caplog.records[-1]
    assert record.http_status is None
    assert record.failure_classification == classification
    assert SECRET not in repr(record.__dict__)


@pytest.mark.asyncio
async def test_unknown_exception_name_and_chain_are_not_diagnostics(caplog):
    error = type(SECRET, (Exception,), {})(SECRET)
    error.__cause__ = http_error(401)
    item = SimpleNamespace(id="test-job", kind="analyze_run")
    queue = SimpleNamespace(heartbeat=AsyncMock(), fail=AsyncMock())
    await worker.execute(
        queue, SimpleNamespace(analyze_run=AsyncMock(side_effect=error)), item
    )
    queue.fail.assert_awaited_once_with(item, "job_error", retry=True)
    assert SECRET not in repr(caplog.records[-1].__dict__)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["analyze_run", "refresh_run", "fetch_document"])
async def test_http_failure_keeps_lease_loss_safe(kind, caplog):
    item = SimpleNamespace(id="test-job", kind=kind)
    queue = SimpleNamespace(
        heartbeat=AsyncMock(), fail=AsyncMock(side_effect=LeaseLost(SECRET))
    )
    await worker.execute(
        queue, SimpleNamespace(**{kind: AsyncMock(side_effect=http_error(403))}), item
    )
    queue.fail.assert_awaited_once_with(item, "http_403", retry=False)
    assert SECRET not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["semantic", "evidence"])
async def test_current_provider_paths_surface_safe_http_diagnostics(
    monkeypatch, caplog, call
):
    client_type = httpx.AsyncClient

    def respond(request):
        return http_error(401).response

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(respond)),
    )

    async def work(_):
        if call == "semantic":
            await semantics.analyze_text(
                SECRET, SECRET, f"https://example.test/{SECRET}", SECRET
            )
        else:
            await evidence.verify_pair(
                SECRET, SECRET, SECRET, f"https://example.test/{SECRET}", SECRET
            )

    item = SimpleNamespace(id="test-job", kind="analyze_run")
    queue = SimpleNamespace(heartbeat=AsyncMock(), fail=AsyncMock())
    await worker.execute(queue, SimpleNamespace(analyze_run=work), item)
    queue.fail.assert_awaited_once_with(item, "http_401", retry=False)
    records = [r for r in caplog.records if r.name == "intelligence.worker"]
    assert SECRET not in repr([r.__dict__ for r in records])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,max_attempts,expected",
    [
        (401, 3, "failed"),
        (402, 3, "failed"),
        (403, 3, "failed"),
        (404, 3, "failed"),
        (429, 3, "retry_wait"),
        (503, 3, "retry_wait"),
        (503, 1, "failed"),
    ],
)
async def test_real_queue_persists_safe_code_and_preserves_retry_budget(
    db_session, status, max_attempts, expected
):
    audit = await fixture_audit(db_session, text="Safe test answer")
    run = await prepare_run(db_session, "p1", audit.id)
    await db_session.flush()
    job = await db_session.scalar(
        select(AnalysisJob).where(AnalysisJob.run_id == run.id)
    )
    job.max_attempts = max_attempts
    job.payload = {"model_requests": 2, "reserved_usage": {"tokens": 42}}
    immutable_run = (run.revision, run.manifest, run.signature, run.input_hash)
    audit_state = (audit.status, audit.completed_at, audit.error_message)
    await db_session.commit()
    queue = JobQueue(TestSessionLocal, "test-worker")
    lease = await queue.claim()
    await worker.execute(
        queue,
        SimpleNamespace(analyze_run=AsyncMock(side_effect=http_error(status))),
        lease,
    )
    db_session.expire_all()
    saved = await db_session.get(AnalysisJob, lease.id)
    assert saved.status == expected
    assert saved.error_code == f"http_{status}"
    assert len(saved.error_code) <= 50
    assert saved.attempt == 1 and saved.max_attempts == max_attempts
    assert saved.lease_until is None
    assert saved.payload == {"model_requests": 2, "reserved_usage": {"tokens": 42}}
    await db_session.refresh(run)
    await db_session.refresh(audit)
    assert run.status == ("failed" if expected == "failed" else "queued")
    assert (run.finished_at is not None) is (expected == "failed")
    assert (run.revision, run.manifest, run.signature, run.input_hash) == immutable_run
    assert (audit.status, audit.completed_at, audit.error_message) == audit_state
