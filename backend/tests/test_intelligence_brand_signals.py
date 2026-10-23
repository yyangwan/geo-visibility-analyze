from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.intelligence.brand_signals import summarize_brand_signals
from app.intelligence.semantics import checked_result


def signal(**changes):
    return {"name": "云衡", "quote": "优先推荐云衡", "start": 0, "end": 6,
            "recommendation": 4, "explicit_rank": None, "reason_level": "none", **changes}


def test_signal_requires_verbatim_attributable_evidence():
    result = checked_result("优先推荐云衡", {"brand_signals": [signal()]})
    assert result["brand_signals"][0]["recommendation"] == 4
    assert checked_result("只有其他品牌", {"brand_signals": [signal()]})["rejected_spans"] == 1
    assert checked_result("优先推荐云衡", {"brand_signals": [signal(name="其他品牌")]})["brand_signals"] == []


@pytest.mark.parametrize("changes", [{"recommendation": 5}, {"recommendation": True},
                                     {"explicit_rank": "1"}, {"explicit_rank": 0},
                                     {"reason_level": "invented"}])
def test_signal_rejects_invalid_model_labels(changes):
    with pytest.raises(ValidationError):
        checked_result("优先推荐云衡", {"brand_signals": [signal(**changes)]})


def test_omitted_extraction_is_not_an_empty_observation():
    assert checked_result("云衡", {})["brand_signals"] is None
    assert checked_result("云衡", {"brand_signals": []})["brand_signals"] == []


def test_unicode_span_repair_and_ambiguous_quote_rejection():
    result = checked_result("🧭优先推荐云衡", {"brand_signals": [signal()]})
    assert result["brand_signals"][0]["start"] == 1
    assert result["brand_signals"][0]["end"] == 7
    result = checked_result("优先推荐云衡，优先推荐云衡", {"brand_signals": [signal(start=2, end=8)]})
    assert result["rejected_spans"] == 1


def test_summary_keeps_platform_owner_and_ambiguous_identity_separate():
    segment = SimpleNamespace(id="s1", owner_kind="answer", owner_id="r1", start_offset=100)
    cache = SimpleNamespace(result=checked_result("优先推荐云衡", {"brand_signals": [signal()]}))
    application = SimpleNamespace(status="completed")
    responses = {"r1": {"platform": "qwen", "prompt_hash": "p1", "error": False}}
    mention = {"id": "m1", "segment_id": "s1", "observed_entity_name": "云衡", "start": 104,
               "end": 106, "entity_id": "e1", "identity_ambiguity": False}
    result = summarize_brand_signals([(application, segment, cache)], responses, [mention])
    item = result["observations"][0]
    assert (item["start"], item["end"], item["platform"], item["entity_id"]) == (100, 106, "qwen", "e1")
    assert result["score_status"] == "awaiting_evaluation_cohort"
    assert "score" not in item
    ambiguous = summarize_brand_signals([(application, segment, cache)], responses,
                                       [mention, {**mention, "id": "m2", "entity_id": "e2"}])
    assert ambiguous["observations"][0]["entity_id"] is None
    outside = summarize_brand_signals([(application, segment, cache)], responses, [{**mention, "start": 200, "end": 202}])
    assert outside["observations"][0]["entity_id"] is None
    segment.owner_kind = "snapshot"
    assert summarize_brand_signals([(application, segment, cache)], responses, [mention])["observations"] == []


def test_missing_partial_and_failed_analysis_do_not_report_complete_extraction():
    segment = SimpleNamespace(id="s1", owner_kind="answer", owner_id="r1", start_offset=0)
    responses = {"r1": {"platform": "qwen", "prompt_hash": "p1", "error": False}}
    application = SimpleNamespace(status="completed")
    old_cache = SimpleNamespace(result={"entities": []})
    missing = summarize_brand_signals([(application, segment, old_cache)], responses, [])
    assert missing["status"] == "pending"
    assert missing["extracted_segments"] == 0
    cache = SimpleNamespace(result=checked_result("优先推荐云衡", {"brand_signals": [signal()]}))
    application.status = "partial"
    partial = summarize_brand_signals([(application, segment, cache)], responses, [])
    assert partial["status"] == "partial"
    assert partial["extracted_segments"] == 0
    responses["r1"]["error"] = True
    assert summarize_brand_signals([(application, segment, cache)], responses, [])["observations"] == []


@pytest.mark.asyncio
async def test_pipeline_persists_signals_and_reuses_cache_without_cross_platform_binding(db_session, monkeypatch, tmp_path):
    from app.config import settings
    from app.intelligence.blob import LocalBlobStore
    from app.intelligence.ingestion import prepare_run
    from app.intelligence.models import SemanticCache
    from app.intelligence.pipeline import Pipeline
    from app.intelligence.queue import JobQueue
    from app.models.models import PlatformResponseRecord
    from tests.conftest import TestSessionLocal
    from tests.test_intelligence_foundation import fixture_audit

    monkeypatch.setattr(settings, "analysis_llm_api_key", "fake-test-key")
    audit = await fixture_audit(db_session, text="优先推荐云衡")
    first = await db_session.scalar(select(PlatformResponseRecord))
    db_session.add(PlatformResponseRecord(audit_id=audit.id, prompt_id=first.prompt_id,
                                        platform="deepseek", response_text=first.response_text))
    await db_session.commit()
    run = await prepare_run(db_session, "p1", audit.id)
    await db_session.commit()
    calls = 0

    async def analyzer(text, *_):
        nonlocal calls
        calls += 1
        return checked_result(text, {"entities": [{"name": "云衡", "kind": "brand", "quote": "云衡",
                                                  "start": 4, "end": 6, "sentiment": "neutral"}],
                                     "brand_signals": [signal()]}), {}

    queue = JobQueue(TestSessionLocal, "worker")
    await Pipeline(queue, LocalBlobStore(tmp_path), analyze=analyzer).analyze_run(await queue.claim())
    db_session.expire_all()
    await db_session.refresh(run)
    assert calls == 1
    persisted = await db_session.scalar(select(SemanticCache))
    assert persisted.result["brand_signals"][0]["recommendation"] == 4
    observations = run.summary["brand_signals"]["observations"]
    assert len(observations) == 2
    assert {item["platform"] for item in observations} == {"qwen", "deepseek"}
    assert len({item["response_id"] for item in observations}) == 2
    assert len({item["id"] for item in observations}) == 2
    assert all(item["entity_id"] and item["mention_ids"] for item in observations)
