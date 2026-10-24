"""The real job row lock must serialize reservations without exceeding its cap."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.intelligence.ingestion import prepare_run
from app.intelligence.model_budget import budget_status
from app.intelligence.models import AnalysisJob, IntelligenceRun
from app.intelligence.pipeline import Pipeline
from app.intelligence.queue import JobQueue, LeaseLost
from app.models.models import Audit, PlatformResponseRecord, Prompt
from tests.test_intelligence_foundation import fixture_audit
from tests.test_intelligence_mysql import MYSQL_URL, mysql_sessions  # noqa: F401

pytestmark = pytest.mark.skipif(
    not MYSQL_URL, reason="Explicit isolated MySQL CI database not configured"
)


@pytest.mark.asyncio
async def test_concurrent_reservations_share_one_frozen_budget_and_stale_settlement_is_refused(
    mysql_sessions,  # noqa: F811 - imported isolated fixture
    monkeypatch,
):
    sessions, _ = mysql_sessions
    project = "ci-budget-" + uuid4().hex
    audit_id = None
    monkeypatch.setattr(settings, "intelligence_model_request_limit", 1)
    try:
        async with sessions() as db:
            audit = await fixture_audit(db, project=project)
            audit_id = audit.id
            run = await prepare_run(db, project, audit.id)
            await db.commit()
            run_id = run.id
        queue = JobQueue(sessions, "budget-worker")
        lease = await queue.claim()
        assert lease.run_id == run_id
        pipeline = Pipeline(queue, None)

        async def reserve_one(subject):
            return await pipeline.reserve_model_request(
                lease,
                phase="answer_extraction",
                messages=["input"],
                max_output=100,
                subject_id=subject,
            )

        receipts = await asyncio.gather(reserve_one("one"), reserve_one("two"))
        assert sum(bool(receipt) for receipt in receipts) == 1
        receipt = next(receipt for receipt in receipts if receipt)
        async with sessions() as db:
            job = await db.scalar(
                select(AnalysisJob).where(AnalysisJob.run_id == run_id)
            )
            assert budget_status(job.payload["model_budget"])["requests"] == 1
            assert len(job.payload["model_deferred"]) == 1
        await pipeline.settle_model_request(
            lease, receipt, {"prompt_tokens": 10, "completion_tokens": 20}
        )
        await pipeline.settle_model_request(
            lease, receipt, {"prompt_tokens": 10, "completion_tokens": 20}
        )
        await queue.fail(lease, "provider_timeout")
        with pytest.raises(LeaseLost):
            await pipeline.settle_model_request(lease, receipt, None)
        async with sessions() as db:
            job = await db.scalar(
                select(AnalysisJob).where(AnalysisJob.run_id == run_id)
            )
            assert budget_status(job.payload["model_budget"])["charged_tokens"] == 30
    finally:
        async with sessions() as db, db.begin():
            await db.execute(
                delete(AnalysisJob).where(AnalysisJob.project_id == project)
            )
            await db.execute(
                delete(IntelligenceRun).where(IntelligenceRun.project_id == project)
            )
            if audit_id is not None:
                await db.execute(
                    delete(PlatformResponseRecord).where(
                        PlatformResponseRecord.audit_id == audit_id
                    )
                )
            await db.execute(delete(Audit).where(Audit.project_id == project))
            await db.execute(delete(Prompt).where(Prompt.project_id == project))
