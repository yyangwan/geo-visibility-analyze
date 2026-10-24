from copy import deepcopy

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select

from app.api.auth import get_current_user
from app.config import settings
from app.intelligence.brand_evaluation import freeze_cohort
from app.intelligence.evaluation_reviews import (
    CohortReviewRequest,
    review_cohort,
    reviewed_cohort,
)
from app.intelligence.ingestion import prepare_run
from app.intelligence.models import AnalysisJob
from app.intelligence.project_context import ContextRequest, preference_projection
from app.intelligence.question_context import (
    SCENARIOS,
    QuestionReview,
    question_proposal,
    scenario_projection,
)
from app.main import app
from tests.test_intelligence_brand_presence import fixture, projection
from tests.test_intelligence_sampling import new_audit


def context(**changes):
    return QuestionReview(
        status="confirmed",
        primaryScenario="cost",
        secondaryScenarios=["implementation"],
        brandScope="neutral",
        **changes,
    )


def test_literal_suggestions_keep_verbatim_unicode_and_never_confirm_scope():
    text = "🧭预算有限，如何比较门店实施成本与功能？"
    suggestion = question_proposal(text, [])
    assert suggestion["review"] is None
    assert {row["scenario"] for row in suggestion["scenarios"]} >= {
        "cost",
        "features",
        "implementation",
    }
    for row in suggestion["scenarios"]:
        for span in row["evidence"]:
            assert text[span["start"] : span["end"]] == span["quote"]
    assert len(SCENARIOS) == 8


def test_brand_screen_preserves_ambiguity_and_latin_word_boundaries():
    def cohort(text, name):
        return freeze_cohort(
            [
                {
                    "sample_id": "s",
                    "sample_index": 1,
                    "platform": "kimi",
                    "prompt_hash": "a" * 64,
                    "prompt": text,
                    "category": "recommend",
                }
            ],
            [{"name": name}],
        )["cells"][0]

    assert cohort("推荐Kimi品牌如何使用", "Kimi")["eligibility"] == "excluded"
    assert (
        cohort("skimitive有哪些含义", "Kimi")["eligibility_reason"]
        == "neutrality_not_verified"
    )
    assert (
        cohort("理想的方案如何选择", "理想")["eligibility_reason"]
        == "registered_name_ambiguous"
    )
    assert cohort("云衡品牌如何实施", "云衡")["eligibility"] == "excluded"
    assert cohort("苹果价格是多少", "苹果")["eligibility"] == "needs_review"


def test_reviewed_primary_scopes_do_not_duplicate_tags_or_fill_a_missing_platform():
    manifest, applications = fixture(platforms=("qwen", "deepseek"))
    cohort = manifest["evaluation_cohort"]
    original = deepcopy(cohort)
    prompts = {row["prompt_hash"] for row in cohort["cells"]}
    manifest["evaluation_cohort"] = reviewed_cohort(
        cohort,
        {key: "eligible" for key in prompts},
        {key: context() for key in prompts},
    )
    assert [row["weight"] for row in cohort["cells"]] == [
        row["weight"] for row in manifest["evaluation_cohort"]["cells"]
    ]
    assert cohort == original
    applications[1][0].status = "failed"
    result = scenario_projection(manifest, projection(manifest, applications))
    assert {row["scenario"] for row in result["scope_scores"]} == {"cost"}
    overall = next(row for row in result["scope_scores"] if row["platform"] == "all")
    assert (
        overall["total"]["score"] is None and overall["total"]["covered_weight"] == 0.5
    )
    assert (
        next(row for row in result["scope_scores"] if row["platform"] == "qwen")[
            "total"
        ]["score"]
        == 0
    )
    assert result["reviewed_questions"] == 1


def test_unreviewed_suggestions_have_no_scenario_score():
    manifest, applications = fixture()
    assert (
        scenario_projection(manifest, projection(manifest, applications))[
            "scope_scores"
        ]
        == []
    )


def test_unregistered_brand_direction_is_excluded_with_frozen_prompt_evidence():
    manifest, _ = fixture()
    cohort = manifest["evaluation_cohort"]
    for cell in cohort["cells"]:
        cell["prompt"] = "Peec AI 的价格是多少"
    review = QuestionReview(
        status="confirmed",
        primaryScenario="cost",
        brandScope="directed",
        brandReferences=[{"quote": "Peec AI", "start": 0, "end": 7}],
    )
    key = cohort["cells"][0]["prompt_hash"]
    with pytest.raises(HTTPException):
        reviewed_cohort(cohort, {key: "eligible"}, {key: review})
    result = reviewed_cohort(cohort, {key: "excluded"}, {key: review})
    assert result["status"] == "no_competitive_prompts"
    assert (
        result["cells"][0]["question_context"]["review"]["brand_references"][0]["quote"]
        == "Peec AI"
    )


