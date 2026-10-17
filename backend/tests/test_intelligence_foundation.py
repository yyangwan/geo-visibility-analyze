"""Failure-boundary tests for the new durable intelligence pipeline."""

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.intelligence.blob import IntegrityError, LocalBlobStore
from app.intelligence.ingestion import prepare_run
from app.intelligence.models import (
    AnalysisJob,
    EntityMention,
    IntelligenceRun,
    SourceDocument,
    SourceSnapshot,
)
from app.intelligence.pipeline import Pipeline
from app.intelligence.queue import JobQueue, LeaseLost, db_now
from app.intelligence.semantics import checked_result, chunks
from app.intelligence.sources import (
    CapturedSource,
    SourceBlocked,
    normalized_url,
    public_ip,
)
from app.models.models import Audit, PlatformResponseRecord, Prompt, QueryStatus
from tests.conftest import TestSessionLocal


async def fixture_audit(
    db, *, sources=None, text="云衡提供服务。新品牌提供方案。", project="p1"
):
    prompt = Prompt(project_id=project, text="有哪些方案")
    audit = Audit(
        project_id=project, status=QueryStatus.COMPLETED, brands_json=[{"name": "云衡"}]
    )
    db.add_all([prompt, audit])
    await db.flush()
    db.add(
        PlatformResponseRecord(
            audit_id=audit.id,
            prompt_id=prompt.id,
            platform="qwen",
            response_text=text,
            raw_response={"sources": sources or []},
        )
    )
    await db.commit()
    return audit


def test_full_unicode_chunks_cover_tail_exactly():
    text = "🧭中文\n" * 1900 + "尾部未登记品牌"
    result = chunks(text)
    assert len(result) > 2
    assert "".join(chunk["text"] for chunk in result) == text
    assert result[-1]["end"] == len(text)
    for chunk in result:
        assert text[chunk["start"] : chunk["end"]] == chunk["text"]


def test_model_quotes_cannot_fabricate_entities():
    result = checked_result(
        "原文只有云衡",
        {
            "entities": [
                {
                    "name": "竞品",
                    "kind": "brand",
                    "quote": "不存在的竞品",
                    "start": 0,
                    "end": 5,
                    "sentiment": "positive",
                    "attributes": [],
                }
            ],
            "claims": [],
            "topics": [],
        },
    )
    assert result["entities"] == []
    assert result["rejected_spans"] == 1


def test_unicode_offsets_repair_only_unique_verbatim_quote():
    value = {
        "entities": [
            {
                "name": "品牌",
                "kind": "brand",
                "quote": "品牌",
                "start": 1,
                "end": 3,
                "sentiment": "unknown",
                "attributes": [],
            }
        ]
    }
    assert checked_result("🧭新品牌", value)["entities"][0]["start"] == 2
    assert checked_result("品牌和品牌", value)["entities"] == []


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.1",
        "100.100.100.200",
        "169.254.169.254",
        "::1",
        "::ffff:127.0.0.1",
        "fc00::1",
        "198.18.0.1",
        "224.0.0.1",
        "0.0.0.0",
    ],
)
def test_private_reserved_and_metadata_ips_blocked(ip):
    assert not public_ip(ip)


@pytest.mark.parametrize(
    "url",
    [
        "file:///x",
        "https://user:pass@example.com/",
        "https://example.com:8443/",
        "https://example.com\\@127.0.0.1/",
        "https://example.com/\r\nHeader: value",
    ],
)
def test_unsafe_urls_rejected(url):
    with pytest.raises(SourceBlocked):
        normalized_url(url)


@pytest.mark.asyncio
async def test_blobs_are_immutable_and_verified(tmp_path):
    store = LocalBlobStore(tmp_path)
    key = "projects/p1/sources/doc/hash/text.gz"
    info = await store.put_immutable(key, b"evidence")
    assert await store.get(key, info.sha256) == b"evidence"
    assert (await store.put_immutable(key, b"evidence")) == info
    with pytest.raises(IntegrityError):
        await store.put_immutable(key, b"changed")
    with pytest.raises(IntegrityError):
        await store.get(key, "bad_hash")
    with pytest.raises(ValueError):
        await store.put_immutable("projects/p1/../../secrets", b"x")


