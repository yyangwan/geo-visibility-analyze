from copy import deepcopy

import pytest

from app.intelligence.evaluation import (
    Annotation,
    EvaluationInvalid,
    Prediction,
    evaluate,
    freeze_case,
)
from app.intelligence.semantics import stable_hash


def entity_case(**updates):
    fields = {
        "group_id": stable_hash("same question"),
        "origin": "synthetic",
        "split": "holdout",
        "platform": "qwen",
        "signature": "a" * 64,
        "run_id": "run-1",
        "task": "entities",
        "owner_kind": "answer",
        "owner_id": "answer-1",
        "segment_id": "segment-1",
        "observation_id": "answer-1",
        "global_offset": 0,
        "text": "青禾可靠，云桥不稳定。",
        "prompt": "推荐哪些方案？",
        "source_kind": "answer",
        "content_status": "complete",
    }
    return freeze_case(**{**fields, **updates})


def mention(name="青禾", start=0, sentiment="positive", **updates):
    return {
        "name": name,
        "kind": "brand",
        "quote": name,
        "start": start,
        "end": start + len(name),
        "sentiment": sentiment,
        **updates,
    }


def prediction(case, **updates):
    return Prediction(
        case_id=case.case_id,
        input_hash=case.input_hash,
        signature=case.signature,
        status="completed",
        **updates,
    ).model_dump()


def labels(case, *, entities=None, relation=None, reviewers=("reader-a", "reader-b")):
    return [
        Annotation(
            case_id=case.case_id,
            input_hash=case.input_hash,
            reviewer_id=reviewer,
            status="reviewed",
            entities=entities,
            relation=relation,
        ).model_dump()
        for reviewer in reviewers
    ]


def relation_case(**updates):
    return entity_case(
        **{
            "task": "relation",
            "owner_kind": "snapshot",
            "owner_id": "snapshot-1",
            "source_kind": "answer_reference",
            "content_status": "captured",
            "text": "青禾仅支持一年的质保。",
            "answer_quote": "青禾支持三年的质保。",
            "evidence_quote": "仅支持一年的质保",
            "citation_eligible": True,
            **updates,
        }
    )


def test_exact_mentions_precision_recall_and_entity_specific_sentiment():
    case = entity_case()
    gold = [mention(), mention("云桥", 5, "negative")]
    predicted = [
        mention(sentiment="negative"),
        mention("云桥", 5, "negative"),
        mention("虚构", 0),
    ]
    result = evaluate(
        [case.model_dump()],
        [prediction(case, entities=predicted)],
        labels(case, entities=gold),
    )
    row = result["strata"][0]
    assert row["entity_precision"] == {"numerator": 2, "denominator": 3, "value": 2 / 3}
    assert row["entity_recall"]["value"] == 1
    assert row["matched_entity_sentiment_accuracy"]["value"] == 0.5
    assert row["invalid_prediction_spans"] == 1
    assert "repair_verbatim_spans_and_unicode_offsets" in row["review_priorities"]
    assert row["entity_by_kind"]["brand"]["precision"]["value"] == 2 / 3
    assert not result["release_eligible"]


def test_duplicate_predictions_are_false_positives_not_free_extra_votes():
    case = entity_case()
    result = evaluate(
        [case],
        [prediction(case, entities=[mention(sentiment="negative"), mention()])],
        labels(case, entities=[mention()]),
    )
    row = result["strata"][0]
    assert row["entity_counts"] == {
        "true_positive": 1,
        "false_positive": 1,
        "false_negative": 0,
    }
    assert row["matched_entity_sentiment_accuracy"]["value"] == 0


def test_missing_predictions_lower_recall_and_never_turn_into_negative_gold():
    case = entity_case()
    row = evaluate([case], [], labels(case, entities=[mention()]))["strata"][0]
    assert row["entity_recall"]["value"] == 0
    assert row["entity_precision"]["value"] is None
    assert row["annotation_coverage"]["prediction_missing"] == 1
    pending = Annotation(
        case_id=case.case_id,
        input_hash=case.input_hash,
        reviewer_id="reader-a",
        status="pending",
    )
    row = evaluate([case], [prediction(case, entities=[mention()])], [pending])[
        "strata"
    ][0]
    assert (
        row["entity_precision"]["value"] is None
        and row["annotation_coverage"]["unreviewed"] == 1
    )


def test_reviewed_empty_list_is_a_valid_negative_with_undefined_empty_denominators():
    case = entity_case(text="这里没有任何品牌名。")
    row = evaluate([case], [prediction(case)], labels(case, entities=[]))["strata"][0]
    assert row["annotation_coverage"]["agreed"] == 1
    assert (
        row["entity_precision"]["value"] is None
        and row["entity_recall"]["value"] is None
    )


def test_single_review_and_disagreement_are_not_scored_as_truth():
    case = entity_case()
    single = labels(case, entities=[mention()], reviewers=("reader-a",))
    row = evaluate([case], [prediction(case, entities=[mention()])], single)["strata"][
        0
    ]
    assert (
        row["annotation_coverage"]["single_review"] == 1
        and row["entity_precision"]["value"] is None
    )
    disagreement = single + labels(case, entities=[], reviewers=("reader-b",))
    row = evaluate([case], [prediction(case)], disagreement)["strata"][0]
    assert row["annotation_coverage"]["disagreement"] == 1
    assert row["entity_recall"]["value"] is None


