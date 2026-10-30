"""Question/material isolation and verbatim evidence at the second model boundary."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.api.auth import get_current_user
from app.config import settings
from app.intelligence.contextual_analysis import (
    checked_context,
    context_payload,
    frozen_context,
)
from app.intelligence.ingestion import prepare_run
from app.intelligence.models import AnalysisJob, SemanticCache
from app.intelligence.pipeline import Pipeline
from app.intelligence.queue import JobQueue
from app.intelligence.semantics import (
    SemanticOutputError,
    checked_result,
    decode_semantic,
    stable_hash,
)
from app.main import app
from app.models.models import PlatformResponseRecord, Prompt
from tests.conftest import TestSessionLocal
from tests.test_intelligence_foundation import fixture_audit

TEXT = "🧭云衡支持30家门店，但仅限专业版。千问用于回答问题。多门店权限是一项功能。"
QUESTION = "需要支持30家门店的方案"


def span(text, quote):
    start = text.index(quote)
    return {"quote": quote, "start": start, "end": start + len(quote)}


def extraction(text=TEXT):
    names = [
        ("云衡", "brand", "云衡支持30家门店，但仅限专业版。"),
        ("千问", "brand", "千问用于回答问题。"),
        ("多门店权限", "product", "多门店权限是一项功能。"),
    ]
    return checked_result(
        text,
        {
            "entities": [
                {
                    "name": name,
                    "kind": kind,
                    **span(text, quote),
                    "sentiment": "unknown",
                    "attributes": [],
                }
                for name, kind, quote in names
            ],
            "claims": [],
            "topics": [],
            "brand_signals": [
                {
                    "name": "云衡",
                    **span(text, names[0][2]),
                    "recommendation": 3,
                    "reason_level": "need_matched",
                    "explicit_rank": None,
                }
            ],
            "information_units": [
                {
                    "kind": "feature",
                    "subject": "云衡",
                    "value": "30",
                    "unit": "家门店",
                    "conditions": ["仅限专业版"],
                    "effective_time": None,
                    **span(text, names[0][2]),
                },
                {
                    "kind": "feature",
                    "subject": None,
                    "value": "多门店权限",
                    "unit": None,
                    "conditions": [],
                    "effective_time": None,
                    **span(text, names[2][2]),
                },
            ],
        },
    )


def response(prompt=QUESTION, platform="qwen"):
    return {
        "id": "r1",
        "prompt": prompt,
        "prompt_hash": stable_hash(prompt),
        "platform": platform,
        "conditions": {"searchMode": "enabled"},
        "surface": "app",
    }


def manifest(review=True):
    result = {"responses": [response()], "sources": [], "approved_facts": {}}
    if review:
        result["evaluation_cohort"] = {
            "cells": [
                {
                    "prompt_hash": stable_hash(QUESTION),
                    "question_context": {
                        "review": {
                            "status": "confirmed",
                            "primary_scenario": "implementation",
                            "requirements": [span(QUESTION, "支持30家门店")],
                            "constraints": [],
                        }
                    },
                }
            ]
        }
    return result


def payload(review=True):
    frozen = manifest(review)
    return context_payload(
        SimpleNamespace(text=TEXT), extraction(), frozen_context(frozen, response())
    )


def verdict(value):
    roles = {
        "云衡": "evaluated_solution",
        "千问": "monitored_platform",
        "多门店权限": "feature_or_category",
    }
    return {
        "subjects": [
            {
                "candidate_id": item["id"],
                "context_role": roles[item["name"]],
                **{key: item[key] for key in ("quote", "start", "end")},
            }
            for item in value["candidates"]
        ],
        "requirement_matches": [
            {
                "unit_id": value["units"][0]["id"],
                "requirement_id": req["id"],
                "relation": "addresses",
                **span(TEXT, "云衡支持30家门店，但仅限专业版。"),
            }
            for req in value["reviewed_requirements"]
        ],
    }


def test_units_preserve_unbranded_information_and_price_conditions_without_business_owner():
    result = extraction()
    assert len(result["information_units"]) == 2
    assert result["information_units"][0]["conditions"] == ["仅限专业版"]
    assert result["information_units"][1]["subject"] is None
    assert "project_id" not in json.dumps(result)
    assert "own" not in result


@pytest.mark.parametrize(
    "field,value", [("value", "100"), ("conditions", ["免费版"]), ("subject", "竞品")]
)
def test_unit_fields_cannot_borrow_values_outside_its_evidence(field, value):
    raw = extraction()
    item = deepcopy(raw["information_units"][0])
    item[field] = value
    checked = checked_result(TEXT, {"information_units": [item]})
    assert checked["information_units"] == []
    assert checked["rejected_items"][0]["code"] == "unit_fields_not_in_quote"


def test_unknown_conditions_stay_unknown_and_single_literal_repair_is_audited():
    from app.intelligence.information_units import local_units

    raw = {
        key: extraction()[key]
        for key in (
            "entities",
            "claims",
            "topics",
            "brand_signals",
            "information_units",
        )
    }
    raw["information_units"][0]["conditions"] = None
    raw["information_units"][1]["conditions"] = "多门店权限"
    checked = decode_semantic(TEXT, json.dumps(raw))
    assert checked["information_units"][0]["conditions"] is None
    assert local_units(checked)[0]["conditions_status"] == "unknown"
    assert checked["information_units"][1]["conditions"] == ["多门店权限"]
    assert checked["schema_repairs"] == [
        {"path": ["information_units", 1, "conditions"], "code": "string_to_singleton"}
    ]


def test_dense_text_is_fully_partitioned_without_one_large_output_request():
    from app.intelligence.semantics import chunks

    dense = "🧭某方案价格100元，限一年。" * 200 + "尾部限制不能遗漏。"
    segments = chunks(dense)
    assert all(len(item["text"]) <= 900 for item in segments)
    assert "".join(item["text"] for item in segments) == dense
    assert segments[-1]["end"] == len(dense)


def test_new_provider_contract_requires_units_and_forbids_business_role_injection():
    raw = {
        key: extraction()[key]
        for key in (
            "entities",
            "claims",
            "topics",
            "brand_signals",
            "information_units",
        )
    }
    raw.pop("information_units")
    with pytest.raises(SemanticOutputError):
        decode_semantic(TEXT, json.dumps(raw))
    raw["information_units"] = [{**extraction()["information_units"][0], "role": "own"}]
    with pytest.raises(SemanticOutputError):
        decode_semantic(TEXT, json.dumps(raw))


def test_context_role_and_requirement_retain_two_distinct_verbatim_sources():
    value = payload()
    checked = checked_context(value, verdict(value))
    matched = checked["requirement_matches"][0]
    assert matched["question_evidence"]["quote"] == "支持30家门店"
    assert TEXT[matched["start"] : matched["end"]] == matched["quote"]
    assert checked["subjects"][0]["review_state"] == "unreviewed"
    assert checked["subjects"][1]["context_role"] == "monitored_platform"


@pytest.mark.parametrize(
    "failure",
    [
        "missing_subject",
        "invented_subject",
        "duplicate_subject",
        "invented_requirement",
        "unit_span",
        "utf16_offset",
        "owner_role",
    ],
)
def test_context_rejects_incomplete_ids_cross_owner_evidence_and_business_roles(
    failure,
):
    value = payload()
    output = verdict(value)
    if failure == "missing_subject":
        output["subjects"].pop()
    elif failure == "invented_subject":
        output["subjects"][0]["candidate_id"] = "0" * 64
    elif failure == "duplicate_subject":
        output["subjects"].append(output["subjects"][0])
    elif failure == "invented_requirement":
        output["requirement_matches"][0]["requirement_id"] = "0" * 64
    elif failure == "unit_span":
        output["requirement_matches"][0].update(span(TEXT, "千问用于回答问题。"))
    elif failure == "utf16_offset":
        output["subjects"][0]["start"] += 1
    else:
        output["subjects"][0]["context_role"] = "competitor"
    from pydantic import ValidationError

    with pytest.raises((SemanticOutputError, ValidationError)):
        checked_context(value, output)


def test_unreviewed_question_cannot_claim_a_requirement_match():
    value = payload(review=False)
    assert value["reviewed_requirements"] == []
    output = verdict(payload())
    with pytest.raises(SemanticOutputError):
        checked_context(value, output)


def test_context_hash_varies_by_question_platform_conditions_and_material_but_not_priority():
    frozen = manifest()
    original = frozen_context(frozen, response())
    for changed in (
        response("需要10家门店"),
        response(platform="kimi"),
        {**response(), "conditions": {"searchMode": "disabled"}},
    ):
        assert stable_hash(frozen_context(frozen, changed)) != stable_hash(original)
    frozen["business_preferences"] = {"scenario_importance": {"implementation": 100}}
    frozen["brand_context"] = {
        "version": 99,
        "business_preferences": frozen["business_preferences"],
    }
    assert frozen_context(frozen, response()) == original
    frozen["approved_facts"] = {
        "fact": {"version": 2, "statement": "自有宣传不能补入回答"}
    }
    changed = frozen_context(frozen, response())
    assert changed["material_versions_hash"] != original["material_versions_hash"]
    assert "自有宣传" not in json.dumps(
        context_payload(SimpleNamespace(text=TEXT), extraction(), changed),
        ensure_ascii=False,
    )


async def run_fixture(db, monkeypatch, tmp_path, *, context=None, budget=30):
    monkeypatch.setattr(settings, "analysis_llm_api_key", "fake")
    monkeypatch.setattr(settings, "intelligence_model_request_limit", budget)
    monkeypatch.setattr(settings, "intelligence_model_token_limit", 2_000_000)
    audit = await fixture_audit(db, text=TEXT)
    run = await prepare_run(db, "p1", audit.id)
    await db.commit()
    calls = []

    async def first_pass(text, *_):
        calls.append("extract")
        return extraction(text), {"prompt_tokens": 100, "completion_tokens": 30}

    async def second_pass(value, *_):
        calls.append("context")
        if context:
            return context(value)
        return verdict(value), {"prompt_tokens": 200, "completion_tokens": 40}

    queue = JobQueue(TestSessionLocal, "context-test")
    pipeline = Pipeline(queue, None, analyze=first_pass, contextualize=second_pass)
    await pipeline.analyze_run(await queue.claim())
    await db.refresh(run)
    return audit, run, calls, pipeline


@pytest.mark.asyncio
async def test_runtime_preserves_all_subjects_but_only_solution_enters_comparison(
    db_session, monkeypatch, tmp_path
):
    _, run, calls, _ = await run_fixture(db_session, monkeypatch, tmp_path)
    assert calls == ["extract", "context"]
    assert {item["name"] for item in run.summary["entities"]} == {
        "云衡",
        "千问",
        "多门店权限",
    }
    assert [item["name"] for item in run.summary["brand_presence"]["targets"]] == [
        "云衡"
    ]
    assert run.summary["context_analysis"]["completed"] == 1
    assert len(run.summary["information_units"]["observations"]) == 2
    # No reviewed question requirement, so extraction cannot assign maximum match level.
    assert run.summary["brand_signals"]["observations"][0]["reason_level"] == "specific"
    assert run.coverage["model_budget"]["phases"]["context"]["reported_tokens"] == 240


@pytest.mark.asyncio
async def test_runtime_invalid_context_retains_units_and_records_usage_without_empty_success(
    db_session, monkeypatch, tmp_path
):
    _, run, calls, _ = await run_fixture(
        db_session,
        monkeypatch,
        tmp_path,
        context=lambda _: (
            {"subjects": [], "requirement_matches": []},
            {"prompt_tokens": 50, "completion_tokens": 20},
        ),
    )
    assert calls == ["extract", "context"]
    assert run.status == "partial"
    assert run.summary["context_analysis"]["failed"] == 1
    assert run.summary["context_analysis"]["observations"][0]["interpretation"] is None
    assert len(run.summary["information_units"]["observations"]) == 2
    assert run.coverage["model_budget"]["phases"]["context"]["reported_tokens"] == 70


@pytest.mark.asyncio
async def test_context_budget_defers_without_consuming_extraction_or_erasing_observations(
    db_session, monkeypatch, tmp_path
):
    _, run, calls, _ = await run_fixture(db_session, monkeypatch, tmp_path, budget=1)
    assert calls == ["extract"]
    assert run.summary["context_analysis"]["deferred"] == 1
    assert run.coverage["model_budget"]["phases"]["context"]["requests"] == 0
    assert any(
        row["phase"] == "context" and row["reason"] == "phase_request_limit"
        for row in run.coverage["deferred_operations"]
    )


@pytest.mark.asyncio
async def test_context_reuses_exact_frozen_interpretation_on_new_budget_revision(
    db_session, monkeypatch, tmp_path
):
    audit, first, calls, pipeline = await run_fixture(db_session, monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "intelligence_model_request_limit", 40)
    second = await prepare_run(db_session, "p1", audit.id)
    await db_session.commit()
    await pipeline.analyze_run(await pipeline.queue.claim())
    await db_session.refresh(second)
    assert second.id != first.id and calls == ["extract", "context"]
    assert second.coverage["model_budget"]["requests"] == 0
    assert second.summary["context_analysis"]["observations"][0]["cache_origin_id"]
    assert await db_session.scalar(select(func.count()).select_from(SemanticCache)) == 1


@pytest.mark.asyncio
async def test_same_passage_on_another_question_and_platform_gets_distinct_context(
    db_session, monkeypatch, tmp_path
):
    audit, _first, calls, pipeline = await run_fixture(
        db_session, monkeypatch, tmp_path
    )
    prompt = Prompt(project_id="p1", text="比较大模型平台")
    db_session.add(prompt)
    await db_session.flush()
    db_session.add(
        PlatformResponseRecord(
            audit_id=audit.id, prompt_id=prompt.id, platform="kimi", response_text=TEXT
        )
    )
    await db_session.commit()
    second = await prepare_run(db_session, "p1", audit.id)
    await db_session.commit()
    await pipeline.analyze_run(await pipeline.queue.claim())
    await db_session.refresh(second)
    assert calls == ["extract", "context", "context"]
    rows = second.summary["context_analysis"]["observations"]
    assert len(rows) == 2 and len({row["context_hash"] for row in rows}) == 2
    assert {row["context"]["platform"] for row in rows} == {"qwen", "kimi"}


@pytest.mark.asyncio
async def test_read_apis_are_bounded_project_scoped_and_do_not_queue_analysis(
    client, db_session, monkeypatch, tmp_path
):
    _, run, _, _ = await run_fixture(db_session, monkeypatch, tmp_path)
    before = await db_session.scalar(select(func.count()).select_from(AnalysisJob))
    app.dependency_overrides[get_current_user] = lambda: {
        "scope": "project",
        "pid": "p1",
        "role": "viewer",
    }
    try:
        units = await client.get(
            f"/api/intelligence/runs/{run.id}/information-units?limit=1"
        )
        assert units.status_code == 200 and units.json()["total"] == 2
        assert units.json()["next_offset"] == 1
        assert (
            await client.get(f"/api/intelligence/runs/{run.id}/context-analyses")
        ).status_code == 200
        assert (
            await client.get(
                f"/api/intelligence/runs/{run.id}/information-units?limit=101"
            )
        ).status_code == 422
        app.dependency_overrides[get_current_user] = lambda: {
            "scope": "project",
            "pid": "p2",
            "role": "owner",
        }
        assert (
            await client.get(f"/api/intelligence/runs/{run.id}/context-analyses")
        ).status_code == 404
        assert (
            await db_session.scalar(select(func.count()).select_from(AnalysisJob))
            == before
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_shared_snapshot_retains_separate_question_platform_observations(
    db_session, monkeypatch
):
    from app.intelligence.context_runtime import (
        analyze_run_contexts,
        context_projection,
    )
    from app.intelligence.models import ContentSegment, SegmentAnalysis
    from app.intelligence.semantics import extraction_signature

    monkeypatch.setattr(settings, "analysis_llm_api_key", "fake")
    monkeypatch.setattr(settings, "intelligence_model_request_limit", 40)
    monkeypatch.setattr(settings, "intelligence_model_token_limit", 2_000_000)
    audit = await fixture_audit(db_session, text=TEXT)
    run = await prepare_run(db_session, "p1", audit.id)
    await db_session.flush()
    _, base_url, model = settings.get_analysis_llm_config()
    segment = ContentSegment(
        project_id="p1",
        owner_kind="snapshot",
        owner_id="s" * 32,
        ordinal=0,
        start_offset=0,
        end_offset=len(TEXT),
        text=TEXT,
        segment_hash=stable_hash(TEXT),
        extractor_version="unicode-nonoverlap-1",
    )
    cache = SemanticCache(
        project_id="p1",
        segment_hash=segment.segment_hash,
        signature=extraction_signature(model, base_url),
        result=extraction(),
        usage={},
    )
    db_session.add_all([segment, cache])
    await db_session.flush()
    db_session.add(
        SegmentAnalysis(
            project_id="p1",
            run_id=run.id,
            segment_id=segment.id,
            cache_id=cache.id,
            status="completed",
        )
    )
    frozen = deepcopy(run.manifest)
    first = frozen["responses"][0]
    second = {
        **first,
        "id": "second-response",
        "platform": "kimi",
        "prompt": "比较不同方案",
        "prompt_hash": stable_hash("比较不同方案"),
    }
    frozen["responses"] = [first, second]
    frozen["sources"] = [
        {
            "id": f"ref-{index}",
            "response_id": row["id"],
            "platform": row["platform"],
            "snapshot_id": segment.owner_id,
            "kind": "answer_reference",
            "quality": {},
        }
        for index, row in enumerate(frozen["responses"])
    ]
    await db_session.commit()
    calls = []

    async def contextualize(value, *_):
        calls.append(value)
        return verdict(value), {"prompt_tokens": 100, "completion_tokens": 20}

    queue = JobQueue(TestSessionLocal, "source-context")
    pipeline = Pipeline(queue, None, contextualize=contextualize)
    await analyze_run_contexts(
        pipeline, await queue.claim(), frozen, "fake", base_url, model
    )
    projection = await context_projection(db_session, run)
    assert projection["completed"] == 2
    assert len({row["context_hash"] for row in projection["observations"]}) == 2
    assert {
        row["context"]["observation"]["source_id"] for row in projection["observations"]
    } == {"ref-0", "ref-1"}
    assert {row["platform"] for row in calls} == {"qwen", "kimi"}


def test_semantic_role_does_not_leak_between_same_name_mentions():
    from app.intelligence.context_runtime import annotate_subjects

    entities = [{"id": "e"}]
    mentions = [
        {
            "entity_id": "e",
            "segment_id": "s",
            "start": start,
            "end": start + 3,
            "observed_entity_name": "同名",
        }
        for start in (0, 10)
    ]
    projection = {
        "observations": [
            {
                "id": "c",
                "status": "completed",
                "segment_id": "s",
                "segment_start": 0,
                "context": {"observation": {}},
                "interpretation": {
                    "subjects": [
                        {
                            "observed_name": "同名",
                            "candidate_start": start,
                            "candidate_end": start + 3,
                            "context_role": role,
                        }
                        for start, role in (
                            (0, "reference_source"),
                            (10, "evaluated_solution"),
                        )
                    ]
                },
            }
        ]
    }
    annotate_subjects(entities, mentions, projection)
    assert [
        item["context_role"] for item in mentions[0]["context_interpretations"]
    ] == ["reference_source"]
    assert [
        item["context_role"] for item in mentions[1]["context_interpretations"]
    ] == ["evaluated_solution"]


def test_need_match_requires_this_answer_unit_and_prompt_evidence_and_no_contradiction():
    from app.intelligence.brand_signals import summarize_brand_signals

    value = payload()
    segment = SimpleNamespace(
        id="segment", owner_kind="answer", owner_id="r1", start_offset=0
    )
    cache = SimpleNamespace(result=extraction())
    applications = [(SimpleNamespace(status="completed"), segment, cache)]
    mention = {
        "id": "mention",
        "segment_id": "segment",
        "observed_entity_name": "云衡",
        "start": 1,
        "end": len("🧭云衡支持30家门店，但仅限专业版。"),
        "entity_id": "entity",
        "identity_ambiguity": False,
    }
    interpreted = checked_context(value, verdict(value))
    projection = {
        "observations": [
            {
                "id": "context",
                "owner_kind": "answer",
                "owner_id": "r1",
                "segment_id": "segment",
                "status": "completed",
                "interpretation": interpreted,
            }
        ]
    }
    result = summarize_brand_signals(
        applications, {"r1": response()}, [mention], projection
    )
    assert result["observations"][0]["reason_level"] == "need_matched"
    assert (
        result["observations"][0]["requirement_evidence"][0]["question_evidence"][
            "quote"
        ]
        == "支持30家门店"
    )
    interpreted["requirement_matches"].append(
        {**interpreted["requirement_matches"][0], "relation": "contradicts"}
    )
    result = summarize_brand_signals(
        applications, {"r1": response()}, [mention], projection
    )
    assert result["observations"][0]["reason_level"] == "specific"


def test_evaluated_role_on_one_platform_cannot_supply_another_platforms_score():
    from app.intelligence.brand_evaluation import summarize_answer_scores

    responses = [
        {"id": key, "platform": platform, "text": "云衡", "error": False}
        for key, platform in (("r1", "qwen"), ("r2", "kimi"))
    ]
    applications = [
        (
            SimpleNamespace(status="completed"),
            SimpleNamespace(
                owner_kind="answer",
                owner_id=row["id"],
                start_offset=0,
                end_offset=2,
                text="云衡",
            ),
            SimpleNamespace(result={"brand_signals": []}),
        )
        for row in responses
    ]
    entity = {
        "id": "e",
        "kind": "brand",
        "identity_status": "confirmed",
        "role": "unregistered",
        "comparison_context_status": "eligible",
        "answer_context_roles": {
            "r1": ["evaluated_solution"],
            "r2": ["monitored_platform"],
        },
    }
    mentions = [
        {
            "id": "m-" + row["id"],
            "owner_kind": "answer",
            "owner_id": row["id"],
            "entity_id": "e",
            "identity_ambiguity": False,
        }
        for row in responses
    ]
    results = summarize_answer_scores(
        {"responses": responses}, applications, [entity], mentions, {"observations": []}
    )
    assert results["answer_scores"][0]["dimensions"]["presence"] == 100
    assert results["answer_scores"][1]["dimensions"]["presence"] is None
