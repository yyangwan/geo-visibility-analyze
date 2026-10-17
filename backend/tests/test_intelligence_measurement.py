import copy
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.api.auth import get_current_user
from app.config import settings
from app.intelligence.blob import digest
from app.intelligence.measurement import (
    AssetRequest,
    CohortRequest,
    CompareRequest,
    RetestRequest,
    checked_spec,
    compare_cohort,
    compare_snapshots,
    create_cohort,
    record_asset,
    run_snapshot,
    start_retest,
)
from app.intelligence.models import (
    AuditSample,
    ContentSegment,
    IntelligenceRun,
    MeasurementCohort,
    PromptVersion,
    SegmentAnalysis,
)
from app.intelligence.semantics import stable_hash
from app.main import app
from app.models.device_gateway import DeviceTask
from tests.test_intelligence_foundation import fixture_audit

ENTITY = "e" * 32
CONDITIONS = {
    "searchMode": "enabled",
    "modelVersion": "observed-v1",
    "accountClass": "standard",
    "location": "CN",
}


def snapshots(prompts=20, rounds=3, platforms=("qwen",)):
    slots, before, after = [], [], []
    for platform in platforms:
        for prompt in range(prompts):
            for index in range(1, rounds + 1):
                base_id = f"{platform}-{prompt}-{index}"
                slots.append(
                    {
                        "sample_id": base_id,
                        "prompt_version_id": str(prompt),
                        "prompt_hash": str(prompt),
                        "platform": platform,
                        "surface": "app",
                        "sample_index": index,
                    }
                )
                response = {
                    "id": base_id,
                    "sample_id": base_id,
                    "prompt_version_id": str(prompt),
                    "prompt_hash": str(prompt),
                    "platform": platform,
                    "surface": "app",
                    "sample_index": index,
                    "conditions": CONDITIONS.copy(),
                    "answer_status": "complete",
                    "error": False,
                    "observed_at": "2026-10-13T00:00:00Z",
                    "text": "品牌原文",
                }
                before.append(response)
                after.append(
                    {
                        **copy.deepcopy(response),
                        "id": f"next-{base_id}",
                        "sample_id": f"next-{base_id}",
                        "observed_at": "2026-10-14T00:00:00Z",
                    }
                )
    spec = {
        "slots": slots,
        "entity_ids": [ENTITY],
        "identity_context": {ENTITY: {}},
        "analysis_signature": "signature",
    }
    baseline = {
        "responses": before,
        "completed": {row["id"] for row in before},
        "hits": {},
        "signature": "signature",
        "identity_context": {},
    }
    followup = {
        "responses": after,
        "completed": {row["id"] for row in after},
        "hits": {ENTITY: {row["id"] for row in after}},
        "signature": "signature",
        "identity_context": {},
    }
    return spec, baseline, followup


def test_distinct_answers_and_paired_prompt_cluster_interval_are_platform_specific():
    spec, before, after = snapshots(platforms=("qwen", "deepseek"))
    before["hits"][ENTITY] = {
        row["id"] for row in before["responses"] if row["platform"] == "deepseek"
    }
    after["hits"][ENTITY] = {
        row["id"] for row in after["responses"] if row["platform"] == "qwen"
    }
    result = compare_snapshots(spec, before, after)
    platforms = {row["platform"]: row for row in result["platforms"]}
    assert platforms["qwen"]["metrics"][0]["difference"] == 1
    assert platforms["qwen"]["metrics"][0]["interval95"] == [1, 1]
    assert platforms["deepseek"]["metrics"][0]["difference"] == -1
    assert platforms["deepseek"]["metrics"][0]["baseline"]["denominator"] == 60
    assert (
        not result["causal_claim_allowed"]
        and result["business_metrics"]["roi"] == "unknown"
    )
    assert result == compare_snapshots(spec, before, after)


