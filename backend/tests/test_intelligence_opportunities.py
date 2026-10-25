"""Business rules retain unknowns, real target identities and immutable deliverables."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.auth import get_current_user
from app.intelligence.actions import PlanRequest, create_plan, persist_diagnoses
from app.intelligence.drafts import authorize_draft
from app.intelligence.ingestion import prepare_run
from app.intelligence.models import AnalysisJob, PlatformDiagnosis
from app.intelligence.opportunities import build_opportunities
from app.intelligence.opportunity_tasks import (
    make_candidate,
    merge_candidates,
    rank_opportunities,
)
from app.intelligence.semantics import stable_hash
from app.intelligence.value_contracts import TaskPackV2
from app.main import app
from tests.test_intelligence_foundation import fixture_audit

QUESTION = "适合多门店权限的方案有哪些？"
NEED = "多门店权限"


def span(text, quote):
    start = text.index(quote)
    return {"quote": quote, "start": start, "end": start + len(quote)}


def fixture(
    *,
    platforms=("qwen",),
    own_present=False,
    rival_identity="confirmed",
    eligible="eligible",
):
    targets = [
        {
            "target_id": "registry:own",
            "registry_brand_id": "own",
            "entity_id": "own-entity" if own_present else None,
            "name": "云衡",
            "role": "own",
            "identity_status": "confirmed",
        },
        {
            "target_id": "entity:rival",
            "entity_id": "rival",
            "name": "锐川",
            "role": "unregistered",
            "identity_status": rival_identity,
        },
    ]
    responses, cells, applications, mentions, observations = [], [], [], [], []
    prompt_hash = stable_hash(QUESTION)
    for platform in platforms:
        text = "🧭锐川支持多门店权限，仅限专业版。" + (
            "云衡提供基础权限。" if own_present else ""
        )
        response = {
            "id": platform,
            "platform": platform,
            "prompt_hash": prompt_hash,
            "prompt": QUESTION,
            "text": text,
            "error": False,
            "answer_status": "complete",
            "conditions": {"searchMode": "unknown"},
        }
        responses.append(response)
        review = {
            "status": "confirmed",
            "brand_scope": "neutral" if eligible != "excluded" else "directed",
            "requirements": [span(QUESTION, NEED)],
            "constraints": [],
            "audience": [],
        }
        cells.append(
            {
                "prompt_hash": prompt_hash,
                "platform": platform,
                "primary_scenario": "implementation",
                "eligibility": eligible,
                "question_context": {"review": review},
            }
        )
        segment = SimpleNamespace(
            id="segment-" + platform,
            owner_kind="answer",
            owner_id=platform,
            ordinal=0,
            text=text,
            start_offset=0,
            end_offset=len(text),
        )
        applications.append(
            (SimpleNamespace(status="completed"), segment, SimpleNamespace(result={}))
        )
        mentions.append(
            {
                "id": "mention-" + platform,
                "segment_id": segment.id,
                "owner_kind": "answer",
                "owner_id": platform,
                "entity_id": "rival",
                "observed_entity_name": "锐川",
                "sentiment": "positive",
                **span(text, "锐川"),
            }
        )
        observations.extend(
            [
                {
                    "target_id": "registry:own",
                    "response_id": platform,
                    "platform": platform,
                    "state": "present" if own_present else "observed_absent",
                    "coverage_complete": True,
                    "evidence_ids": [],
                    "score": None,
                    "dimensions": {
                        "presence": 100 if own_present else 0,
                        "recommendation": None,
                        "position": None,
                        "reasons": 49 if own_present else None,
                    },
                },
                {
                    "target_id": "entity:rival",
                    "response_id": platform,
                    "platform": platform,
                    "state": "present",
                    "coverage_complete": True,
                    "evidence_ids": ["mention-" + platform],
                    "score": None,
                    "dimensions": {
                        "presence": 100,
                        "recommendation": None,
                        "position": None,
                        "reasons": 50,
                    },
                },
            ]
        )
    run = SimpleNamespace(
        id="run",
        project_id="p1",
        manifest={
            "responses": responses,
            "sources": [],
            "evaluation_cohort": {"cells": cells},
            "brand_context": None,
            "owned_asset_revisions": [],
            "approved_facts": {},
        },
    )
    summary = {
        "brand_presence": {"targets": targets, "observations": observations},
        "mentions": mentions,
        "information_units": {"status": "processed", "observations": []},
        "context_analysis": {"status": "processed", "observations": []},
        "claim_evidence": [],
        "claims": [],
        "source_observations": {"observations": []},
    }
    return run, summary, applications


def items(result, kind):
    return [row for row in result["items"] if kind in row["kinds"]]


def add_unit(
    run,
    summary,
    applications,
    *,
    kind="case_study",
    source_kind="answer_reference",
    subject=None,
    value="30家门店",
):
    text = "该案例实施多门店权限，覆盖30家门店，仅限专业版。"
    segment = SimpleNamespace(
        id="source-segment",
        owner_kind="snapshot",
        owner_id="snapshot",
        ordinal=0,
        text=text,
        start_offset=0,
        end_offset=len(text),
    )
    applications.append(
        (SimpleNamespace(status="completed"), segment, SimpleNamespace(result={}))
    )
    run.manifest["sources"].append(
        {
            "id": "source",
            "response_id": "qwen",
            "platform": "qwen",
            "kind": source_kind,
            "url_resolution": "exact",
            "snapshot_id": "snapshot",
            "document_id": "document",
            "title": "案例",
            "snippet": "",
        }
    )
    summary["source_observations"]["observations"].append(
        {
            "id": "source",
            "snapshot_id": "snapshot",
            "frozen_version_valid": True,
            "content_coverage": {"stored_text_integrity": "verified"},
        }
    )
    unit = {
        "id": "unit",
        "local_id": "local-unit",
        "segment_id": segment.id,
        "owner_kind": "snapshot",
        "owner_id": "snapshot",
        "kind": kind,
        "subject": subject,
        "value": value,
        "conditions": ["仅限专业版"],
        "effective_time": None,
        **span(text, text),
    }
    summary["information_units"]["observations"].append(unit)
    summary["context_analysis"]["observations"].append(
        {
            "status": "completed",
            "segment_id": segment.id,
            "context": {"observation": {"response_id": "qwen", "source_id": "source"}},
            "interpretation": {
                "requirement_matches": [
                    {
                        "unit_id": "local-unit",
                        "relation": "addresses",
                        "question_evidence": span(QUESTION, NEED),
                    }
                ]
            },
        }
    )
    return unit


def test_absent_own_target_and_unregistered_rival_need_no_fake_entity_or_total():
    run, summary, apps = fixture()
    before = deepcopy(run.manifest)
    result = build_opportunities(run, summary, apps)
    row = items(result, "scenario_coverage")[0]
    pack = TaskPackV2.model_validate(row["task_pack"])
    assert pack.own_target_id == "registry:own" and row["own_entity_id"] is None
    assert row["comparison_target_ids"] == ["entity:rival"]
    assert row["actionability"] == "needs_material" and pack.own_fact_ids == ()
    assert row["publication_allowed"] is False and run.manifest == before
    assert "没有该能力" in " ".join(row["unknowns"])


def test_single_known_dimension_and_one_point_gap_still_produces_task():
    run, summary, apps = fixture(own_present=True)
    result = build_opportunities(run, summary, apps)
    row = items(result, "recommendation_strength")[0]
    assert row["task_pack"]["ai_targets"][0]["dimensions"] == ["reasons"]
    assert "49" in row["finding"] and "50" in row["finding"]
    assert (
        row["priority"]["roi"] is None
        and row["priority"]["success_probability"] is None
    )


def test_same_real_change_merges_platforms_but_added_target_changes_signature():
    run, summary, apps = fixture()
    first = items(build_opportunities(run, summary, apps), "scenario_coverage")[0]
    run, summary, apps = fixture(platforms=("qwen", "kimi"))
    merged = items(build_opportunities(run, summary, apps), "scenario_coverage")[0]
    assert (
        first["task_key"] == merged["task_key"]
        and first["task_signature"] != merged["task_signature"]
    )
    assert (
        len(merged["task_pack"]["ai_targets"]) == 2
        and len(merged["task_pack"]["delivery"]["questions"]) == 1
    )
    assert len(merged["subtargets"]) == 2


@pytest.mark.parametrize(
    "identity,state", [("pending", "needs_identity"), ("confirmed", "needs_material")]
)
def test_pending_discovered_brand_is_a_review_requirement(identity, state):
    run, summary, apps = fixture(rival_identity=identity)
    assert (
        items(build_opportunities(run, summary, apps), "scenario_coverage")[0][
            "actionability"
        ]
        == state
    )


def test_directed_question_is_not_a_competitive_weakness_action():
    run, summary, apps = fixture(eligible="excluded")
    row = items(build_opportunities(run, summary, apps), "scenario_coverage")[0]
    assert row["actionability"] == "not_applicable" and row["action"] is None


@pytest.mark.parametrize(
    "source_kind,kind",
    [("answer_reference", "source_channel"), ("search_candidate", "content_evidence")],
)
def test_unbranded_source_units_still_yield_specific_material_work_without_scores(
    source_kind, kind
):
    run, summary, apps = fixture()
    for row in summary["brand_presence"]["observations"]:
        row.update(state="unknown", dimensions={})
    add_unit(run, summary, apps, source_kind=source_kind)
    row = items(build_opportunities(run, summary, apps), kind)[0]
    assert row["source_ids"] == ["source"]
    assert row["subtargets"][0]["observation_kind"] == source_kind
    assert "客户类型、实施范围" in " ".join(row["task_pack"]["delivery"]["outline"])
    assert row["task_pack"]["own_fact_ids"] == [] and "30家门店" in row["finding"]
    if source_kind == "search_candidate":
        assert "检索候选" in row["finding"] and "实际引用" not in row["finding"]


def test_only_matching_own_valid_facts_can_be_review_candidates():
    run, summary, apps = fixture()
    run.manifest["owned_asset_revisions"] = [
        {
            "id": "material",
            "asset_key": "logical",
            "target_id": "registry:own",
            "title": NEED,
            "version": 2,
            "status": "captured",
            "decision": "verified",
            "verification_scope": NEED,
            "url": "https://brand.example.com/specs",
        }
    ]
    base = {
        "version": 1,
        "statement": "云衡支持多门店权限，仅限专业版。",
        "scope": NEED,
        "effective_status": "valid",
        "target_id": "registry:own",
        "owned_asset_revision_id": "material",
    }
    run.manifest["approved_facts"] = {
        "own": {**base, "id": "own-fact"},
        "rival": {**base, "id": "rival-fact", "target_id": "entity:rival"},
        "expired": {**base, "id": "expired", "effective_status": "expired"},
    }
    row = items(build_opportunities(run, summary, apps), "scenario_coverage")[0]
    assert row["task_pack"]["own_fact_ids"] == ["own-fact"]
    assert (
        row["actionability"] == "ready_for_review"
        and row["task_pack"]["delivery"]["material_version"] == 2
    )
    assert "not_entailment" in row["material_readiness"]["match_basis"]


def test_material_absence_does_not_claim_capability_absence_and_unknown_presence_is_not_zero():
    run, summary, apps = fixture()
    for row in summary["brand_presence"]["observations"]:
        row.update(state="unknown", dimensions={})
    result = build_opportunities(run, summary, apps)
    assert result["items"] == [] and result["status"] == "partial"


def test_different_claim_text_is_not_merged_even_when_value_is_missing():
    run, summary, apps = fixture()
    unit = add_unit(run, summary, apps, value=None)
    first = items(build_opportunities(run, summary, apps), "source_channel")[0]
    segment = apps[-1][1]
    segment.text = "该案例不适合多门店权限，覆盖范围待核对。"
    segment.end_offset = len(segment.text)
    unit.update(**span(segment.text, segment.text))
    second = items(build_opportunities(run, summary, apps), "source_channel")[0]
    assert first["task_key"] != second["task_key"]


def test_priority_preview_uses_scenario_settings_without_mutating_task_or_run():
    run, summary, apps = fixture()
    result = build_opportunities(run, summary, apps)
    before = deepcopy(result)
    context = {
        "version": 2,
        "content_hash": "a" * 64,
        "business_preferences": {"scenario_importance": {"implementation": 90}},
    }
    reranked, policy = rank_opportunities(result["items"], context)
    assert result == before
    assert reranked[0]["priority"]["scenario_importance"] == 90
    assert reranked[0]["task_signature"] == result["items"][0]["task_signature"]
    assert policy["preferences"]["score_weights_changed"] is False


def test_brand_perception_and_platform_differences_keep_original_evidence():
    run, summary, apps = fixture(platforms=("qwen", "kimi"), own_present=True)
    segment = apps[0][1]
    summary["mentions"].append(
        {
            "id": "negative-own",
            "segment_id": segment.id,
            "owner_kind": "answer",
            "owner_id": "qwen",
            "entity_id": "own-entity",
            "observed_entity_name": "云衡",
            "sentiment": "negative",
            **span(segment.text, "云衡提供基础权限。"),
        }
    )
    own = next(
        row
        for row in summary["brand_presence"]["observations"]
        if row["target_id"] == "registry:own" and row["platform"] == "kimi"
    )
    own["dimensions"]["reasons"] = 80
    result = build_opportunities(run, summary, apps)
    negative = items(result, "brand_perception")[0]
    assert (
        negative["task_pack"]["intent"] == "material_verification"
        and "未判断客观真伪" in negative["finding"]
    )
    difference = items(result, "platform_difference")[0]
    assert difference["counterevidence"][0]["owner_id"] == "kimi"
    assert "稳定效果" in " ".join(difference["unknowns"])


def test_conflict_keeps_both_quotes_and_never_becomes_automatic_correction():
    run, summary, apps = fixture(own_present=True)
    unit = add_unit(run, summary, apps)
    answer = apps[0][1]
    quote = "云衡提供基础权限。"
    summary["claims"].append(
        {
            "segment_id": answer.id,
            "owner_kind": "answer",
            "owner_id": "qwen",
            "subject": "云衡",
            **span(answer.text, quote),
        }
    )
    summary["mentions"].append(
        {
            "id": "own",
            "segment_id": answer.id,
            "owner_kind": "answer",
            "owner_id": "qwen",
            "entity_id": "own-entity",
            "observed_entity_name": "云衡",
            **span(answer.text, "云衡"),
        }
    )
    summary["claim_evidence"].append(
        {
            "id": "conflict",
            "response_id": "qwen",
            "citation_id": "source",
            "answer_segment_id": answer.id,
            "source_segment_id": unit["segment_id"],
            "relation": "contradicts",
            "answer_quote": quote,
            "source_quote": unit["quote"],
            "verification": {
                "answer_span": span(answer.text, quote),
                "source_span": span(unit["quote"], unit["quote"]),
            },
        }
    )
    row = next(
        row
        for row in items(build_opportunities(run, summary, apps), "content_evidence")
        if row["task_pack"]["intent"] == "material_verification"
    )
    assert row["counterevidence"][0]["owner_kind"] == "snapshot"
    assert (
        row["source_ids"] == ["source"]
        and row["priority_inputs"]["correction_urgency"] == 3
    )
    assert row["publication_allowed"] is False


def test_duplicate_source_candidates_merge_work_but_retain_each_citation():
    run, summary, apps = fixture()
    add_unit(run, summary, apps)
    run.manifest["sources"].append(
        {**run.manifest["sources"][0], "id": "second-source"}
    )
    summary["source_observations"]["observations"].append(
        {**summary["source_observations"]["observations"][0], "id": "second-source"}
    )
    second = deepcopy(summary["context_analysis"]["observations"][0])
    second["context"]["observation"]["source_id"] = "second-source"
    summary["context_analysis"]["observations"].append(second)
    row = items(build_opportunities(run, summary, apps), "source_channel")[0]
    assert row["source_ids"] == ["second-source", "source"]
    assert len(row["task_pack"]["ai_targets"]) == 1


def test_excluded_scope_does_not_cancel_or_enter_a_valid_shared_task():
    run, summary, apps = fixture(platforms=("qwen", "kimi"))
    run.manifest["evaluation_cohort"]["cells"][1]["eligibility"] = "excluded"
    row = items(build_opportunities(run, summary, apps), "scenario_coverage")[0]
    assert row["actionability"] == "needs_material"
    assert (
        row["task_pack"]["ai_targets"][0]["platform"] == "qwen"
        and len(row["task_pack"]["ai_targets"]) == 1
    )
    assert row["excluded_scopes"][0]["platform"] == "kimi"


def test_changed_scenario_priority_reorders_tasks_with_explicit_neutral_default():
    run, summary, apps = fixture()
    first = items(build_opportunities(run, summary, apps), "scenario_coverage")[0]
    other = deepcopy(first)
    other["task_key"] = "another-task"
    other["priority_inputs"]["scenarios"] = ["risk"]
    ranked, policy = rank_opportunities(
        [first, other],
        {
            "version": 4,
            "content_hash": "b" * 64,
            "business_preferences": {"scenario_importance": {"risk": 90}},
        },
    )
    assert (
        ranked[0]["task_key"] == "another-task"
        and ranked[1]["priority"]["scenario_importance"] == 50
    )
    assert policy["preferences"]["mode"] == "custom"


def test_candidate_limit_keeps_late_urgent_findings_and_reports_deferred(monkeypatch):
    from app.intelligence import opportunities

    run, summary, apps = fixture(own_present=True)
    segment = apps[0][1]
    summary["mentions"].append(
        {
            "id": "negative-own",
            "segment_id": segment.id,
            "owner_kind": "answer",
            "owner_id": "qwen",
            "entity_id": "own-entity",
            "observed_entity_name": "云衡",
            "sentiment": "negative",
            **span(segment.text, "云衡提供基础权限。"),
        }
    )
    monkeypatch.setattr(opportunities, "MAX_CANDIDATES", 1)
    result = build_opportunities(run, summary, apps)
    assert items(result, "brand_perception") and result["status"] == "partial"
    assert result["deferred_candidates"]["candidate_limit_priority_selection"] > 0


def test_capacity_rejection_keeps_all_subtargets_instead_of_silent_truncation():
    run, summary, apps = fixture()
    reference = items(build_opportunities(run, summary, apps), "scenario_coverage")[0][
        "task_pack"
    ]["diagnostic_evidence"]
    own = summary["brand_presence"]["targets"][0]
    response = run.manifest["responses"][0]
    scope = {
        "scenario_id": "implementation",
        "review_state": "confirmed",
        "eligibility": "eligible",
        "audience": [],
    }
    candidates = []
    for index in range(101):
        current = {
            **response,
            "id": str(index),
            "prompt_hash": stable_hash(index),
            "prompt": f"具体问题{index}",
        }
        candidates.append(
            make_candidate(
                run,
                own,
                current,
                scope,
                kind="scenario_coverage",
                goal=["shared"],
                topic=NEED,
                finding="保存的原文观察事实",
                evidence=reference,
            )
        )
    row = merge_candidates(run, candidates)[0]
    assert len(row["subtargets"]) == 101 and row["task_pack"] is None
    assert row["contract_error"] and row["action"] is None


@pytest.mark.asyncio
async def test_opportunity_read_and_plan_preserve_frozen_pack_and_block_v2_draft(
    db_session, client
):
    audit = await fixture_audit(db_session)
    stored = await prepare_run(db_session, "p1", audit.id, enrich=False)
    generated, summary, apps = fixture()
    generated.id = stored.id
    projection = build_opportunities(generated, summary, apps)
    stored.summary = {"opportunities": projection, "diagnoses": projection["items"]}
    stored.status = "partial"
    await persist_diagnoses(db_session, stored, stored.summary)
    await db_session.commit()
    original = deepcopy(stored.summary)
    app.dependency_overrides[get_current_user] = lambda: {
        "scope": "project",
        "pid": "p1",
        "sub": "reviewer",
        "role": "owner",
    }
    jobs_before = len((await db_session.scalars(select(AnalysisJob))).all())
    response = await client.get(
        f"/api/intelligence/runs/{stored.id}/opportunities?priority=current"
    )
    assert response.status_code == 200 and response.json()["frozen_run_unchanged"]
    assert len((await db_session.scalars(select(AnalysisJob))).all()) == jobs_before
    diagnosis = await db_session.scalar(
        select(PlatformDiagnosis).where(PlatformDiagnosis.run_id == stored.id)
    )
    await persist_diagnoses(db_session, stored, stored.summary)
    altered = deepcopy(stored.summary)
    altered["diagnoses"][0]["finding"] = "不允许覆盖既有任务包的诊断内容"
    with pytest.raises(HTTPException) as immutable:
        await persist_diagnoses(db_session, stored, altered)
    assert immutable.value.status_code == 409
    action = await create_plan(
        db_session,
        stored,
        PlanRequest(diagnosisId=diagnosis.id, idempotencyKey="opportunity-plan"),
        "reviewer",
    )
    assert action.target["task_pack"] == diagnosis.payload["task_pack"]
    with pytest.raises(HTTPException) as blocked:
        await authorize_draft(db_session, action, stored, None, "reviewer")
    assert blocked.value.status_code == 409 and stored.summary == original
    app.dependency_overrides[get_current_user] = lambda: {
        "scope": "project",
        "pid": "other",
        "sub": "reviewer",
        "role": "owner",
    }
    assert (
        await client.get(f"/api/intelligence/runs/{stored.id}/opportunities")
    ).status_code == 404
