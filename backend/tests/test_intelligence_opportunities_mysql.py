"""Real Pipeline -> persisted opportunity -> unchanged task plan, without a fake own entity."""

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.database import Base
from app.intelligence.actions import PlanRequest, create_plan
from app.intelligence.blob import LocalBlobStore
from app.intelligence.brand_evaluation import freeze_cohort
from app.intelligence.evaluation_reviews import reviewed_cohort
from app.intelligence.ingestion import prepare_run
from app.intelligence.models import AnalysisJob, IntelligenceRun, PlatformDiagnosis
from app.intelligence.opportunity_tasks import rank_opportunities
from app.intelligence.pipeline import Pipeline
from app.intelligence.project_context import (
    ContextRequest,
    context_snapshot,
    receive_context,
)
from app.intelligence.question_context import QuestionReview
from app.intelligence.queue import JobQueue, Lease, db_now
from app.intelligence.semantics import checked_result, stable_hash
from app.intelligence.value_contracts import TaskPackV2
from app.models.models import PlatformResponseRecord, Prompt
from tests.test_intelligence_foundation import fixture_audit
from tests.test_intelligence_mysql import MYSQL_URL, mysql_sessions  # noqa: F401
from tests.test_intelligence_opportunities import NEED, QUESTION, span

pytestmark = pytest.mark.skipif(
    not MYSQL_URL, reason="Explicit isolated MySQL CI database not configured"
)


@pytest.mark.asyncio
async def test_real_mysql_pipeline_keeps_absent_target_task_and_exact_plan(
    mysql_sessions,  # noqa: F811 - isolated imported fixture
    tmp_path,
    monkeypatch,
):
    sessions, _ = mysql_sessions
    project, audit_id = "ci-opportunity-" + uuid4().hex, None
    text = "🧭多门店权限应按总部、区域与门店配置，实施范围需要核实。"
    monkeypatch.setattr(settings, "analysis_llm_api_key", "fake-opportunity-mysql")
    monkeypatch.setattr(settings, "intelligence_model_request_limit", 1000)
    monkeypatch.setattr(settings, "intelligence_model_token_limit", 10000000)
    try:
        async with sessions() as db:
            await receive_context(
                db,
                project,
                ContextRequest(
                    version=1,
                    brands=[{"id": "own", "name": "云衡", "is_competitor": False}],
                ),
                "owner",
            )
            await db.commit()
            audit = await fixture_audit(db, project=project, text=text)
            audit_id = audit.id
            response = await db.scalar(
                select(PlatformResponseRecord).where(
                    PlatformResponseRecord.audit_id == audit.id
                )
            )
            prompt = await db.get(Prompt, response.prompt_id)
            prompt.text = QUESTION
            await db.commit()
            run = await prepare_run(db, project, audit.id, enrich=False)
            frozen_response = run.manifest["responses"][0]
            base = freeze_cohort(
                [
                    {
                        "sample_id": frozen_response["id"],
                        "sample_index": 1,
                        "platform": "qwen",
                        "prompt_hash": frozen_response["prompt_hash"],
                        "prompt": QUESTION,
                        "category": "scenario",
                    }
                ],
                run.manifest["brands"],
            )
            review = QuestionReview(
                status="confirmed",
                primaryScenario="implementation",
                brandScope="neutral",
                requirements=[span(QUESTION, NEED)],
            )
            cohort = reviewed_cohort(
                base,
                {frozen_response["prompt_hash"]: "eligible"},
                {frozen_response["prompt_hash"]: review},
            )
            # Seed the same reviewed manifest consumed after the normal A01 reviewer workflow.
            run.manifest = {
                **run.manifest,
                "evaluation_cohort": cohort,
                "responses": [{**frozen_response, "answer_status": "complete"}],
            }
            run.input_hash = stable_hash(run.manifest)
            frozen = deepcopy(run.manifest)
            await db.flush()
            job = await db.scalar(
                select(AnalysisJob).where(AnalysisJob.run_id == run.id)
            )
            job.status, job.lease_owner, job.lease_token, job.lease_version = (
                "leased",
                "mysql-opportunity",
                uuid4().hex,
                1,
            )
            job.lease_until, job.attempt = await db_now(db) + timedelta(seconds=180), 1
            lease = Lease(
                job.id,
                project,
                job.lease_owner,
                job.lease_token,
                1,
                job.kind,
                job.subject_id,
                job.payload,
                run.id,
            )
            run_id = run.id
            await db.commit()

        async def analyze(value, *_):
            return checked_result(
                value,
                {
                    "entities": [],
                    "topics": [],
                    "claims": [],
                        "brand_signals": [],
                    "information_units": [
                        {
                            "kind": "implementation",
                            "subject": None,
                            "value": NEED,
                            "conditions": None,
                            **span(text, text),
                        }
                    ],
                },
            ), {"prompt_tokens": 1, "completion_tokens": 1}

        async def contextualize(payload, *_):
            return {
                "subjects": [],
                "requirement_matches": [
                    {
                        "unit_id": payload["units"][0]["id"],
                        "requirement_id": payload["reviewed_requirements"][0]["id"],
                        "relation": "addresses",
                        **span(text, text),
                    }
                ],
            }, {"prompt_tokens": 2, "completion_tokens": 2}

        await Pipeline(
            JobQueue(sessions, "mysql-opportunity"),
            LocalBlobStore(tmp_path),
            analyze=analyze,
            contextualize=contextualize,
        ).analyze_run(lease)
        async with sessions() as db:
            saved = await db.get(IntelligenceRun, run_id)
            projection = saved.summary["opportunities"]
            assert saved.manifest == frozen and projection["items"]
            assert saved.summary["brand_presence"]["targets"][0]["entity_id"] is None
            assert {kind for row in projection["items"] for kind in row["kinds"]} >= {
                "scenario_coverage",
                "content_evidence",
            }
            row = projection["items"][0]
            pack = TaskPackV2.model_validate(row["task_pack"])
            assert pack.own_target_id == "registry:own" and pack.own_fact_ids == ()
            assert all(
                ref.run_id == run_id and ref.project_id == project
                for ref in pack.diagnostic_evidence
            )
            diagnosis = await db.get(PlatformDiagnosis, row["record_id"])
            action = await create_plan(
                db,
                saved,
                PlanRequest(
                    diagnosisId=diagnosis.id, idempotencyKey="mysql-opportunity-plan"
                ),
                "owner",
            )
            await db.commit()
            original_task = deepcopy(action.target["task_pack"])
            await receive_context(
                db,
                project,
                ContextRequest(
                    version=2,
                    brands=[{"id": "own", "name": "云衡", "is_competitor": False}],
                    business_preferences={
                        "version": "business-preferences-v1",
                        "scenario_importance": {"implementation": 90},
                        "reason": "本季度优先多门店权限实施",
                    },
                ),
                "owner",
            )
            await db.commit()
            reordered, policy = rank_opportunities(
                projection["items"], await context_snapshot(db, project)
            )
            assert policy["preferences"]["mode"] == "custom"
            assert reordered[0]["priority"]["scenario_importance"] == 90
            assert (
                action.target["task_pack"] == original_task and saved.manifest == frozen
            )
    finally:
        async with sessions() as db, db.begin():
            if audit_id is not None:
                await db.execute(
                    delete(PlatformResponseRecord).where(
                        PlatformResponseRecord.audit_id == audit_id
                    )
                )
            for table in reversed(Base.metadata.sorted_tables):
                if "project_id" in table.c:
                    await db.execute(
                        table.delete().where(table.c.project_id == project)
                    )
