from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.api.auth import get_current_user
from app.config import settings
from app.intelligence.blob import LocalBlobStore, digest
from app.intelligence.models import (
    AnalysisJob,
    AuditSample,
    IntelligenceRun,
    SemanticCache,
)
from app.intelligence.pipeline import Pipeline
from app.intelligence.queue import JobQueue
from app.intelligence.refresh import request_refresh
from app.intelligence.semantics import checked_result, extraction_signature
from app.intelligence.sources import CapturedSource
from app.intelligence.visibility_flow import find_flow, reconcile_flows
from app.main import app
from app.models.device_gateway import DeviceTask
from app.models.models import Prompt
from app.services.device_gateway_service import claim_task, complete_task, fail_task
from app.utils.timezone import utcnow
from tests.conftest import TestSessionLocal
from tests.test_intelligence_sampling import result_for


@pytest.fixture
def writer(client, monkeypatch):
    monkeypatch.setattr(settings, "intelligence_enabled", True)
    user = {"scope": "project", "pid": "p1", "role": "owner", "sub": "editor-1"}
    app.dependency_overrides[get_current_user] = lambda: user
    return user


async def start_audit(client, db, key="v4-capture-key", platforms=("qwen",), brands=()):
    question = Prompt(project_id="p1", text="本地测试题目")
    db.add(question)
    await db.commit()
    request = {
        "sampling": {
            "promptIds": [question.id],
            "platforms": list(platforms),
            "surface": "managed_v1",
            "processingMode": "v4",
            "idempotencyKey": key,
        }
    }
    request["brands"] = list(brands)
    started = await client.post("/api/intelligence/projects/p1/audits", json=request)
    assert started.status_code == 202, started.text
    return int(started.json()["audit_id"])


async def capture_answer(db, with_source=False):
    task, token = await claim_task(db, "CHAO", ["browser.prompt"])
    result = result_for(task)
    if with_source:
        result["sources"] = [
            {
                "ordinal": 1,
                "urlResolution": "exact",
                "kind": "answer_reference",
                "title": "链接原文",
                "url": "https://example.com/article",
            }
        ]
        result["sourcePanel"]["capturedCount"] = 1
    await complete_task(db, "CHAO", task.id, token, result)
    return task, token, result


async def fetch_article(url, _limit):
    return CapturedSource(
        url, 200, b"<p>Article</p>", "Article source text.", "complete"
    )


async def job_count(db, kind):
    return await db.scalar(
        select(func.count()).select_from(AnalysisJob).where(AnalysisJob.kind == kind)
    )


