from copy import deepcopy

import pytest

from app.intelligence.evaluation import freeze_case
from app.intelligence.value_replay import inspect_dataset


def dataset(observation="obs1", offset=0, text="云衡🙂，按完整原文核对。"):
    case = freeze_case(
        group_id="a" * 64,
        origin="stored_audit",
        split="development",
        platform="qwen",
        signature="b" * 64,
        run_id="r1",
        task="entities",
        owner_kind="snapshot",
        owner_id="source1",
        segment_id="segment1",
        observation_id=observation,
        global_offset=offset,
        text=text,
        prompt="如何选型？",
        source_kind="answer_reference",
        content_status="unknown",
    ).model_dump()
    prediction = {
        "case_id": case["case_id"],
        "input_hash": case["input_hash"],
        "signature": case["signature"],
        "status": "missing",
        "entities": [],
    }
    return {"dataset_id": "test", "cases": [case], "predictions": [prediction]}


def test_empty_failed_export_is_not_a_successful_replay():
    with pytest.raises(ValueError, match="No frozen input"):
        inspect_dataset({"dataset_id": "failed", "cases": [], "predictions": []})


def test_corrupted_original_input_cannot_be_replayed_as_verified():
    value = dataset()
    value["cases"][0]["text"] += "篡改"
    with pytest.raises(ValueError):
        inspect_dataset(value)


def test_same_body_two_observations_is_not_double_counted_as_unique_input():
    first, second = dataset(), dataset(observation="obs2")
    value = deepcopy(first)
    value["cases"] += second["cases"]
    value["predictions"] += second["predictions"]
    result = inspect_dataset(value)
    assert result["owners"] == 2
    assert result["characters"] == 2 * result["unique_input_characters"]
    assert result["release_eligible"] is False
    assert result["semantic_quality"] == "not_evaluated"
    assert result["model_calls"] == 0


def test_owner_gap_and_missing_prediction_state_are_rejected():
    with pytest.raises(ValueError, match="gap or overlap"):
        inspect_dataset(dataset(offset=1))
    value = dataset()
    value["predictions"] = []
    with pytest.raises(ValueError, match="Missing prediction"):
        inspect_dataset(value)
