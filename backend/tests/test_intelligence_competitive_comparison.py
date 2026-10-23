from copy import deepcopy

from app.intelligence.brand_evaluation import freeze_cohort
from app.intelligence.competitive_comparison import competitive_breakdown
from app.intelligence.evaluation_reviews import reviewed_cohort


def fixture():
    cells = [
        {
            "sample_id": platform,
            "sample_index": 1,
            "platform": platform,
            "category": "recommend",
            "prompt": "企业如何选型",
            "prompt_hash": "prompt1",
        }
        for platform in ("qwen", "deepseek")
    ]
    cohort = reviewed_cohort(freeze_cohort(cells, []), {"prompt1": "eligible"})
    entities = [
        {
            "id": "own",
            "name": "自有品牌",
            "kind": "brand",
            "role": "own",
            "identity_status": "confirmed",
        },
        {
            "id": "new",
            "name": "新发现品牌",
            "kind": "brand",
            "role": "unregistered",
            "identity_status": "confirmed",
        },
    ]
    rows = [
        {
            "entity_id": entity,
            "sample_id": platform,
            "response_id": platform + "-answer",
            "score": score,
            "dimensions": {
                "presence": score,
                "recommendation": score,
                "position": score,
                "reasons": score,
            },
            "evidence_ids": [platform + "-" + entity],
        }
        for platform in ("qwen", "deepseek")
        for entity, score in (("own", 20), ("new", 80))
    ]
    return cohort, entities, rows


def test_new_brands_are_compared_with_evidence_and_platform_specific_tasks():
    cohort, entities, rows = fixture()
    result = competitive_breakdown(cohort, rows, entities)
    assert len(result["gaps"]) == 8
    gap = next(item for item in result["gaps"] if item["platform"] == "qwen")
    assert gap["gap_points"] == 60
    assert gap["comparison_entity_id"] == "new"
    assert gap["evidence_ids"] == ["qwen-new", "qwen-own"]
    assert gap["supporting_sample_ids"] == ["qwen"]
    assert gap["cause_status"] == "not_established"
    assert gap["action"]["target_prompts"] == ["企业如何选型"]
    assert gap["action"]["retest"]["platform"] == "qwen"


def test_missing_platform_does_not_change_other_platform_or_publish_false_gap():
    cohort, entities, rows = fixture()
    rows = [row for row in rows if row["sample_id"] != "deepseek"]
    result = competitive_breakdown(cohort, rows, entities)
    assert {gap["platform"] for gap in result["gaps"]} == {"qwen"}
    deepseek = [
        score
        for score in result["scope_scores"]
        if score["axis"] == "platform" and score["scope"] == "deepseek"
    ]
    assert all(score["total"]["score"] is None for score in deepseek)
    overall = [score for score in result["scope_scores"] if score["axis"] == "overall"]
    assert len(overall) == 2
    assert all(score["total"]["score"] is None and score["total"]["covered_weight"] == .5 for score in overall)
    category = [
        score for score in result["scope_scores"] if score["axis"] == "category"
    ]
    assert all(
        score["total"]["score"] is None and score["total"]["covered_weight"] == 0.5
        for score in category
    )


def test_incomplete_dimension_no_gap_but_known_dimensions_still_compare():
    cohort, entities, rows = fixture()
    rows[0]["dimensions"]["position"] = None
    rows[0]["score"] = None
    result = competitive_breakdown(cohort, rows, entities)
    assert not any(
        gap["platform"] == "qwen" and gap["dimension"] == "position"
        for gap in result["gaps"]
    )
    assert any(
        gap["platform"] == "qwen" and gap["dimension"] == "reasons"
        for gap in result["gaps"]
    )


def test_unreviewed_cohort_unconfirmed_identity_and_duplicate_sample_cannot_create_tasks():
    cohort, entities, rows = fixture()
    pending = deepcopy(cohort)
    for cell in pending["cells"]:
        cell["eligibility"] = "needs_review"
    assert competitive_breakdown(pending, rows, entities)["gaps"] == []
    unknown = deepcopy(entities)
    unknown[1]["identity_status"] = "provisional"
    assert competitive_breakdown(cohort, rows, unknown)["gaps"] == []
    duplicate = rows + [deepcopy(rows[0])]
    assert not any(
        gap["platform"] == "qwen"
        for gap in competitive_breakdown(cohort, duplicate, entities)["gaps"]
    )


def test_comparison_is_deterministic_and_does_not_mutate_frozen_weights():
    cohort, entities, rows = fixture()
    original = deepcopy(cohort)
    first = competitive_breakdown(cohort, rows, entities)
    second = competitive_breakdown(cohort, list(reversed(rows)), entities)
    assert first == second
    assert cohort == original