@pytest.mark.asyncio
async def test_v4_waits_for_source_then_freezes_one_run(
    client, db_session, writer, tmp_path
):
    audit_id = await start_audit(client, db_session, platforms=("qwen", "deepseek"))
    base = f"/api/intelligence/audits/{audit_id}"
    assert (await client.get(f"{base}/capture")).json()["analysis"][
        "state"
    ] == "waiting_for_capture"

    task, token, result = await capture_answer(db_session, with_source=True)
    await complete_task(db_session, "CHAO", task.id, token, result)
    assert await job_count(db_session, "fetch_document") == 1
    assert await job_count(db_session, "analyze_run") == 0
    await db_session.commit()
    queue = JobQueue(TestSessionLocal, "worker-1")
    source_job = await queue.claim()
    assert source_job.kind == "fetch_document"
    await Pipeline(
        queue, LocalBlobStore(tmp_path), capture=fetch_article
    ).fetch_document(source_job)
    await reconcile_flows(TestSessionLocal)
    assert await job_count(db_session, "analyze_run") == 0
    await db_session.commit()

    failed_task, failed_token = await claim_task(db_session, "CHAO", ["appium.prompt"])
    await fail_task(
        db_session,
        "CHAO",
        failed_task.id,
        failed_token,
        "timeout",
        "internal",
        False,
        0,
    )
    await reconcile_flows(TestSessionLocal)
    await reconcile_flows(TestSessionLocal)
    run = await db_session.scalar(select(IntelligenceRun))
    assert run.manifest["capture_status"] == "partial"
    assert run.manifest["sources"][0]["snapshot_id"]
    assert await job_count(db_session, "analyze_run") == 1
    assert await job_count(db_session, "refresh_run") == 0
    progress = (await client.get(f"{base}/capture?summary_only=true")).json()
    assert progress["archive"]["state"] == "settled"
    assert progress["analysis"]["snapshot_id"] == run.id
    assert progress["analysis"]["state"] == "queued"
    assert (await client.post(f"{base}/runs")).status_code == 409
    await db_session.rollback()
    analysis = await queue.claim()
    assert analysis.kind == "analyze_run"
    await Pipeline(queue, LocalBlobStore(tmp_path)).analyze_run(analysis)
    progress = (await client.get(f"{base}/capture")).json()
    assert progress["analysis"]["state"] == "completed"
    assert progress["published_run_id"] == analysis.run_id
    score_response = await client.get(f"{base}/visibility-score")
    assert score_response.status_code == 200
    assert score_response.json()["status"] == "published"
    assert (
        score_response.json()["score"]["no_score_reason"] == "own_brand_not_configured"
    )
    assert (
        await client.get(f"{base}/visibility-score?own_target_id=registry:other")
    ).status_code == 404
    await db_session.commit()
    next_audit_id = await start_audit(client, db_session, key="workbench-next-audit")
    workbench_url = "/api/intelligence/projects/p1/workbench-audit"
    workbench_response = await client.get(workbench_url)
    assert workbench_response.status_code == 200, workbench_response.text
    workbench = workbench_response.json()
    assert workbench["latest_audit_id"] == str(next_audit_id)
    assert workbench["capture"]["analysis"]["state"] == "waiting_for_capture"
    assert workbench["capture"]["poll_after_ms"] > 0
    assert workbench["report"]["audit_id"] == str(audit_id)
    assert workbench["report"]["run_id"] == analysis.run_id
    assert workbench["report"]["finished_at"]
    expected_score = score_response.json()["score"]
    expected_score.get("coverage", {}).pop("scored_sample_ids", None)
    assert workbench["report"]["score"] == expected_score

    writer["pid"] = "another-project"
    assert (await client.get(f"{base}/visibility-score")).status_code == 403
    assert (await client.get(workbench_url)).status_code == 403
    assert progress["poll_after_ms"] is None


@pytest.mark.asyncio
async def test_v4_late_archive_is_readable_and_manual_retry_keeps_frozen_input(
    client, db_session, writer, tmp_path
):
    audit_id = await start_audit(client, db_session)
    await capture_answer(db_session, with_source=True)
    base = f"/api/intelligence/audits/{audit_id}"
    sample_id = await db_session.scalar(select(AuditSample.id))
    progress = (await client.get(f"{base}/capture")).json()
    assert progress["analysis"]["state"] == "waiting_for_archive"
    await db_session.commit()
    await reconcile_flows(TestSessionLocal)
    assert await job_count(db_session, "analyze_run") == 0

    flow = await find_flow(db_session, "p1", audit_id)
    flow.available_at = utcnow() - timedelta(seconds=5)
    await db_session.commit()
    queue = JobQueue(TestSessionLocal, "worker-1")
    source_job = await queue.claim()
    assert source_job.kind == "fetch_document"
    # The fetch finished after the deadline but before a resumed worker
    # detected settlement. Availability at the deadline still wins.
    await Pipeline(
        queue, LocalBlobStore(tmp_path), capture=fetch_article
    ).fetch_document(source_job)
    await reconcile_flows(TestSessionLocal)

    run = await db_session.scalar(select(IntelligenceRun))
    manifest = deepcopy(run.manifest)
    assert manifest["sources"][0]["fetch_status"] == "archive_deadline"
    assert manifest["sources"][0]["snapshot_id"] is None
    detail = (await client.get(f"{base}/capture/samples/{sample_id}")).json()
    assert detail["sources"][0]["archive"]["status"] == "archived"
    assert detail["sources"][0]["archive"]["used_in_analysis"] is False
    await db_session.commit()
    automatic = await queue.claim()
    assert automatic.kind == "analyze_run"
    assert automatic.payload["visibility_attempt"] == 1
    await queue.fail(automatic, "provider_error", retry=True)
    progress = (await client.get(f"{base}/capture")).json()
    assert progress["analysis"]["state"] == "failed"
    assert progress["analysis"]["can_retry"]
    assert progress["poll_after_ms"] is None
    await db_session.commit()

    body = {"failedAttemptId": automatic.id}
    writer["role"] = "viewer"
    assert (await client.post(f"{base}/analysis/retry", json=body)).status_code == 403
    writer["role"] = "owner"
    response = await client.post(f"{base}/analysis/retry", json=body)
    assert response.status_code == 202, response.text
    attempt = response.json()
    assert attempt["attempt_no"] == 2
    assert attempt["snapshot_id"] == run.id
    again = await client.post(f"{base}/analysis/retry", json=body)
    assert again.json() == attempt
    await request_refresh(db_session, "p1", audit_id, "identity_decision", "other")
    await db_session.commit()
    await reconcile_flows(TestSessionLocal)
    await db_session.refresh(run)
    assert run.manifest == manifest
    assert await job_count(db_session, "analyze_run") == 2
    assert await job_count(db_session, "refresh_run") == 0
    assert await db_session.scalar(select(func.count()).select_from(DeviceTask)) == 1


