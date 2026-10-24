"""Offline integrity replay of a frozen real dataset; never calls a model or DB."""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from app.intelligence.evaluation import Case, Prediction, check_case
from app.intelligence.semantics import chunks, stable_hash


def inspect_dataset(dataset):
    cases = [Case.model_validate(row) for row in dataset["cases"]]
    predictions = [Prediction.model_validate(row) for row in dataset["predictions"]]
    if not cases:
        raise ValueError(
            "No frozen input cases; a failed run is not a successful replay"
        )
    ids = set()
    owners = defaultdict(list)
    for case in cases:
        check_case(case)
        if case.case_id in ids:
            raise ValueError("Duplicate case")
        ids.add(case.case_id)
        if case.task != "entities":
            continue
        segments = chunks(case.text)
        if "".join(s["text"] for s in segments) != case.text:
            raise ValueError("Segment replay dropped input text")
        owners[
            (case.run_id, case.owner_kind, case.owner_id, case.observation_id)
        ].append(case)
    for rows in owners.values():
        rows.sort(key=lambda row: row.global_offset)
        end = 0
        for row in rows:
            if row.global_offset != end:
                raise ValueError("Owner coverage contains a gap or overlap")
            end += len(row.text)
    prediction_ids = set()
    by_id = {case.case_id: case for case in cases}
    for prediction in predictions:
        case = by_id.get(prediction.case_id)
        if not case or prediction.case_id in prediction_ids:
            raise ValueError("Prediction scope is invalid")
        if (
            prediction.input_hash != case.input_hash
            or prediction.signature != case.signature
        ):
            raise ValueError("Prediction version does not match frozen input")
        prediction_ids.add(prediction.case_id)
    if prediction_ids != ids:
        raise ValueError("Missing prediction states must remain explicit")
    return {
        "dataset_id": dataset["dataset_id"],
        "input_manifest_hash": stable_hash([(c.case_id, c.input_hash) for c in cases]),
        "cases": len(cases),
        "owners": len(owners),
        "owner_kinds": dict(
            Counter(c.owner_kind for c in cases if c.task == "entities")
        ),
        "characters": sum(len(c.text) for c in cases if c.task == "entities"),
        "unique_input_characters": sum(
            len(text)
            for _, _, _, text in {
                (c.owner_kind, c.owner_id, c.global_offset, c.text)
                for c in cases
                if c.task == "entities"
            }
        ),
        "prediction_states": dict(Counter(p.status for p in predictions)),
        "integrity": "passed",
        "semantic_quality": "not_evaluated",
        "model_calls": 0,
        "release_eligible": False,
    }


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        raise ValueError("Choose a new output path")
    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8-sig"))
    report = inspect_dataset(dataset)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    cli()