@pytest.mark.parametrize(
    "span",
    [
        {"quote": "不存在的需求", "start": 0, "end": 6},
        {"quote": "预算", "start": 0, "end": 2},
    ],
)
def test_question_requirements_cannot_be_taken_from_an_answer_or_wrong_utf16_offset(
    span,
):
    manifest, _ = fixture(text="与问题不同的回答")
    cohort = manifest["evaluation_cohort"]
    for cell in cohort["cells"]:
        cell["prompt"] = "🧭预算与实施成本"
    key = cohort["cells"][0]["prompt_hash"]
    with pytest.raises(HTTPException):
        reviewed_cohort(cohort, {key: "eligible"}, {key: context(requirements=[span])})
    valid = context(requirements=[{"quote": "预算", "start": 1, "end": 3}])
    assert (
        reviewed_cohort(cohort, {key: "eligible"}, {key: valid})["cells"][0][
            "primary_scenario"
        ]
        == "cost"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"primaryScenario": "other"},
        {"primaryScenario": None},
        {"secondaryScenarios": ["cost"]},
        {"secondaryScenarios": ["features", "features"]},
        {"brandScope": "uncertain"},
    ],
)
def test_confirmed_context_requires_unambiguous_bounded_values(changes):
    data = {
        "status": "confirmed",
        "primaryScenario": "cost",
        "brandScope": "neutral",
        **changes,
    }
    with pytest.raises(ValidationError):
        QuestionReview.model_validate(data)


def test_neutral_defaults_and_reset_reason_are_kept_separate_from_score_weights():
    default = preference_projection(None)
    assert default["mode"] == "neutral" and set(default["importance"].values()) == {50}
    reset = ContextRequest(
        version=1,
        brands=[],
        business_preferences={
            "version": "business-preferences-v1",
            "scenario_importance": {},
            "reason": "恢复默认等权排序",
        },
    )
    assert (
        preference_projection(
            {
                "version": 1,
                "content_hash": "hash",
                "business_preferences": reset.business_preferences,
            }
        )["mode"]
        == "neutral"
    )
    for bad in ({"unknown": 50}, {"cost": True}, {"cost": 0}):
        with pytest.raises(ValidationError):
            ContextRequest(
                version=1,
                brands=[],
                business_preferences={
                    "version": "business-preferences-v1",
                    "scenario_importance": bad,
                    "reason": "明确的业务设置理由",
                },
            )


@pytest.mark.asyncio
async def test_review_context_is_idempotent_and_frozen_only_in_new_revision(db_session):
    audit, _, _ = await new_audit(db_session, rounds=1)
    run = await prepare_run(db_session, "p1", audit.id)
    await db_session.commit()
    original = deepcopy(run.manifest)
    base = run.manifest["evaluation_base_cohort"]
    keys = {row["prompt_hash"] for row in base["cells"]}
    data = CohortReviewRequest(
        baseHash=base["hash"],
        decisions={key: "eligible" for key in keys},
        contexts={key: context() for key in keys},
        reason="按实际问题确认成本主场景",
        expectedVersion=0,
        idempotencyKey="context-review-123",
    )
    first = await review_cohort(db_session, run, data, "reviewer")
    await db_session.commit()
    assert (await review_cohort(db_session, run, data, "reviewer")).id == first.id
    next_run = await prepare_run(db_session, "p1", audit.id)
    assert next_run.id != run.id and run.manifest == original
    assert (
        next_run.manifest["evaluation_cohort"]["cells"][0]["primary_scenario"] == "cost"
    )


@pytest.mark.asyncio
async def test_scenario_read_is_project_scoped_and_never_mutates_run_or_jobs(
    client, db_session, monkeypatch
):
    monkeypatch.setattr(settings, "intelligence_enabled", True)
    audit, _, _ = await new_audit(db_session, rounds=1)
    run = await prepare_run(db_session, "p1", audit.id)
    run.status = "failed"
    await db_session.commit()
    original = deepcopy(run.summary)
    count = await db_session.scalar(select(func.count()).select_from(AnalysisJob))
    user = {"scope": "project", "pid": "p2", "role": "viewer", "sub": "reader"}
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        assert (
            await client.get(f"/api/intelligence/runs/{run.id}/scenarios")
        ).status_code == 404
        user["pid"] = "p1"
        response = await client.get(f"/api/intelligence/runs/{run.id}/scenarios")
        assert response.status_code == 200 and len(response.json()["taxonomy"]) == 8
        assert response.json()["reviewed_questions"] == 0
        detail = await client.get(f"/api/intelligence/runs/{run.id}")
        assert (
            detail.status_code == 200
            and "scenario_comparison" in detail.json()["summary"]
        )
        assert run.summary == original
        assert (
            await db_session.scalar(select(func.count()).select_from(AnalysisJob))
            == count
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