@pytest.mark.parametrize("gold", ["partial", "contradicts", "unverified"])
def test_unsupported_support_claims_reduce_support_precision(gold):
    case = relation_case()
    row = evaluate(
        [case],
        [prediction(case, relation="supports", source_quote=case.evidence_quote)],
        labels(case, relation=gold),
    )["strata"][0]
    assert row["support_precision"]["value"] == 0
    assert row["relation_confusion"][gold]["supports"] == 1


def test_candidates_and_wrong_scope_never_count_as_verified_support():
    case = relation_case(citation_eligible=False, source_kind="search_candidate")
    row = evaluate(
        [case],
        [prediction(case, relation="supports", source_quote=case.evidence_quote)],
        labels(case, relation="supports"),
    )["strata"][0]
    assert (
        row["citation_chain_violations"] == 1 and row["support_precision"]["value"] == 0
    )


def test_relation_abstentions_and_missing_results_have_explicit_denominators():
    first = relation_case(evidence_quote="")
    second = relation_case(observation_id="evidence-2")
    result = evaluate(
        [first, second],
        [prediction(first, relation="unverified")],
        labels(first, relation="unverified") + labels(second, relation="supports"),
    )
    row = result["strata"][0]
    assert row["relation_accuracy"]["value"] == 0.5
    assert row["support_precision"]["value"] is None
    assert row["relation_confusion"]["supports"]["missing"] == 1
    assert row["unverified_predictions_in_reviewed_pairs"] == 1


def test_platform_model_owner_and_content_status_are_never_pooled():
    cases = [
        entity_case(),
        entity_case(platform="deepseek"),
        entity_case(signature="b" * 64),
        entity_case(
            owner_kind="snapshot",
            source_kind="answer_reference",
            content_status="captured",
        ),
        entity_case(content_status="partial"),
    ]
    result = evaluate(cases, [], [])
    assert len(result["strata"]) == 5
    assert (
        result["prompt_groups"] == 1
    )  # Five observations do not become five independent questions.


@pytest.mark.parametrize(
    "damage",
    [
        "case_hash",
        "prediction_hash",
        "signature",
        "unknown_case",
        "duplicate_case",
        "duplicate_prediction",
        "duplicate_reviewer",
        "gold_span",
        "wrong_task",
        "pending_gold",
        "missing_with_result",
        "quote_changed",
    ],
)
def test_invalid_or_rebound_artifacts_fail_closed(damage):
    case = entity_case()
    cases, predictions, annotations = (
        [case.model_dump()],
        [prediction(case, entities=[mention()])],
        labels(case, entities=[mention()]),
    )
    if damage == "case_hash":
        cases[0]["text"] += "改写"
    elif damage == "prediction_hash":
        predictions[0]["input_hash"] = "b" * 64
    elif damage == "signature":
        predictions[0]["signature"] = "b" * 64
    elif damage == "unknown_case":
        predictions[0]["case_id"] = "b" * 64
    elif damage == "duplicate_case":
        cases.append(deepcopy(cases[0]))
    elif damage == "duplicate_prediction":
        predictions.append(deepcopy(predictions[0]))
    elif damage == "duplicate_reviewer":
        annotations.append(deepcopy(annotations[0]))
    elif damage == "gold_span":
        annotations[0]["entities"][0]["start"] = 1
    elif damage == "wrong_task":
        annotations[0]["relation"] = "supports"
    elif damage == "pending_gold":
        annotations[0]["status"] = "pending"
    elif damage == "missing_with_result":
        predictions[0]["status"] = "missing"
    elif damage == "quote_changed":
        case = relation_case()
        cases, predictions, annotations = (
            [case],
            [prediction(case, relation="supports", source_quote="青禾")],
            [],
        )
    with pytest.raises((EvaluationInvalid, ValueError)):
        evaluate(cases, predictions, annotations)


def test_holdout_separation_is_by_question_group_across_platforms_and_audits():
    holdout = entity_case()
    development = entity_case(
        platform="deepseek", run_id="other-run", split="development"
    )
    with pytest.raises(EvaluationInvalid, match="overlaps"):
        evaluate([holdout, development], [], [])
    with pytest.raises(EvaluationInvalid, match="overlaps"):
        evaluate([holdout], [], [], development_group_ids=[holdout.group_id])


def test_synthetic_samples_do_not_improve_real_audit_metrics():
    cases = [entity_case(), entity_case(origin="stored_audit")]
    result = evaluate(cases, [], [])
    assert len(result["strata"]) == 2
    assert {row["origin"] for row in result["strata"]} == {"synthetic", "stored_audit"}


def test_readable_report_explains_errors_without_ranking_platforms_or_zero_empty_scores():
    from app.intelligence.evaluation import render_report

    entity = entity_case()
    relation = relation_case()
    empty = entity_case(platform="deepseek", text="需要继续核实。")
    report = evaluate(
        [entity, relation, empty],
        [
            prediction(entity, entities=[mention(), mention("虚构", 0)]),
            prediction(
                relation, relation="supports", source_quote=relation.evidence_quote
            ),
            prediction(empty),
        ],
        labels(entity, entities=[mention(), mention("云桥", 5, "negative")])
        + labels(relation, relation="contradicts")
        + labels(empty, entities=[]),
    )
    text = render_report(report)
    assert "合成演示" in text and "不能判断哪个平台更好" in text
    assert "遗漏：原文有主体" in text and "误认：把不应识别" in text
    assert "1 条与所选证据矛盾" in text
    assert "没有需要识别的主体" in text
    assert "100.0%" not in text and "entities /" not in text