@pytest.mark.asyncio
async def test_v4_skips_analysis_after_all_samples_fail(client, db_session, writer):
    audit_id = await start_audit(client, db_session)
    task, token = await claim_task(db_session, "CHAO", ["browser.prompt"])
    await fail_task(db_session, "CHAO", task.id, token, "timeout", "internal", False, 0)
    await reconcile_flows(TestSessionLocal)
    progress = (await client.get(f"/api/intelligence/audits/{audit_id}/capture")).json()
    assert progress["capture"]["state"] == "failed"
    assert progress["archive"]["state"] == progress["analysis"]["state"] == "skipped"
    assert progress["poll_after_ms"] is None
    assert await job_count(db_session, "analyze_run") == 0


@pytest.mark.asyncio
async def test_v4_recovery_only_reclaims_attempts_before_model_dispatch(
    client, db_session, writer
):
    await start_audit(client, db_session)
    await capture_answer(db_session)
    await reconcile_flows(TestSessionLocal)
    queue = JobQueue(TestSessionLocal, "worker-1")
    first = await queue.claim()
    assert first.kind == "analyze_run"
    job = await db_session.get(AnalysisJob, first.id)
    job.lease_until = utcnow() - timedelta(seconds=5)
    await db_session.commit()
    assert await queue.reap() == 1
    resumed = await queue.claim()
    assert resumed.id == first.id and resumed.version > first.version
    await db_session.refresh(job)
    job.payload = {
        **job.payload,
        "model_budget": {"receipts": {"sent": {"state": "reserved"}}},
    }
    job.lease_until = utcnow() - timedelta(seconds=5)
    await db_session.commit()
    assert await queue.reap() == 1
    assert await queue.claim() is None
    await db_session.refresh(job)
    assert job.status == "failed"
    assert await job_count(db_session, "analyze_run") == 1


@pytest.mark.asyncio
async def test_v4_publishes_scored_answer_from_verified_extraction(
    client, db_session, writer, tmp_path
):
    audit_id = await start_audit(
        client, db_session, brands=({"id": "own", "name": "云衡"},)
    )
    task, token = await claim_task(db_session, "CHAO", ["browser.prompt"])
    text = "明确推荐云衡，原因是可以在多仓库之间同步库存。"
    await complete_task(db_session, "CHAO", task.id, token, result_for(task, text))
    _, base_url, model = settings.get_analysis_llm_config()
    db_session.add(
        SemanticCache(
            project_id="p1",
            segment_hash=digest(text.encode()),
            signature=extraction_signature(model, base_url),
            result=checked_result(
                text,
                {
                    "entities": [
                        {
                            "name": "云衡",
                            "kind": "brand",
                            "quote": text,
                            "start": 0,
                            "end": len(text),
                            "sentiment": "positive",
                        }
                    ],
                    "brand_signals": [
                        {
                            "name": "云衡",
                            "quote": text,
                            "start": 0,
                            "end": len(text),
                            "recommendation": 3,
                            "reason_level": "specific",
                        }
                    ],
                    "claims": [],
                    "topics": [],
                    "information_units": [],
                },
            ),
        )
    )
    await db_session.commit()

    await reconcile_flows(TestSessionLocal)
    queue = JobQueue(TestSessionLocal, "worker-scoring")
    lease = await queue.claim()
    await Pipeline(queue, LocalBlobStore(tmp_path)).analyze_run(lease)
    result = await client.get(f"/api/intelligence/audits/{audit_id}/visibility-score")
    assert result.status_code == 200
    assert result.json()["score"]["value"] == 65
    assert result.json()["score"]["coverage"]["scored"] == 1
    assert result.json()["targets"][0]["identity_method"] == "exact_registered_name"
