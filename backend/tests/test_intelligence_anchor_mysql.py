"""Real MySQL JSON spans, fenced publication and copy reuse in an isolated CI DB."""

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.database import Base
from app.intelligence.blob import LocalBlobStore
from app.intelligence.ingestion import prepare_run
from app.intelligence.models import AnalysisJob, ClaimEvidence, IntelligenceRun
from app.intelligence.pipeline import Pipeline
from app.intelligence.queue import JobQueue, Lease, LeaseLost, db_now
from app.intelligence.semantics import checked_result
from app.models.models import PlatformResponseRecord
from tests.test_intelligence_anchors import seed_sources
from tests.test_intelligence_foundation import fixture_audit
from tests.test_intelligence_mysql import MYSQL_URL, mysql_sessions  # noqa: F401

pytestmark = pytest.mark.skipif(
    not MYSQL_URL, reason="Explicit isolated MySQL CI database not configured"
)


@pytest.mark.asyncio
async def test_anchor_json_proof_and_reuse_survive_real_mysql_publication(
    mysql_sessions,  # noqa: F811 - explicitly imported isolated fixture
    tmp_path,
    monkeypatch,
):
    sessions, _ = mysql_sessions
    project = "ci-anchor-" + uuid4().hex
    text, body = "🧭云衡全部版本质保三年。", "云衡质保两年，仅限专业版。"
    quote = text[1:]
    audit_id = None
    blobs = LocalBlobStore(tmp_path)
    monkeypatch.setattr(settings, "analysis_llm_api_key", "fake-mysql-anchor-key")
    monkeypatch.setattr(settings, "intelligence_model_request_limit", 1000)
    monkeypatch.setattr(settings, "intelligence_model_token_limit", 10000000)
    try:
        async with sessions() as db:
            audit = await fixture_audit(
                db,
                project=project,
                text=text,
                sources=[
                    {
                        "title": "规格",
                        "url": f"https://example.com/article-{index}",
                        "kind": "answer_reference",
                        "url_resolution": "exact",
                        "answerAnchor": [1, len(text)],
                    }
                    for index in range(2)
                ],
            )
            audit_id = audit.id
            await seed_sources(db, blobs, audit.id, [body, body], project)
            run = await prepare_run(db, project, audit.id, enrich=False)
            await db.flush()
            frozen = deepcopy(run.manifest)
            job = await db.scalar(
                select(AnalysisJob).where(AnalysisJob.run_id == run.id)
            )
            # Own only this test's queue row; other CI queue rows are never claimed.
            job.status, job.lease_owner, job.lease_token, job.lease_version = (
                "leased",
                "mysql-anchor",
                uuid4().hex,
                1,
            )
            job.lease_until = await db_now(db) + timedelta(seconds=180)
            job.attempt = 1
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
        calls = []

        async def analyze(value, *_):
            return checked_result(
                value,
                {
                    "entities": [],
                    "topics": [],
                    "claims": [
                        {
                            "quote": quote,
                            "start": 1,
                            "end": len(text),
                            "subject": "云衡",
                            "topic": "质保",
                        }
                    ]
                    if value == text
                    else [],
                },
            ), {"prompt_tokens": 1, "completion_tokens": 1}

        async def verify(answer, source, *_):
            calls.append((answer, source))
            return {
                "relation": "supports",
                "source_quote": "云衡",
                "checks": [
                    {
                        "dimension": "number",
                        "relation": "contradicts",
                        "answer_quote": "三年",
                        "source_quote": "两年",
                    },
                    {
                        "dimension": "condition",
                        "relation": "partial",
                        "answer_quote": "全部版本",
                        "source_quote": "仅限专业版",
                    },
                ],
            }, {"prompt_tokens": 2, "completion_tokens": 3}

        queue = JobQueue(sessions, "mysql-anchor")
        await Pipeline(queue, blobs, analyze=analyze, verify=verify).analyze_run(lease)
        async with sessions() as db:
            saved = await db.get(IntelligenceRun, run_id)
            evidence = (
                await db.scalars(
                    select(ClaimEvidence).where(ClaimEvidence.project_id == project)
                )
            ).all()
            assert saved.manifest == frozen and len(evidence) == 2 and len(calls) == 1
            assert all(
                row.relation == "contradicts"
                and row.verification["answer_span"]["start"] == 1
                for row in evidence
            )
            assert all(
                row.verification["result"]["checks"][0]["source_quote"] == "两年"
                for row in evidence
            )
            assert (
                sum(
                    row["deduplicated_content_weight"]
                    for row in saved.summary["claim_evidence"]
                )
                == 1
            )
            assert saved.coverage["evidence_alignment"]["processed_pairs"] == 2

        async def prohibited(_):
            raise AssertionError("Stale lease entered result writer")

        with pytest.raises(LeaseLost):
            await queue.checkpoint(lease, prohibited)
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
