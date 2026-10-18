from datetime import UTC, timedelta

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.intelligence.blob import digest
from app.intelligence.capture_contract import CaptureV2, SamplingRequest
from app.intelligence.ingestion import prepare_run
from app.intelligence.models import (
    AuditSample,
    CaptureAttempt,
    CapturedResponse,
    PromptVersion,
    RunRefreshRequest,
)
from app.intelligence.sampling import create_sampling_audit, reconcile_capture_failures
from app.models.device_gateway import DeviceTask
from app.models.models import Prompt
from app.services.device_gateway_service import claim_task, complete_task, fail_task
from app.utils.timezone import utcnow


async def new_audit(db, rounds=2):
    prompt = Prompt(project_id="p1", text="完整提示词")
    db.add(prompt)
    await db.commit()
    request = SamplingRequest.model_validate(
        {
            "promptIds": [prompt.id],
            "platforms": ["qwen"],
            "rounds": rounds,
            "surface": "web",
            "conditions": {"searchMode": "enabled"},
            "idempotencyKey": "sampling-key-123",
        }
    )
    audit, _ = await create_sampling_audit(db, "p1", request, [])
    await db.commit()
    return audit, prompt, request


@pytest.mark.asyncio
async def test_managed_policy_covers_every_platform_and_records_actual_surface(
    db_session,
):
    prompt = Prompt(project_id="p1", text="完整提示词")
    db_session.add(prompt)
    await db_session.commit()
    request = SamplingRequest.model_validate(
        {
            "promptIds": [prompt.id],
            "platforms": ["qwen", "deepseek", "doubao", "kimi", "hunyuan"],
            "rounds": 2,
            "surface": "managed_v1",
            "idempotencyKey": "managed-policy-123",
        }
    )
    audit, count = await create_sampling_audit(db_session, "p1", request, [])
    await db_session.commit()
    assert count == 10
    samples = (await db_session.scalars(select(AuditSample))).all()
    tasks = (await db_session.scalars(select(DeviceTask))).all()
    assert len(samples) == len(tasks) == 10
    for sample in samples:
        expected = "web" if sample.platform == "qwen" else "app"
        assert sample.surface == expected
        task = next(t for t in tasks if t.payload["sample_id"] == sample.id)
        assert task.surface == expected
        assert task.task_type == (
            "browser.prompt" if expected == "web" else "appium.prompt"
        )
        assert task.platform == (
            "yuanbao" if sample.platform == "hunyuan" else sample.platform
        )
    again, repeated = await create_sampling_audit(db_session, "p1", request, [])
    assert again.id == audit.id and repeated == count
    assert len((await db_session.scalars(select(DeviceTask))).all()) == 10


def result_for(task, text="云衡与未知品牌都出现", kind="unknown"):
    return {
        "contractVersion": 2,
        "taskId": task.id,
        "platform": task.platform,
        "surface": task.surface,
        "observedAt": utcnow().replace(tzinfo=UTC).isoformat(),
        "answer": {
            "text": text,
            "status": "complete",
            "contentHash": digest(text.encode()),
        },
        "conditions": {"searchMode": "unknown"},
        "sources": [],
        "sourcePanel": {"status": "unknown", "expectedCount": None, "capturedCount": 0},
        "adapter": {"name": "test", "version": "2"},
    }


@pytest.mark.asyncio
async def test_rounds_are_independent_and_dispatch_is_bound_atomically(db_session):
    audit, prompt, request = await new_audit(db_session)
    samples = (await db_session.scalars(select(AuditSample))).all()
    attempts = (await db_session.scalars(select(CaptureAttempt))).all()
    assert len(samples) == len(attempts) == 2
    assert {sample.sample_index for sample in samples} == {1, 2}
    assert all(attempt.attempt_no == 1 for attempt in attempts)
    assert len((await db_session.scalars(select(DeviceTask))).all()) == 2
    again, count = await create_sampling_audit(db_session, "p1", request, [])
    assert again.id == audit.id and count == 2
    version = await db_session.scalar(select(PromptVersion))
    prompt.text = "修改后的提示词"
    await db_session.commit()
    assert version.text == "完整提示词"


