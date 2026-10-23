from types import SimpleNamespace

import pytest

from app.intelligence.actions import PlanRequest, create_plan, persist_diagnoses
from app.intelligence.competitive_actions import competitive_diagnoses


def fixture():
    run = SimpleNamespace(
        id="run",
        manifest={
            "sources": [
                {
                    "id": "source",
                    "response_id": "r",
                    "platform": "qwen",
                    "kind": "answer_reference",
                    "snapshot_id": "snapshot",
                    "fetch_status": "captured",
                }
            ]
        },
    )
    gap = {
        "id": "gap",
        "dimension": "reasons",
        "platform": "qwen",
        "own_entity_id": "own",
        "comparison_entity_id": "other",
        "prompts": ["如何选型"],
        "evidence_ids": ["signal"],
        "response_ids": ["r"],
        "action": {
            "task": "补充选型依据",
            "materials_needed": ["案例"],
            "retest": {"success_check": "同题复测"},
        },
    }
    summary = {
        "brand_evaluation": {"comparison": {"gaps": [gap]}},
        "brand_signals": {
            "observations": [{"id": "signal", "mention_ids": ["answer-mention"]}]
        },
        "mentions": [
            {
                "id": "answer-mention",
                "entity_id": "other",
                "owner_kind": "answer",
                "owner_id": "r",
            },
            {
                "id": "source-mention",
                "entity_id": "other",
                "owner_kind": "snapshot",
                "owner_id": "snapshot",
            },
        ],
    }
    return run, summary


def test_action_bridge_uses_durable_mentions_and_preserves_signal_provenance():
    run, summary = fixture()
    result = competitive_diagnoses(run, summary)
    gap = next(item for item in result if item["kind"] == "competitive_reasons")
    assert gap["evidence_ids"] == ["answer-mention"]
    assert gap["signal_evidence_ids"] == ["signal"]
    assert gap["action"]["success_check"] == "同题复测"
    source = next(
        item for item in result if item["kind"] == "competitor_source_evidence"
    )
    assert source["evidence_ids"] == ["source-mention"]
    assert source["source_ids"] == ["source"]
    assert source["cause_status"] == "not_established"


@pytest.mark.parametrize(
    "field,value",
    [
        ("platform", "deepseek"),
        ("kind", "search_candidate"),
        ("snapshot_id", None),
        ("fetch_status", "blocked"),
    ],
)
def test_unrelated_or_missing_sources_cannot_create_channel_diagnosis(field, value):
    run, summary = fixture()
    run.manifest["sources"][0][field] = value
    assert len(competitive_diagnoses(run, summary)) == 1


@pytest.mark.asyncio
async def test_competitive_diagnosis_can_create_idempotent_existing_action_plan(
    db_session,
):
    from app.intelligence.ingestion import prepare_run
    from tests.test_intelligence_foundation import fixture_audit

    audit = await fixture_audit(db_session)
    run = await prepare_run(db_session, "p1", audit.id)
    _, summary = fixture()
    summary["diagnoses"] = competitive_diagnoses(run, summary)
    await persist_diagnoses(db_session, run, summary)
    assert summary["diagnoses"][0]["record_id"]
    request = PlanRequest(
        diagnosisId=summary["diagnoses"][0]["record_id"],
        idempotencyKey="competitive-plan-123",
    )
    plan = await create_plan(db_session, run, request, "content-owner")
    await db_session.commit()
    assert (await create_plan(db_session, run, request, "content-owner")).id == plan.id
    assert plan.target["signal_evidence_ids"] == ["signal"]
