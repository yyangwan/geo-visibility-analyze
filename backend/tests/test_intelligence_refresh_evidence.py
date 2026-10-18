import gzip

import pytest
from sqlalchemy import select

from app.intelligence.blob import LocalBlobStore, digest
from app.intelligence.evidence import checked_verdict
from app.intelligence.ingestion import prepare_run
from app.intelligence.models import (
    AnalysisJob,
    AuditIntelligenceState,
    ClaimEvidence,
    SourceDocument,
    SourceFetch,
    SourceSnapshot,
)
from app.intelligence.pipeline import Pipeline
from app.intelligence.queue import JobQueue
from app.intelligence.refresh import publish_pointer, request_refresh
from app.intelligence.semantics import checked_result, stable_hash
from app.intelligence.sources import normalized_url
from tests.conftest import TestSessionLocal
from tests.test_intelligence_foundation import fixture_audit


@pytest.mark.asyncio
async def test_automatic_capture_refresh_dispatches_each_new_source_once(
    db_session, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "intelligence_refresh_debounce_seconds", 0)
    audit = await fixture_audit(
        db_session,
        sources=[
            {
                "url": "https://example.com/article",
                "kind": "answer_reference",
                "url_resolution": "exact",
            }
        ],
    )
    await request_refresh(db_session, "p1", audit.id, "sample_sealed", "first")
    await db_session.commit()
    queue = JobQueue(TestSessionLocal, "worker")
    await Pipeline(queue, None).refresh_run(await queue.claim())
    jobs = (await db_session.scalars(select(AnalysisJob))).all()
    assert sum(job.kind == "fetch_document" for job in jobs) == 1
    await prepare_run(db_session, "p1", audit.id, enrich=True)
    await db_session.commit()
    assert (
        len(
            (
                await db_session.scalars(
                    select(AnalysisJob).where(AnalysisJob.kind == "fetch_document")
                )
            ).all()
        )
        == 1
    )


@pytest.mark.asyncio
async def test_refresh_events_coalesce_and_latest_pointer_never_regresses(
    db_session, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "intelligence_refresh_debounce_seconds", 0)
    audit = await fixture_audit(db_session)
    await request_refresh(db_session, "p1", audit.id, "sample_sealed", "one")
    await request_refresh(db_session, "p1", audit.id, "source_ready", "two")
    await db_session.commit()
    assert len((await db_session.scalars(select(AnalysisJob))).all()) == 1
    queue = JobQueue(TestSessionLocal, "worker")
    pipeline = Pipeline(queue, None)
    await pipeline.refresh_run(await queue.claim())
    first = await prepare_run(db_session, "p1", audit.id, enrich=False)
    first.status = "partial"
    await publish_pointer(db_session, first)
    # A later-created revision finishing first remains the latest published one.
    from app.intelligence.models import IntelligenceRun

    later = IntelligenceRun(
        project_id="p1",
        audit_id=audit.id,
        revision=first.revision + 1,
        manifest=first.manifest,
        input_hash="f" * 64,
        signature=first.signature,
        status="partial",
    )
    db_session.add(later)
    await db_session.flush()
    await publish_pointer(db_session, later)
    await publish_pointer(db_session, first)
    state = await db_session.scalar(select(AuditIntelligenceState))
    assert state.published_run_id == later.id