@pytest.mark.asyncio
async def test_queue_fences_old_worker_and_commits_atomically(db_session):
    db_session.add(
        AnalysisJob(
            project_id="p1", kind="test", subject_id="x", idempotency_hash="a" * 64
        )
    )
    await db_session.commit()
    first = JobQueue(TestSessionLocal, "first")
    second = JobQueue(TestSessionLocal, "second")
    lease = await first.claim()
    assert await second.claim() is None
    now = await db_now(db_session)
    await db_session.execute(
        update(AnalysisJob)
        .where(AnalysisJob.id == lease.id)
        .values(lease_until=now - timedelta(seconds=1))
    )
    await db_session.commit()
    assert await second.reap() == 1
    replacement = await second.claim()
    assert replacement.version > lease.version
    called = False

    async def forbidden(_):
        nonlocal called
        called = True

    with pytest.raises(LeaseLost):
        await first.commit(lease, forbidden)
    with pytest.raises(LeaseLost):
        await first.heartbeat(lease)
    assert not called
    await second.commit(replacement, forbidden)
    assert called


@pytest.mark.asyncio
async def test_final_attempt_crash_reaches_terminal_state(db_session):
    db_session.add(
        AnalysisJob(
            project_id="p1",
            kind="test",
            subject_id="x",
            idempotency_hash="b" * 64,
            max_attempts=1,
        )
    )
    await db_session.commit()
    queue = JobQueue(TestSessionLocal, "worker")
    lease = await queue.claim()
    now = await db_now(db_session)
    await db_session.execute(
        update(AnalysisJob)
        .where(AnalysisJob.id == lease.id)
        .values(lease_until=now - timedelta(seconds=1))
    )
    await db_session.commit()
    await queue.reap()
    db_session.expire_all()
    assert (await db_session.get(AnalysisJob, lease.id)).status == "failed"
    assert await queue.claim() is None


@pytest.mark.asyncio
async def test_fenced_transaction_rolls_back_result_and_success(db_session):
    db_session.add(
        AnalysisJob(
            project_id="p1", kind="test", subject_id="x", idempotency_hash="c" * 64
        )
    )
    await db_session.commit()
    queue = JobQueue(TestSessionLocal, "worker")
    lease = await queue.claim()

    async def failing(db):
        db.add(
            SourceDocument(
                project_id="p1", url="https://example.com/", url_hash="f" * 64
            )
        )
        await db.flush()
        raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError):
        await queue.commit(lease, failing)
    assert await db_session.scalar(select(SourceDocument.id)) is None
    db_session.expire_all()
    assert (await db_session.get(AnalysisJob, lease.id)).status == "leased"


@pytest.mark.asyncio
async def test_candidate_links_not_promoted_and_inputs_freeze(db_session):
    audit = await fixture_audit(
        db_session,
        sources=[
            {
                "url": "https://example.com/search",
                "kind": "search_candidate",
                "url_resolution": "exact",
            },
            {
                "url": "https://example.com/article",
                "kind": "answer_reference",
                "url_resolution": "exact",
                "snippet": "摘要",
            },
        ],
    )
    run = await prepare_run(db_session, "p1", audit.id)
    await db_session.commit()
    assert run.manifest["sources"][0]["document_id"] is None
    assert run.manifest["sources"][1]["document_id"] is not None
    assert (await prepare_run(db_session, "p1", audit.id)).id == run.id
    original = run.manifest["responses"][0]["text"]
    prr = await db_session.scalar(select(PlatformResponseRecord))
    prr.response_text = "updated after cutoff"
    await db_session.commit()
    assert run.manifest["responses"][0]["text"] == original
    with pytest.raises(LookupError):
        await prepare_run(db_session, "another-project", audit.id)


@pytest.mark.asyncio
async def test_unconfigured_model_reports_deferred_not_fake_analysis(
    db_session, monkeypatch, tmp_path
):
    from app.config import settings

    monkeypatch.setattr(settings, "analysis_llm_api_key", "")
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    audit = await fixture_audit(db_session, text="完整文本" * 2000)
    run = await prepare_run(db_session, "p1", audit.id)
    await db_session.commit()
    queue = JobQueue(TestSessionLocal, "worker")
    lease = await queue.claim()
    await Pipeline(queue, LocalBlobStore(tmp_path)).analyze_run(lease)
    db_session.expire_all()
    await db_session.refresh(run)
    result = run
    assert result.status == "partial"
    assert result.coverage["total_characters"] == 8000
    assert result.coverage["analyzed_characters"] == 0
    assert result.coverage["segment_states"]["deferred"] == 3