@pytest.mark.parametrize(
    "change,reason",
    [
        ("unknown", "unknown_conditions"),
        ("conditions", "conditions_changed"),
        ("signature", "analysis_signature_changed"),
        ("identity", "entity_mapping_changed"),
        ("partial", "incomplete_answer"),
        ("coverage", "incomplete_analysis"),
        ("time", "observation_not_later"),
    ],
)
def test_uncomparable_inputs_never_produce_a_change_estimate(change, reason):
    spec, before, after = snapshots()
    for row in after["responses"]:
        if change == "unknown":
            row["conditions"]["modelVersion"] = None
        if change == "conditions":
            row["conditions"]["location"] = "other"
        if change == "partial":
            row["answer_status"] = "partial"
        if change == "time":
            row["observed_at"] = "2026-10-12T00:00:00Z"
    if change == "signature":
        after["signature"] = "changed"
    if change == "identity":
        after["identity_context"] = {ENTITY: {"decision": "confirmed"}}
    if change == "coverage":
        after["completed"] = set()
    result = compare_snapshots(spec, before, after)["platforms"][0]
    assert result["status"] in ("descriptive_only", "undetermined")
    assert (
        result["metrics"][0]["difference"] is None
        and result["metrics"][0]["interval95"] is None
    )
    assert {item["reason"] for item in result["excluded"]} == {reason}


@pytest.mark.parametrize("prompts,rounds", [(19, 3), (20, 2)])
def test_insufficient_clusters_or_rounds_do_not_emit_effects(prompts, rounds):
    spec, before, after = snapshots(prompts, rounds)
    result = compare_snapshots(spec, before, after)["platforms"][0]
    assert (
        result["status"] == "insufficient"
        and result["metrics"][0]["difference"] is None
    )


def test_missing_and_duplicate_samples_are_traced_and_extra_prompts_never_expand_the_cohort():
    spec, before, after = snapshots(1, 4)
    after["responses"].pop()
    after["responses"].append(copy.deepcopy(after["responses"][0]))
    after["responses"].append(
        {**after["responses"][0], "id": "extra", "prompt_version_id": "other"}
    )
    result = compare_snapshots(spec, before, after)["platforms"][0]
    assert {"missing_followup", "ambiguous_sample", "insufficient_rounds"} == {
        row["reason"] for row in result["excluded"]
    }
    assert result["out_of_cohort"] == [
        {"response_id": "extra", "reason": "outside_frozen_cohort"}
    ]
    assert not result["included"]


async def fixture_cohort(db):
    audit = await fixture_audit(db)
    text = "冻结原提示词"
    version = PromptVersion(
        project_id="p1",
        prompt_id=1,
        text=text,
        text_hash=digest(text.encode()),
        category="custom",
    )
    db.add(version)
    await db.flush()
    sample = AuditSample(
        project_id="p1",
        audit_id=audit.id,
        prompt_version_id=version.id,
        platform="qwen",
        surface="app",
        sample_index=1,
        conditions=CONDITIONS,
        condition_hash=stable_hash(CONDITIONS),
        status="sealed",
    )
    db.add(sample)
    await db.flush()
    run = IntelligenceRun(
        project_id="p1",
        audit_id=audit.id,
        revision=1,
        signature="s" * 64,
        input_hash="i" * 64,
        status="partial",
        manifest={"responses": [], "brands": []},
        summary={
            "entities": [
                {"id": ENTITY, "name": "未登记品牌", "identity_status": "provisional"}
            ]
        },
    )
    db.add(run)
    await db.flush()
    request = CohortRequest(
        baselineRunId=run.id,
        title="固定问题复测方案",
        entityIds=[ENTITY],
        idempotencyKey="cohort-key-123",
    )
    cohort = await create_cohort(db, "p1", request, "user")
    await db.commit()
    return cohort, run, version, request