@pytest.mark.asyncio
async def test_source_final_crash_exposes_failed_source_and_refresh(db_session):
    audit = await fixture_audit(
        db_session,
        sources=[
            {
                "url": "https://example.com/article",
                "kind": "answer_reference",
                "url_resolution": "exact",
            }
        ],
    )
    await prepare_run(db_session, "p1", audit.id)
    await db_session.commit()
    jobs = (await db_session.scalars(select(AnalysisJob))).all()
    fetch = next(job for job in jobs if job.kind == "fetch_document")
    fetch.max_attempts = 1
    other = next(job for job in jobs if job.kind == "analyze_run")
    other.status = "succeeded"
    await db_session.commit()
    queue = JobQueue(TestSessionLocal, "worker")
    lease = await queue.claim()
    await queue.fail(lease, "TimeoutError")
    assert (await db_session.scalar(select(SourceFetch))).status == "failed"
    assert await db_session.scalar(select(AuditIntelligenceState.pending_job_id))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,expected_calls",
    [
        ("answer_reference", 1),
        ("search_candidate", 0),
        ("unknown", 0),
        ("invalid_output", 1),
    ],
)
async def test_claim_verification_only_uses_actual_same_answer_sources(
    db_session, monkeypatch, tmp_path, kind, expected_calls
):
    from app.config import settings

    monkeypatch.setattr(settings, "analysis_llm_api_key", "fake-test-key")
    text = "品牌提供三年质保。"
    audit = await fixture_audit(
        db_session,
        text=text,
        sources=[
            {
                "title": "规格说明",
                "url": "https://example.com/article",
                "kind": "answer_reference" if kind == "invalid_output" else kind,
                "url_resolution": "exact",
            }
        ],
    )
    url = normalized_url("https://example.com/article")
    document = SourceDocument(project_id="p1", url=url, url_hash=stable_hash(url))
    db_session.add(document)
    await db_session.flush()
    source_text = "品牌提供两年质保。"
    blobs = LocalBlobStore(tmp_path)
    info = await blobs.put_immutable(
        f"projects/p1/sources/{document.id}/fixture/text.gz",
        gzip.compress(source_text.encode(), mtime=0),
    )
    snapshot = SourceSnapshot(
        project_id="p1",
        document_id=document.id,
        body_hash=digest(source_text.encode()),
        representation_hash="f" * 64,
        text_key=info.key,
        text_sha256=info.sha256,
        completeness="unknown",
        extractor_version="fixture",
    )
    db_session.add(snapshot)
    await db_session.flush()
    db_session.add(
        SourceFetch(
            project_id="p1",
            document_id=document.id,
            audit_id=audit.id,
            snapshot_id=snapshot.id,
            status="captured",
        )
    )
    await db_session.commit()
    await prepare_run(db_session, "p1", audit.id, enrich=False)
    await db_session.commit()

    async def analyze(value, *_):
        return checked_result(
            value,
            {
                "entities": [],
                "topics": [],
                "claims": [
                    {
                        "quote": text,
                        "start": 0,
                        "end": len(text),
                        "subject": "品牌",
                        "topic": "质保",
                    }
                ]
                if value == text
                else [],
            },
        ), {}

    calls = []

    async def verify(answer, source, *_):
        calls.append((answer, source))
        if kind == "invalid_output":
            return {
                "relation": "invented",
                "source_quote": "private-invalid-model-output",
            }
        return {"relation": "contradicts", "source_quote": "两年质保"}

    queue = JobQueue(TestSessionLocal, "worker")
    await Pipeline(queue, blobs, analyze=analyze, verify=verify).analyze_run(
        await queue.claim()
    )
    rows = (await db_session.scalars(select(ClaimEvidence))).all()
    assert len(calls) == len(rows) == expected_calls
    if rows:
        if kind == "invalid_output":
            assert rows[0].relation == "unverified" and rows[0].source_quote == ""
            assert rows[0].method == "model_output_invalid"
            from app.intelligence.models import IntelligenceRun

            run = await db_session.scalar(select(IntelligenceRun))
            assert run.status == "partial"
            assert run.coverage["evidence_alignment"]["invalid_pairs"] == 1
            return
        assert (
            rows[0].relation == "contradicts" and rows[0].review_state == "needs_review"
        )
        assert rows[0].source_quote in source_text


def test_verdict_cannot_invent_a_supporting_quote():
    assert checked_verdict(
        "两年质保", {"relation": "supports", "source_quote": "三年质保"}
    ) == {"relation": "unverified", "source_quote": ""}