@pytest.mark.asyncio
async def test_cache_reuse_preserves_each_answer_owner_and_unknown_brands(
    db_session, monkeypatch, tmp_path
):
    from app.config import settings

    monkeypatch.setattr(settings, "analysis_llm_api_key", "fake-test-key")
    audit = await fixture_audit(db_session)
    first = await db_session.scalar(select(PlatformResponseRecord))
    db_session.add(
        PlatformResponseRecord(
            audit_id=audit.id,
            prompt_id=first.prompt_id,
            platform="deepseek",
            response_text=first.response_text,
        )
    )
    await db_session.commit()
    run = await prepare_run(db_session, "p1", audit.id)
    await db_session.commit()
    calls = 0

    async def analyzer(text, *_):
        nonlocal calls
        calls += 1
        return checked_result(
            text,
            {
                "entities": [
                    {
                        "name": "新品牌",
                        "kind": "brand",
                        "quote": "新品牌",
                        "start": 7,
                        "end": 10,
                        "sentiment": "unknown",
                        "attributes": [],
                    }
                ],
                "topics": ["方案"],
            },
        ), {}

    queue = JobQueue(TestSessionLocal, "worker")
    await Pipeline(queue, LocalBlobStore(tmp_path), analyze=analyzer).analyze_run(
        await queue.claim()
    )
    db_session.expire_all()
    await db_session.refresh(run)
    result = run
    assert calls == 1
    assert result.summary["entities"][0]["role"] == "unregistered"
    assert result.summary["entities"][0]["response_count"] == 2
    assert result.summary["entities"][0]["platform_response_counts"] == {
        "qwen": 1,
        "deepseek": 1,
    }
    assert len((await db_session.scalars(select(EntityMention))).all()) == 2


@pytest.mark.asyncio
async def test_enrichment_creates_new_run_without_mutating_original(
    db_session, tmp_path, monkeypatch
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
    original = await prepare_run(db_session, "p1", audit.id)
    await db_session.commit()
    original_manifest = original.input_hash
    queue = JobQueue(TestSessionLocal, "worker")
    first = await queue.claim()

    async def ignore(_):
        pass

    await queue.commit(first, ignore)

    async def capture(*_):
        return CapturedSource(
            "https://example.com/article", 200, b"<p>article</p>", "article", "unknown"
        )

    pipeline = Pipeline(queue, LocalBlobStore(tmp_path), capture=capture)
    await pipeline.fetch_document(await queue.claim())
    await pipeline.refresh_run(await queue.claim())
    db_session.expire_all()
    runs = (
        await db_session.scalars(
            select(IntelligenceRun).order_by(IntelligenceRun.revision)
        )
    ).all()
    assert len(runs) == 2
    assert runs[0].input_hash == original_manifest
    assert runs[0].manifest["sources"][0]["snapshot_id"] is None
    assert runs[1].manifest["sources"][0]["snapshot_id"] is not None
    assert (await db_session.scalar(select(SourceSnapshot))).completeness == "unknown"


@pytest.mark.asyncio
async def test_model_budget_survives_failed_request_and_restart(
    db_session, monkeypatch, tmp_path
):
    from app.config import settings

    monkeypatch.setattr(settings, "analysis_llm_api_key", "fake-test-key")
    monkeypatch.setattr(settings, "intelligence_model_request_limit", 1)
    audit = await fixture_audit(db_session)
    run = await prepare_run(db_session, "p1", audit.id)
    await db_session.commit()
    queue = JobQueue(TestSessionLocal, "worker")
    lease = await queue.claim()
    calls = 0

    async def failing(*_):
        nonlocal calls
        calls += 1
        raise TimeoutError("unknown provider usage")

    with pytest.raises(TimeoutError):
        await Pipeline(queue, LocalBlobStore(tmp_path), analyze=failing).analyze_run(
            lease
        )
    await Pipeline(queue, LocalBlobStore(tmp_path), analyze=failing).analyze_run(lease)
    assert calls == 1
    await db_session.refresh(run)
    assert run.status == "partial"
    assert run.coverage["analyzed_characters"] == 0