@pytest.mark.asyncio
async def test_v2_completion_seals_once_and_duplicate_ack_is_idempotent(db_session):
    _audit, _, _ = await new_audit(db_session, rounds=1)
    task, token = await claim_task(db_session, "CHAO", ["browser.prompt"])
    result = result_for(task)
    await complete_task(db_session, "CHAO", task.id, token, result)
    await complete_task(db_session, "CHAO", task.id, token, result)
    assert len((await db_session.scalars(select(CapturedResponse))).all()) == 1
    assert (await db_session.scalar(select(AuditSample))).status == "sealed"
    assert (
        await db_session.scalar(select(RunRefreshRequest))
    ).reason == "sample_sealed"
    changed = result_for(task, "不同回答")
    with pytest.raises(HTTPException) as conflict:
        await complete_task(db_session, "CHAO", task.id, token, changed)
    assert conflict.value.status_code == 409
    await db_session.rollback()


@pytest.mark.asyncio
async def test_wrong_task_or_hash_never_stores_facts(db_session):
    await new_audit(db_session, rounds=1)
    task, token = await claim_task(db_session, "CHAO", ["browser.prompt"])
    result = result_for(task)
    result["answer"]["contentHash"] = "a" * 64
    with pytest.raises(HTTPException) as error:
        await complete_task(db_session, "CHAO", task.id, token, result)
    assert error.value.status_code == 422
    await db_session.rollback()
    assert (await db_session.scalars(select(CapturedResponse))).all() == []


@pytest.mark.asyncio
async def test_failed_attempt_creates_new_task_without_extra_sample(db_session):
    await new_audit(db_session, rounds=1)
    task, token = await claim_task(db_session, "CHAO", ["browser.prompt"])
    await fail_task(db_session, "CHAO", task.id, token, "timeout", "timeout", True, 0)
    assert len((await db_session.scalars(select(AuditSample))).all()) == 1
    attempts = (
        await db_session.scalars(
            select(CaptureAttempt).order_by(CaptureAttempt.attempt_no)
        )
    ).all()
    assert [attempt.attempt_no for attempt in attempts] == [1, 2]
    assert attempts[0].device_task_id != attempts[1].device_task_id
    assert attempts[0].status == "failed"


@pytest.mark.asyncio
async def test_worker_recovers_expired_device_attempt_without_gateway_poll(db_session):
    await new_audit(db_session, rounds=1)
    task, _ = await claim_task(db_session, "CHAO", ["browser.prompt"])
    task.lease_expires_at = utcnow() - timedelta(seconds=1)
    await db_session.commit()
    await reconcile_capture_failures(db_session)
    await db_session.commit()
    assert len((await db_session.scalars(select(CaptureAttempt))).all()) == 2
    assert task.status == "failed"


@pytest.mark.asyncio
async def test_manifest_uses_frozen_prompt_and_observed_not_requested_conditions(
    db_session,
):
    audit, prompt, _ = await new_audit(db_session, rounds=1)
    task, token = await claim_task(db_session, "CHAO", ["browser.prompt"])
    await complete_task(db_session, "CHAO", task.id, token, result_for(task))
    prompt.text = "后续修改"
    await db_session.commit()
    run = await prepare_run(db_session, "p1", audit.id)
    assert run.manifest["responses"][0]["prompt"] == "完整提示词"
    assert run.manifest["responses"][0]["conditions"]["searchMode"] == "unknown"
    assert (
        run.manifest["responses"][0]["requested_conditions"]["searchMode"] == "enabled"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: item.update(cookie="private"),
        lambda item: item["sourcePanel"].update(capturedCount=1),
        lambda item: item.update(observedAt="2026-10-14T00:00:00"),
    ],
)
def test_capture_schema_rejects_secrets_bad_counts_and_naive_time(mutation):
    class Task:
        id = "task-id"
        platform = "qwen"
        surface = "web"

    result = result_for(Task())
    mutation(result)
    with pytest.raises(ValidationError):
        CaptureV2.model_validate(result)