@pytest.mark.asyncio
async def test_cohort_freezes_all_entities_and_retest_uses_old_prompt_versions(
    db_session,
):
    cohort, run, version, request = await fixture_cohort(db_session)
    assert (await create_cohort(db_session, "p1", request, "user")).id == cohort.id
    retest = await start_retest(
        db_session, cohort, RetestRequest(idempotencyKey="retest-key-123"), "user"
    )
    await db_session.commit()
    replay = await start_retest(
        db_session, cohort, RetestRequest(idempotencyKey="retest-key-123"), "user"
    )
    assert retest.id == replay.id and retest.audit_id != run.audit_id
    task = await db_session.scalar(
        select(DeviceTask).where(DeviceTask.project_id == "p1")
    )
    assert (
        task.payload["prompt"] == "冻结原提示词"
        and task.payload["prompt_version_id"] == version.id
    )
    assert cohort.specification["entities"][0]["identity_status"] == "provisional"
    version.text = "篡改的旧版本"
    with pytest.raises(HTTPException) as error:
        await start_retest(
            db_session, cohort, RetestRequest(idempotencyKey="retest-key-456"), "user"
        )
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_changed_spec_same_audit_revision_and_cross_project_are_rejected(
    db_session,
):
    cohort, run, _, request = await fixture_cohort(db_session)
    with pytest.raises(HTTPException):
        await create_cohort(db_session, "p2", request, "user")
    with pytest.raises(HTTPException) as error:
        await compare_cohort(
            db_session, cohort, CompareRequest(followupRunId=run.id), "user"
        )
    assert error.value.status_code == 422
    cohort.specification = {**cohort.specification, "entity_ids": []}
    with pytest.raises(HTTPException) as error:
        checked_spec(cohort)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_only_full_answer_analysis_counts_absence_and_source_mentions_are_separate(
    db_session,
):
    _, run, _, _ = await fixture_cohort(db_session)
    run.manifest = {"responses": [{"id": "answer", "text": "完整回答"}]}
    run.summary = {
        "mentions": [
            {"entity_id": ENTITY, "owner_kind": "snapshot", "owner_id": "article"}
        ]
    }
    segment = ContentSegment(
        project_id="p1",
        owner_kind="answer",
        owner_id="answer",
        ordinal=0,
        start_offset=0,
        end_offset=2,
        text="完整",
        segment_hash="h" * 64,
        extractor_version="v1",
    )
    db_session.add(segment)
    await db_session.flush()
    db_session.add(
        SegmentAnalysis(
            project_id="p1", run_id=run.id, segment_id=segment.id, status="completed"
        )
    )
    await db_session.flush()
    snapshot = await run_snapshot(db_session, run)
    assert not snapshot["completed"] and not snapshot["hits"]
    segment.text, segment.end_offset = "完整回答", 4
    await db_session.flush()
    assert (await run_snapshot(db_session, run))["completed"] == {"answer"}
    run.summary = {
        "mentions": [
            {
                "entity_id": ENTITY,
                "owner_kind": "answer",
                "owner_id": "answer",
                "identity_ambiguity": True,
            }
        ]
    }
    ambiguous = await run_snapshot(db_session, run)
    assert not ambiguous["completed"]
    assert ambiguous["ambiguous_identity"] == {"answer"}
    from app.intelligence.measurement import usable

    assert (
        usable({"id": "answer", "answer_status": "complete"}, ambiguous)
        == "ambiguous_identity"
    )
    run.summary = {
        "mentions": [
            {
                "entity_id": ENTITY,
                "owner_kind": "snapshot",
                "owner_id": "article",
                "identity_ambiguity": True,
            }
        ]
    }
    assert (await run_snapshot(db_session, run))["completed"] == {"answer"}


