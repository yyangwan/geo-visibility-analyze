from fractions import Fraction
from types import SimpleNamespace

import pytest

from app.intelligence.brand_evaluation import (
    aggregate_scores,
    freeze_cohort,
    score_observation,
    summarize_answer_scores,
)


def cell(id, platform="qwen", category="recommend", prompt="有哪些方案", index=1):
    return {
        "sample_id": id,
        "sample_index": index,
        "platform": platform,
        "category": category,
        "prompt": prompt,
        "prompt_hash": prompt,
    }


def test_weights_freeze_platform_category_and_prompt_not_success_count():
    cohort = freeze_cohort(
        [
            cell("a"),
            cell("b", prompt="推荐哪些产品"),
            cell("c", category="scenario", prompt="企业场景"),
            cell("d", platform="deepseek"),
        ],
        [],
    )
    weights = {c["sample_id"]: Fraction(**c["weight"]) for c in cohort["cells"]}
    assert weights == {
        "a": Fraction(1, 8),
        "b": Fraction(1, 8),
        "c": Fraction(1, 4),
        "d": Fraction(1, 2),
    }
    assert aggregate_scores(cohort, {"a": 100, "b": 100, "c": 100}) == {
        "rule_version": "brand-visibility-v1",
        "cohort_hash": cohort["hash"],
        "score": None,
        "covered_weight": 0.5,
        "missing_sample_ids": ["d"],
        "status": "pending",
    }


def test_duplicate_rounds_do_not_increase_weight_and_named_prompts_are_excluded():
    cohort = freeze_cohort(
        [cell("a"), cell("b", index=2), cell("c", prompt="云衡如何")],
        [{"name": "云衡"}],
    )
    assert len(cohort["cells"]) == 2
    assert (
        next(c for c in cohort["cells"] if c["sample_id"] == "c")["weight"]["numerator"]
        == 0
    )
    assert (
        aggregate_scores(cohort, {"a": 100})["score"] is None
    )  # Neutrality still requires review.
    assert (
        freeze_cohort(
            [cell("b", index=2), cell("a"), cell("c", prompt="云衡如何")],
            [{"name": "云衡"}],
        )["hash"]
        == cohort["hash"]
    )


def test_fixed_rubric_missing_is_not_zero_and_boolean_is_not_a_rating():
    base = {
        "complete": True,
        "identity_confirmed": True,
        "present": True,
        "recommendation": 2,
        "rank": 2,
        "list_size": 3,
        "reasons": "generic",
        "evidence_ids": ["e1"],
    }
    assert score_observation(**base)["score"] == 60
    assert score_observation(**{**base, "rank": None})["score"] is None
    assert score_observation(**{**base, "recommendation": True})["score"] is None
    assert score_observation(**{**base, "present": False})["score"] == 0
    assert (
        score_observation(**{**base, "complete": False, "present": False})["score"]
        is None
    )


def test_aggregate_requires_full_coverage_and_eligibility():
    cohort = freeze_cohort([cell("a"), cell("b", platform="deepseek")], [])
    for item in cohort["cells"]:
        item["eligibility"] = "eligible"
    assert aggregate_scores(cohort, {"a": 80, "b": 60, "unrelated": 100})["score"] == 70
    assert aggregate_scores(cohort, {"a": 80})["score"] is None
    cohort["cells"][0]["weight"]["denominator"] = 10
    with pytest.raises(ValueError):
        aggregate_scores(cohort, {"a": 80, "b": 60})


def test_whole_answer_conflicts_and_missing_tail_are_unknown():
    manifest = {
        "responses": [{"id": "r", "platform": "qwen", "text": "云衡", "error": False}]
    }
    application = SimpleNamespace(status="completed")
    segment = SimpleNamespace(
        owner_kind="answer", owner_id="r", start_offset=0, end_offset=2
    )
    cache = SimpleNamespace(result={"brand_signals": []})
    entities = [{"id": "e", "kind": "brand", "identity_status": "confirmed"}]
    mentions = [
        {
            "owner_kind": "answer",
            "owner_id": "r",
            "entity_id": "e",
            "identity_ambiguity": False,
        }
    ]
    signal = {
        "id": "s1",
        "response_id": "r",
        "entity_id": "e",
        "recommendation": 4,
        "reason_level": "specific",
        "explicit_rank": 1,
    }
    signals = {"observations": [signal, {**signal, "id": "s2", "recommendation": 0}]}
    rows = [(application, segment, cache)]
    score = summarize_answer_scores(manifest, rows, entities, mentions, signals)[
        "answer_scores"
    ][0]
    assert score["conflicting_fields"] == ["recommendation"]
    assert score["dimensions"]["recommendation"] is None
    assert score["dimensions"]["reasons"] == 75
    assert score["score"] is None  # No validated full recommendation list.
    segment.end_offset = 1
    incomplete = summarize_answer_scores(manifest, rows, entities, mentions, signals)[
        "answer_scores"
    ][0]
    assert incomplete["status"] == "partial"
    assert incomplete["score"] is None
    assert (
        incomplete["dimensions"]["presence"] == 100
    )  # Validated positive evidence survives a missing tail.
    assert incomplete["dimensions"]["reasons"] is None


@pytest.mark.asyncio
async def test_planned_cells_persist_before_responses_and_ignore_later_prompt_edits(
    db_session,
):
    from app.intelligence.ingestion import prepare_run
    from tests.test_intelligence_sampling import new_audit

    audit, prompt, _ = await new_audit(db_session, rounds=1)
    run = await prepare_run(db_session, "p1", audit.id)
    await db_session.commit()
    cohort = run.manifest["evaluation_cohort"]
    assert len(cohort["cells"]) == 1
    assert run.manifest["responses"] == []
    prompt.text = "后来改变的提示词"
    audit.brands_json = [{"name": "完整提示词"}]
    await db_session.commit()
    later = await prepare_run(db_session, "p1", audit.id)
    assert later.manifest["evaluation_cohort"] == cohort
