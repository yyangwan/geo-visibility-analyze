"""Stable registered/unregistered targets, exact single-round pairs and unknowns."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select

from app.config import settings
from app.intelligence.brand_presence import VERSION as PRESENCE_VERSION
from app.intelligence.descriptive_measurement import (
    VERSION,
    compare_descriptive,
    freeze_descriptive,
)
from app.intelligence.measurement import (
    CohortRequest,
    CompareRequest,
    RetestRequest,
    checked_spec,
    compare_cohort,
    create_cohort,
    start_retest,
)
from app.intelligence.measurement_history import measurement_history
from app.intelligence.models import (
    AuditIntelligenceState,
    AuditSample,
    IntelligenceRun,
    MeasurementResult,
)
from app.intelligence.semantics import stable_hash
from tests.test_intelligence_foundation import fixture_audit
from tests.test_intelligence_measurement import ENTITY, fixture_cohort, snapshots

OWN = "registry:own"
RIVAL = "entity:" + ENTITY
NEW_ENTITY = "d" * 32


def descriptive_fixture(*, rounds=1, platforms=("qwen",)):
    old, before, after = snapshots(1, rounds, platforms)
    targets = [
        {
            "target_id": OWN,
            "registry_brand_id": "own",
            "entity_id": None,
            "name": "云衡",
            "names": ["云衡"],
            "identity_status": "confirmed",
            "role": "own",
        },
        {
            "target_id": RIVAL,
            "registry_brand_id": None,
            "entity_id": ENTITY,
            "name": "锐川",
            "names": ["锐川"],
            "identity_status": "confirmed",
            "role": "unregistered",
        },
    ]
    cells = []
    for slot in old["slots"]:
        slot["prompt_hash"] = stable_hash(slot["prompt_version_id"])
        slot["requested_conditions"] = deepcopy(before["responses"][0]["conditions"])
        cells.append(
            {
                "platform": slot["platform"],
                "prompt_hash": slot["prompt_hash"],
                "primary_scenario": "implementation",
                "eligibility": "eligible",
            }
        )
    for phase, snapshot in enumerate((before, after)):
        snapshot["evaluation_cohort"] = {
            "rule_version": "brand-visibility-v1",
            "cells": cells,
        }
        snapshot["entity_aliases"] = {}
        observed_targets = deepcopy(targets)
        if phase:
            observed_targets[0]["entity_id"] = NEW_ENTITY
        observations = []
        for response in snapshot["responses"]:
            response["prompt_hash"] = stable_hash(response["prompt_version_id"])
            for target in observed_targets:
                present = bool(phase) if target["target_id"] == OWN else not phase
                observations.append(
                    {
                        "target_id": target["target_id"],
                        "entity_id": target["entity_id"],
                        "response_id": response["id"],
                        "sample_id": response["sample_id"],
                        "platform": response["platform"],
                        "state": "present" if present else "observed_absent",
                        "coverage_complete": True,
                        "reason_codes": [],
                        "evidence_ids": ["mention-proof"] if present else [],
                        "dimensions": {
                            "presence": 100 if present else 0,
                            "recommendation": 75 if present else 0,
                            "position": None if present else 0,
                            "reasons": 100 if present else 0,
                        },
                    }
                )
        snapshot["brand_presence"] = {
            "version": PRESENCE_VERSION,
            "targets": observed_targets,
            "observations": observations,
        }
        snapshot["source_metrics"] = {
            "cited_card_presence": {
                "completed": set(snapshot["completed"]),
                "hits": {ENTITY: set(snapshot["completed"]) if not phase else set()},
                "traces": {},
            }
        }
    run = SimpleNamespace(
        id="a" * 32,
        input_hash="i" * 64,
        signature="signature",
        summary={"brand_presence": before["brand_presence"]},
        manifest={"evaluation_cohort": before["evaluation_cohort"]},
    )
    request = CohortRequest(
        baselineRunId=run.id,
        title="实施场景单轮观察",
        comparisonMode="descriptive_v1",
        brandTargetIds=[OWN, RIVAL],
        idempotencyKey="descriptive-fixture",
    )
    return freeze_descriptive(run, request, old["slots"]), before, after


def first_metric(result, metric="presence", target=OWN, platform="qwen"):
    cell = next(row for row in result["platforms"] if row["platform"] == platform)[
        "observations"
    ][0]
    item = next(row for row in cell["targets"] if row["brand_target_id"] == target)
    return next(row for row in item["metrics"] if row["metric"] == metric)


def test_single_round_pairs_absent_registry_target_with_first_confirmed_appearance():
    spec, before, after = descriptive_fixture(rounds=3)
    result = compare_descriptive(spec, before, after)
    assert len(spec["slots"]) == 1 and spec["min_rounds"] == 1
    assert result["method"] == {
        "name": "single_round_descriptive",
        "min_prompts": 1,
        "min_rounds": 1,
    }
    metric = first_metric(result)
    assert metric["baseline"]["value"] == 0 and metric["followup"]["value"] == 1
    assert metric["observed_delta"] == 1
    assert first_metric(result, target=RIVAL)["observed_delta"] == -1
    assert first_metric(result, "recommendation")["observed_delta"] == 75
    assert first_metric(result, "position")["observed_delta"] is None
    assert first_metric(result, "position")["comparison_state"] == "missing"
    assert first_metric(result, "cited_card_presence")["baseline"]["value"] is None
    assert first_metric(result, "cited_card_presence", RIVAL)["observed_delta"] == -1
    assert len(result["platforms"][0]["out_of_cohort"]) == 2
    assert not result["causal_claim_allowed"] and result["improvement_score"] is None
    assert "difference" not in metric and "interval95" not in metric


@pytest.mark.parametrize(
    "change,state,reason",
    [
        ("unknown", "conditions_unverified", "unknown_conditions"),
        ("changed_with_unknown", "incompatible", "conditions_changed"),
        ("surface", "incompatible", "conditions_changed"),
        ("prompt", "incompatible", "prompt_version_changed"),
        ("signature", "incompatible", "analysis_signature_changed"),
        ("rule", "incompatible", "score_rule_changed"),
        ("scenario", "incompatible", "question_scope_changed"),
        ("time", "incompatible", "observation_not_later"),
        ("partial", "missing", "incomplete_answer"),
        ("missing", "missing", "sample_missing"),
        ("duplicate", "missing", "ambiguous_sample"),
    ],
)
def test_changed_unknown_or_missing_pair_retains_facts_without_a_delta(
    change, state, reason
):
    spec, before, after = descriptive_fixture()
    if change in ("unknown", "changed_with_unknown"):
        after["responses"][0]["conditions"]["modelVersion"] = None
        if change == "changed_with_unknown":
            after["responses"][0]["conditions"]["location"] = "other"
    if change == "surface":
        after["responses"][0]["surface"] = "web"
    if change == "prompt":
        after["responses"][0]["prompt_hash"] = "changed"
    if change == "signature":
        after["signature"] = "different"
    if change == "rule":
        after["evaluation_cohort"] = {
            **after["evaluation_cohort"],
            "rule_version": "another-rule",
        }
    if change == "scenario":
        after["evaluation_cohort"] = deepcopy(after["evaluation_cohort"])
        after["evaluation_cohort"]["cells"][0]["primary_scenario"] = "pricing"
    if change == "time":
        after["responses"][0]["observed_at"] = "2026-10-12T00:00:00Z"
    if change == "partial":
        after["responses"][0]["answer_status"] = "partial"
    if change == "missing":
        after["responses"] = []
    if change == "duplicate":
        after["responses"].append(deepcopy(after["responses"][0]))
    result = compare_descriptive(spec, before, after)
    metric = first_metric(result)
    assert metric["observed_delta"] is None and metric["comparison_state"] == state
    assert reason in metric["reason_codes"]
    assert metric["baseline"]["value"] == 0


@pytest.mark.parametrize("change", ["names", "pending", "replacement"])
def test_unverified_identity_or_changed_name_scope_never_substitutes_a_new_subject(
    change,
):
    spec, before, after = descriptive_fixture()
    rival = after["brand_presence"]["targets"][1]
    if change == "names":
        rival["names"].append("另一个产品")
    elif change == "pending":
        rival["identity_status"] = "pending"
    else:
        rival["entity_id"] = "c" * 32
    metric = first_metric(compare_descriptive(spec, before, after), target=RIVAL)
    assert (
        metric["observed_delta"] is None
        and metric["comparison_state"] == "incompatible"
    )


def test_explicit_confirmed_alias_equivalence_is_required_to_follow_a_new_target_id():
    spec, before, after = descriptive_fixture()
    replacement = "c" * 32
    rival = after["brand_presence"]["targets"][1]
    rival.update(target_id="entity:" + replacement, entity_id=replacement)
    for row in after["brand_presence"]["observations"]:
        if row["target_id"] == RIVAL:
            row.update(target_id=rival["target_id"], entity_id=replacement)
    after["entity_aliases"] = {
        ENTITY: {
            "decision": "merge",
            "target_entity_id": replacement,
            "event_id": "human-equivalence",
        }
    }
    after["identity_context"] = {replacement: {"decision": "confirmed"}}
    result = compare_descriptive(spec, before, after)
    assert first_metric(result, target=RIVAL)["observed_delta"] == -1
    target = next(
        row
        for row in result["platforms"][0]["observations"][0]["targets"]
        if row["brand_target_id"] == RIVAL
    )
    assert target["followup_target_id"] == rival["target_id"]
    assert target["identity_equivalence"]["kind"] == "confirmed_alias_equivalence"


def test_platforms_keep_independent_regressions_and_partial_source_positives_stay_unknown():
    spec, before, after = descriptive_fixture(platforms=("qwen", "deepseek"))
    after["responses"][1]["conditions"]["modelVersion"] = None
    result = compare_descriptive(spec, before, after)
    assert first_metric(result)["observed_delta"] == 1
    assert first_metric(result, platform="deepseek")["observed_delta"] is None
    after["source_metrics"]["cited_card_presence"]["completed"] = set()
    after["source_metrics"]["cited_card_presence"]["hits"] = {
        NEW_ENTITY: {after["responses"][0]["id"]}
    }
    source = first_metric(
        compare_descriptive(spec, before, after), "cited_card_presence"
    )
    assert source["followup"]["value"] == 1 and not source["followup"]["known"]
    assert source["observed_delta"] is None


def test_baseline_projection_changes_are_rejected_instead_of_silently_rewriting_absence():
    spec, before, after = descriptive_fixture()
    before["brand_presence"]["observations"][0]["state"] = "unknown"
    with pytest.raises(HTTPException) as error:
        compare_descriptive(spec, before, after)
    assert error.value.status_code == 409


@pytest.mark.parametrize(
    "change", ["duplicate", "uncovered_absence", "no_evidence", "unrecognized_state"]
)
def test_unreliable_brand_observations_never_become_a_measured_zero_or_positive(change):
    spec, before, after = descriptive_fixture()
    own = next(
        row
        for row in after["brand_presence"]["observations"]
        if row["target_id"] == OWN
    )
    if change == "duplicate":
        after["brand_presence"]["observations"].append(deepcopy(own))
    elif change == "uncovered_absence":
        own.update(state="observed_absent", coverage_complete=False)
    elif change == "no_evidence":
        own["evidence_ids"] = []
    else:
        own["state"] = "unrecognized"
    metric = first_metric(compare_descriptive(spec, before, after))
    assert metric["observed_delta"] is None and metric["followup"]["value"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("metric_version", "other"),
        ("condition_policy", "other"),
        ("min_rounds", 3),
        ("metrics", ["presence"]),
    ],
)
def test_supported_policy_is_frozen_even_if_a_spec_hash_is_recomputed(field, value):
    spec, _before, _after = descriptive_fixture()
    spec[field] = value
    cohort = SimpleNamespace(specification=spec, specification_hash=stable_hash(spec))
    with pytest.raises(HTTPException) as error:
        checked_spec(cohort)
    assert error.value.status_code == 409


@pytest.mark.parametrize(
    "fields",
    [
        {"brandTargetIds": []},
        {"entityIds": [ENTITY]},
        {"comparisonMode": "other"},
        {"brandTargetIds": [OWN, OWN]},
        {"promptHashes": ["not-a-hash"]},
    ],
)
def test_comparison_mode_and_scope_are_explicit_and_strict(fields):
    with pytest.raises(ValidationError):
        CohortRequest(
            **{
                "baselineRunId": "a" * 32,
                "title": "单轮描述复测方案",
                "comparisonMode": "descriptive_v1",
                "brandTargetIds": [OWN],
                "idempotencyKey": "strict-scope-123",
                **fields,
            }
        )


@pytest.mark.asyncio
async def test_frozen_modes_have_distinct_results_single_retest_slots_and_stale_deltas_are_removed(
    db_session, monkeypatch
):
    inferential, baseline, version, _ = await fixture_cohort(db_session)
    sample = await db_session.scalar(
        select(AuditSample).where(AuditSample.audit_id == baseline.audit_id)
    )
    _spec, before, after = descriptive_fixture()
    for snapshot, sid in ((before, sample.id), (after, "followup-sample")):
        response = snapshot["responses"][0]
        response.update(
            sample_id=sid, prompt_version_id=version.id, prompt_hash=version.text_hash
        )
        for observation in snapshot["brand_presence"]["observations"]:
            observation["sample_id"] = sid
        snapshot["evaluation_cohort"]["cells"] = [
            {
                "platform": "qwen",
                "prompt_hash": version.text_hash,
                "primary_scenario": "implementation",
                "eligibility": "eligible",
            }
        ]
    baseline.summary = {"brand_presence": before["brand_presence"]}
    baseline.manifest = {
        "responses": before["responses"],
        "evaluation_cohort": before["evaluation_cohort"],
        "brands": [],
    }
    request = CohortRequest(
        baselineRunId=baseline.id,
        title="单轮品牌维度观察",
        comparisonMode="descriptive_v1",
        brandTargetIds=[OWN, RIVAL],
        idempotencyKey="db-descriptive-scope",
    )
    cohort = await create_cohort(db_session, "p1", request, "owner")
    assert cohort.specification["metric_version"] == VERSION
    assert cohort.specification_hash != inferential.specification_hash
    assert (await create_cohort(db_session, "p1", request, "owner")).id == cohort.id
    retest = await start_retest(
        db_session, cohort, RetestRequest(idempotencyKey="single-retest-123"), "owner"
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AuditSample)
            .where(AuditSample.audit_id == retest.audit_id)
        )
        == 1
    )
    audit = await fixture_audit(db_session)
    followup = IntelligenceRun(
        project_id="p1",
        audit_id=audit.id,
        revision=1,
        status="partial",
        input_hash="f" * 64,
        signature=baseline.signature,
        summary={"brand_presence": after["brand_presence"]},
        manifest={
            "responses": after["responses"],
            "evaluation_cohort": after["evaluation_cohort"],
        },
    )
    db_session.add(followup)
    await db_session.flush()
    before["signature"] = after["signature"] = baseline.signature

    async def snapshot(_db, run):
        return before if run.id == baseline.id else after

    monkeypatch.setattr("app.intelligence.measurement.run_snapshot", snapshot)
    result = await compare_cohort(
        db_session, cohort, CompareRequest(followupRunId=followup.id), "owner"
    )
    assert (
        await compare_cohort(
            db_session, cohort, CompareRequest(followupRunId=followup.id), "owner"
        )
    ).id == result.id
    assert first_metric(result.result)["observed_delta"] == 1
    await compare_cohort(
        db_session, inferential, CompareRequest(followupRunId=followup.id), "owner"
    )
    assert (
        await db_session.scalar(select(func.count()).select_from(MeasurementResult))
        == 2
    )
    for run in (baseline, followup):
        db_session.add(
            AuditIntelligenceState(
                project_id="p1", audit_id=run.audit_id, published_run_id=run.id
            )
        )
    monkeypatch.setattr(settings, "intelligence_enabled", True)
    monkeypatch.setattr(
        "app.intelligence.measurement_history.stamp",
        AsyncMock(return_value=("stamp", True)),
    )
    await db_session.commit()
    saved = await measurement_history(db_session, "p1", cohort.id)
    assert saved["results"][0]["snapshot_current"]
    assert first_metric(saved["results"][0])["observed_delta"] == 1

    async def current_followup_only(_db, run, _state):
        return "stamp", run.id == followup.id

    monkeypatch.setattr(
        "app.intelligence.measurement_history.stamp", current_followup_only
    )
    first_appearance = await measurement_history(db_session, "p1", cohort.id)
    assert not first_appearance["results"][0]["baseline_snapshot_current"]
    assert first_appearance["results"][0]["snapshot_current"]
    assert first_metric(first_appearance["results"][0])["observed_delta"] == 1
    db_session.add(
        IntelligenceRun(
            project_id="p1",
            audit_id=followup.audit_id,
            revision=2,
            status="queued",
            input_hash="n" * 64,
            signature=followup.signature,
            manifest={},
        )
    )
    await db_session.flush()
    history = await measurement_history(db_session, "p1", cohort.id)
    assert not history["results"][0]["snapshot_current"]
    assert first_metric(history["results"][0])["observed_delta"] is None
    assert first_metric(result.result)["observed_delta"] == 1