@pytest.mark.asyncio
async def test_measurement_api_blocks_viewer_writes_and_cross_project_reads(
    client, db_session, monkeypatch
):
    cohort, _run, _, _ = await fixture_cohort(db_session)
    monkeypatch.setattr(settings, "intelligence_enabled", True)
    app.dependency_overrides[get_current_user] = lambda: {
        "scope": "project",
        "pid": "p1",
        "role": "viewer",
        "sub": "viewer",
    }
    try:
        response = await client.post(
            f"/api/intelligence/cohorts/{cohort.id}/retests",
            json={"idempotencyKey": "api-retest-key"},
        )
        assert response.status_code == 403
        app.dependency_overrides[get_current_user] = lambda: {
            "scope": "project",
            "pid": "p2",
            "role": "owner",
            "sub": "owner",
        }
        assert (
            await client.get(f"/api/intelligence/cohorts/{cohort.id}/results")
        ).status_code == 404
        assert (
            await client.get("/api/intelligence/projects/p1/cohorts")
        ).status_code == 403
        assert (await db_session.scalars(select(MeasurementCohort))).all() == [cohort]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_publication_asset_is_human_reported_and_has_no_publish_side_effect(
    db_session,
):
    from tests.test_intelligence_actions import fixture_plan

    _, action, _ = await fixture_plan(db_session)
    request = AssetRequest(
        actionId=action.id,
        url="https://example.com/content",
        channel="website",
        contentHash="a" * 64,
        publishedAt=datetime(2026, 1, 1, tzinfo=UTC),
        reason="负责人核对已发布正文",
        idempotencyKey="asset-key-123",
    )
    asset = await record_asset(db_session, "p1", request, "user")
    assert (await record_asset(db_session, "p1", request, "user")).id == asset.id
    with pytest.raises(HTTPException):
        await record_asset(db_session, "p2", request, "user")
    with pytest.raises(ValidationError):
        AssetRequest.model_validate(
            {**request.model_dump(by_alias=True), "url": "javascript:alert(1)"}
        )
    with pytest.raises(ValidationError):
        AssetRequest.model_validate(
            {**request.model_dump(by_alias=True), "publishedAt": "2026-01-01T00:00:00"}
        )


@pytest.mark.asyncio
async def test_comparison_persists_immutable_results_with_both_run_inputs(db_session):
    cohort, baseline, version, _ = await fixture_cohort(db_session)
    audit = await fixture_audit(db_session)
    followup = IntelligenceRun(
        project_id="p1",
        audit_id=audit.id,
        revision=1,
        input_hash="f" * 64,
        signature=baseline.signature,
        manifest={},
        summary={},
        status="partial",
    )
    db_session.add(followup)
    await db_session.flush()
    for phase, run in (("before", baseline), ("after", followup)):
        response = {
            "id": phase,
            "text": "完整回答",
            "prompt_version_id": version.id,
            "prompt_hash": version.text_hash,
            "platform": "qwen",
            "surface": "app",
            "sample_index": 1,
            "conditions": CONDITIONS,
            "answer_status": "complete",
            "error": False,
            "observed_at": "2026-10-13T00:00:00Z"
            if phase == "before"
            else "2026-10-14T00:00:00Z",
        }
        run.manifest = {"responses": [response]}
        run.summary = {
            "mentions": [
                {"entity_id": ENTITY, "owner_kind": "answer", "owner_id": phase}
            ]
            * 2
            if phase == "after"
            else []
        }
        segment = ContentSegment(
            project_id="p1",
            owner_kind="answer",
            owner_id=phase,
            ordinal=0,
            start_offset=0,
            end_offset=4,
            text="完整回答",
            segment_hash=digest("完整回答".encode()),
            extractor_version="v1",
        )
        db_session.add(segment)
        await db_session.flush()
        db_session.add(
            SegmentAnalysis(
                project_id="p1",
                run_id=run.id,
                segment_id=segment.id,
                status="completed",
            )
        )
    await db_session.flush()
    request = CompareRequest(followupRunId=followup.id)
    result = await compare_cohort(db_session, cohort, request, "user")
    await db_session.commit()
    assert (
        result.result["baseline_input_hash"] == baseline.input_hash
        and result.result["followup_input_hash"] == followup.input_hash
    )
    metric = result.result["platforms"][0]["metrics"][0]
    assert metric["descriptive"]["followup"] == {"numerator": 1, "denominator": 1}
    assert metric["difference"] is None
    original = copy.deepcopy(result.result)
    followup.summary = {"mentions": []}
    replay = await compare_cohort(db_session, cohort, request, "user")
    assert replay.id == result.id and replay.result == original


def test_reused_response_ids_cannot_inflate_independent_sampling():
    spec, before, after = snapshots(1, 3)
    after["responses"][1]["id"] = after["responses"][0]["id"]
    result = compare_snapshots(spec, before, after)["platforms"][0]
    assert sum(item["reason"] == "ambiguous_sample" for item in result["excluded"]) == 2
    assert not result["included"]
