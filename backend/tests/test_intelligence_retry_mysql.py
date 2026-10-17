"""A manual retry must not inherit an obsolete MySQL repeatable-read snapshot."""

from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select

from app.api.intelligence import create_run
from app.config import settings
from app.intelligence.ingestion import prepare_run
from app.intelligence.models import AnalysisJob, IntelligenceRun
from app.models.models import Audit, QueryStatus
from tests.test_intelligence_mysql import MYSQL_URL, mysql_sessions  # noqa: F401

pytestmark = pytest.mark.skipif(
    not MYSQL_URL, reason="Explicit isolated MySQL CI database not configured"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("superseded", [False, True])
async def test_manual_retry_rechecks_current_state_after_earlier_read(
    mysql_sessions, monkeypatch, superseded  # noqa: F811 - imported pytest fixture
):
    sessions, _ = mysql_sessions
    project = "ci-retry-" + uuid4().hex
    monkeypatch.setattr(settings, "intelligence_enabled", True)
    try:
        async with sessions() as writer:
            audit = Audit(
                project_id=project, status=QueryStatus.COMPLETED, brands_json=[]
            )
            writer.add(audit)
            await writer.flush()
            run = await prepare_run(writer, project, audit.id)
            job = await writer.scalar(
                select(AnalysisJob).where(AnalysisJob.run_id == run.id)
            )
            run.status, job.status, job.attempt = "failed", "failed", 3
            await writer.commit()
            audit_id, run_id = audit.id, run.id

        async with sessions() as reader:
            # This authorization/read transaction predates another request's write.
            observed = await reader.scalar(
                select(IntelligenceRun).where(IntelligenceRun.id == run_id)
            )
            assert observed.status == "failed"
            async with sessions() as writer:
                current = await writer.get(IntelligenceRun, run_id)
                if superseded:
                    writer.add(
                        IntelligenceRun(
                            project_id=project,
                            audit_id=audit_id,
                            revision=2,
                            input_hash="f" * 64,
                            signature=current.signature,
                            manifest=current.manifest,
                        )
                    )
                else:
                    await prepare_run(writer, project, audit_id, retry_failed=True)
                await writer.commit()

            user = {"scope": "project", "pid": project, "role": "owner"}
            if superseded:
                with pytest.raises(HTTPException) as error:
                    await create_run(audit_id, user=user, db=reader)
                assert error.value.status_code == 409
                await reader.rollback()
                assert (await reader.get(IntelligenceRun, run_id)).status == "failed"
            else:
                result = await create_run(audit_id, user=user, db=reader)
                assert result["id"] == run_id and result["status"] == "queued"
                job = await reader.scalar(
                    select(AnalysisJob).where(AnalysisJob.run_id == run_id)
                )
                assert len(job.payload["manual_retry_history"]) == 1
    finally:
        # Only this test's unique project; leave the isolated DB and other fixtures.
        async with sessions() as cleanup, cleanup.begin():
            for model in (AnalysisJob, IntelligenceRun, Audit):
                await cleanup.execute(delete(model).where(model.project_id == project))
