"""Single-round facts for a frozen brand/question/platform scope, never inference."""

from collections import Counter, defaultdict
from copy import deepcopy

from fastapi import HTTPException

from app.intelligence.aliases import resolved_entity
from app.intelligence.brand_evaluation import VERSION as SCORE_VERSION
from app.intelligence.brand_evaluation import WEIGHTS
from app.intelligence.brand_presence import VERSION as PRESENCE_VERSION
from app.intelligence.semantics import stable_hash
from app.intelligence.source_metrics import METRICS as SOURCE_METRICS

VERSION = "brand-observation-descriptive-1"
IDENTITY_POLICY = "stable-target-confirmed-names-equivalence-1"
CONDITION_POLICY = "known-difference-before-unknown-1"
METRICS = (*WEIGHTS, *SOURCE_METRICS)


def question_scope(cohort, slots):
    selected = {(row["platform"], row["prompt_hash"]) for row in slots}
    return sorted(
        [
            {
                key: cell.get(key)
                for key in (
                    "platform",
                    "prompt_hash",
                    "primary_scenario",
                    "eligibility",
                )
            }
            for cell in (cohort or {}).get("cells", [])
            if (cell["platform"], cell["prompt_hash"]) in selected
        ],
        key=lambda row: (row["platform"], row["prompt_hash"]),
    )


def target_projection(presence, target_ids):
    return {
        "version": presence.get("version"),
        "targets": [
            row for row in presence.get("targets", []) if row["target_id"] in target_ids
        ],
        "observations": [
            row
            for row in presence.get("observations", [])
            if row["target_id"] in target_ids
        ],
    }


def freeze_descriptive(run, data, rows):
    presence = run.summary.get("brand_presence", {})
    targets = {row["target_id"]: row for row in presence.get("targets", [])}
    if presence.get("version") != PRESENCE_VERSION or any(
        key not in targets for key in data.brand_target_ids
    ):
        raise HTTPException(
            422, "Select stable brand targets from the published baseline"
        )
    first = [row for row in rows if row["sample_index"] == 1]
    platforms = {row["platform"] for row in first}
    prompts = {row["prompt_hash"] for row in first}
    if set(data.platforms) - platforms or set(data.prompt_hashes) - prompts:
        raise HTTPException(
            422, "Selected platforms/questions are outside the baseline"
        )
    slots = [
        row
        for row in first
        if (not data.platforms or row["platform"] in data.platforms)
        and (not data.prompt_hashes or row["prompt_hash"] in data.prompt_hashes)
    ]
    if not slots or len(
        {(row["platform"], row["prompt_hash"]) for row in slots}
    ) != len(slots):
        raise HTTPException(
            422, "One unambiguous first-round slot per question/platform is required"
        )
    cohort = run.manifest.get("evaluation_cohort", {})
    if cohort.get("rule_version") != SCORE_VERSION:
        raise HTTPException(409, "Published scoring rule is unavailable or changed")
    return {
        "version": VERSION,
        "comparison_mode": "descriptive_v1",
        "metric_version": VERSION,
        "baseline_run_id": run.id,
        "baseline_input_hash": run.input_hash,
        "analysis_signature": run.signature,
        "brand_target_ids": sorted(data.brand_target_ids),
        "brand_targets": [
            {
                **deepcopy(targets[key]),
                "frozen_identity_context": deepcopy(
                    run.manifest.get("entity_resolutions", {}).get(
                        targets[key].get("entity_id"), {}
                    )
                ),
            }
            for key in sorted(data.brand_target_ids)
        ],
        "baseline_brand_projection_hash": stable_hash(
            target_projection(presence, data.brand_target_ids)
        ),
        "entities": [],
        "entity_ids": [],
        "slots": deepcopy(slots),
        "question_scope": question_scope(cohort, slots),
        "score_rule_version": SCORE_VERSION,
        "score_weights": WEIGHTS.copy(),
        "presence_version": PRESENCE_VERSION,
        "identity_policy": IDENTITY_POLICY,
        "condition_policy": CONDITION_POLICY,
        "metrics": list(METRICS),
        "min_prompts": 1,
        "min_rounds": 1,
        "metric": "brand_dimensions_and_source_facts",
        "causal_claim_allowed": False,
    }


def normalized_names(target):
    return sorted(
        {name.strip().casefold() for name in target.get("names", []) if name.strip()}
    )


def match_target(frozen, snapshot):
    targets = snapshot.get("brand_presence", {}).get("targets", [])
    exact = [row for row in targets if row["target_id"] == frozen["target_id"]]
    if len(exact) == 1:
        return exact[0]
    if frozen.get("entity_id"):
        root = resolved_entity(
            frozen["entity_id"],
            {
                "entity_aliases": snapshot.get("entity_aliases", {}),
                "entity_resolutions": snapshot.get("identity_context", {}),
            },
        )
        if root != frozen["entity_id"]:
            equivalent = [row for row in targets if row.get("entity_id") == root]
            if len(equivalent) == 1:
                return equivalent[0]
    return None


def identity_pair(frozen, before, after, followup):
    if not before or not after:
        return "brand_target_missing", None
    if (
        before.get("identity_status") != "confirmed"
        or after.get("identity_status") != "confirmed"
    ):
        return "identity_unconfirmed", None
    if (
        not normalized_names(frozen)
        or normalized_names(frozen) != normalized_names(before)
        or normalized_names(frozen) != normalized_names(after)
    ):
        return "brand_name_scope_changed", None
    if frozen.get("registry_brand_id") != after.get("registry_brand_id"):
        return "registry_target_changed", None
    if frozen.get("role") != after.get("role"):
        return "brand_role_changed", None
    left, right = before.get("entity_id"), after.get("entity_id")
    if left == right:
        if left and frozen["frozen_identity_context"] != followup.get(
            "identity_context", {}
        ).get(right, {}):
            return "entity_mapping_changed", None
        return None, {
            "kind": "same_entity" if left else "stable_registry",
            "baseline_entity_id": left,
            "followup_entity_id": right,
        }
    if left is None and right and frozen["target_id"].startswith("registry:"):
        return None, {
            "kind": "stable_registry_first_observed",
            "baseline_entity_id": None,
            "followup_entity_id": right,
        }
    if (
        left
        and right
        and resolved_entity(
            left,
            {
                "entity_aliases": followup.get("entity_aliases", {}),
                "entity_resolutions": followup.get("identity_context", {}),
            },
        )
        == right
    ):
        return None, {
            "kind": "confirmed_alias_equivalence",
            "baseline_entity_id": left,
            "followup_entity_id": right,
            "alias_events": alias_evidence(
                left, right, followup.get("entity_aliases", {})
            ),
        }
    return "entity_mapping_changed", None


def alias_evidence(left, right, aliases):
    events = {}
    while left != right and left in aliases and len(events) < 64:
        events[left] = deepcopy(aliases[left])
        left = aliases[left].get("target_entity_id")
    return events


def supported_spec(spec):
    return (
        spec.get("metric_version") == VERSION
        and spec.get("score_rule_version") == SCORE_VERSION
        and spec.get("score_weights") == WEIGHTS
        and spec.get("identity_policy") == IDENTITY_POLICY
        and spec.get("condition_policy") == CONDITION_POLICY
        and spec.get("presence_version") == PRESENCE_VERSION
        and spec.get("metrics") == list(METRICS)
        and spec.get("min_prompts") == spec.get("min_rounds") == 1
        and bool(spec.get("slots"))
        and all(row.get("sample_index") == 1 for row in spec["slots"])
        and (
            not spec.get("action_link")
            or spec["action_link"].get("policy_version") == "publication-window-v1"
        )
    )


def condition_state(before, after):
    """An observed change must not disappear behind another unknown field."""
    unknown, changed = [], []
    for key in ("searchMode", "modelVersion", "accountClass", "location"):
        left, right = (
            (row.get("conditions") or {}).get(key) for row in (before, after)
        )
        left_known = (
            isinstance(left, str)
            and left.strip()
            and left.strip().casefold() not in ("unknown", "unavailable")
        )
        right_known = (
            isinstance(right, str)
            and right.strip()
            and right.strip().casefold() not in ("unknown", "unavailable")
        )
        if left_known and right_known and left != right:
            changed.append(key)
        if not left_known or not right_known:
            unknown.append(key)
    if before.get("surface") not in ("app", "web") or after.get("surface") not in (
        "app",
        "web",
    ):
        unknown.append("surface")
    elif before.get("surface") != after.get("surface"):
        changed.append("surface")
    return (
        "incompatible"
        if changed
        else "conditions_unverified"
        if unknown
        else "comparable"
    ), {"changed": changed, "unknown": unknown}


def observation(snapshot, target, response):
    index = snapshot.get("observation_index")
    if index is not None:
        return index.get(
            (
                target["target_id"],
                response["id"],
                response.get("sample_id"),
                response["platform"],
            )
        )
    rows = [
        row
        for row in snapshot.get("brand_presence", {}).get("observations", [])
        if row["target_id"] == target["target_id"]
        and row.get("response_id") == response["id"]
        and row.get("sample_id") == response.get("sample_id")
        and row.get("platform") == response["platform"]
    ]
    return rows[0] if len(rows) == 1 else None


def metric_value(metric, snapshot, target, response):
    result = {
        "value": None,
        "known": False,
        "reason_codes": [],
        "evidence_ids": [],
        "source_traces": [],
    }
    if not target or not response:
        return {**result, "reason_codes": ["observation_missing"]}
    if target.get("identity_status") != "confirmed":
        return {**result, "reason_codes": ["identity_unconfirmed"]}
    if metric in SOURCE_METRICS:
        eid = target.get("entity_id")
        source = snapshot.get("source_metrics", {}).get(metric, {})
        if not eid:
            return {**result, "reason_codes": ["source_brand_entity_unbound"]}
        rid = response["id"]
        present = rid in source.get("hits", {}).get(eid, set())
        result["value"] = (
            1 if present else 0 if rid in source.get("completed", set()) else None
        )
        result["known"] = rid in source.get("completed", set())
        result["source_traces"] = [
            deepcopy(row)
            for row in source.get("traces", {}).get(rid, [])
            if row["entity_id"] == eid
        ]
        if not result["known"]:
            result["reason_codes"] = [
                source.get("unusable_reasons", {}).get(rid, "source_analysis_unknown")
            ]
        return result
    row = observation(snapshot, target, response)
    if not row:
        return {**result, "reason_codes": ["brand_observation_missing"]}
    result["evidence_ids"] = row.get("evidence_ids", [])
    result["reason_codes"] = row.get("reason_codes", [])
    if row["state"] not in ("present", "observed_absent"):
        return result
    if (
        row["state"] == "observed_absent"
        and not row.get("coverage_complete")
        or row["state"] == "present"
        and not result["evidence_ids"]
    ):
        return {
            **result,
            "reason_codes": [*result["reason_codes"], "observation_proof_incomplete"],
        }
    if metric == "presence":
        value = 1 if row["state"] == "present" else 0
    else:
        value = (
            row.get("dimensions", {}).get(metric)
            if row.get("coverage_complete")
            else None
        )
    known = (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0 <= value <= 100
    )
    result.update(value=value if known else None, known=known)
    if not result["known"]:
        result["reason_codes"] = [*result["reason_codes"], "dimension_unobserved"]
    return result


def compare_descriptive(spec, baseline, followup):
    from app.intelligence.measurement import timestamp

    if (
        stable_hash(
            target_projection(
                baseline.get("brand_presence", {}), spec["brand_target_ids"]
            )
        )
        != spec["baseline_brand_projection_hash"]
    ):
        raise HTTPException(409, "Frozen baseline brand observations changed")
    indexed_snapshots = []
    for snapshot in (baseline, followup):
        observations = defaultdict(list)
        for row in snapshot.get("brand_presence", {}).get("observations", []):
            observations[
                (
                    row["target_id"],
                    row.get("response_id"),
                    row.get("sample_id"),
                    row["platform"],
                )
            ].append(row)
        indexed_snapshots.append(
            {
                **snapshot,
                "observation_index": {
                    key: rows[0] if len(rows) == 1 else None
                    for key, rows in observations.items()
                },
            }
        )
    baseline, followup = indexed_snapshots
    grouped = []
    for snapshot in (baseline, followup):
        index = defaultdict(list)
        for row in snapshot.get("responses", []):
            index[
                (
                    row.get("prompt_version_id"),
                    row.get("platform"),
                    row.get("sample_index"),
                )
            ].append(row)
        grouped.append(index)
    expected = {(row["prompt_version_id"], row["platform"], 1) for row in spec["slots"]}
    duplicate_ids = [
        Counter(row["id"] for row in snapshot.get("responses", []))
        for snapshot in (baseline, followup)
    ]
    signature_equal = (
        spec["analysis_signature"] == baseline["signature"] == followup["signature"]
    )
    rules_equal = all(
        snapshot.get("evaluation_cohort", {}).get("rule_version")
        == spec["score_rule_version"]
        and snapshot.get("brand_presence", {}).get("version")
        == spec["presence_version"]
        for snapshot in (baseline, followup)
    )
    question_equal = all(
        question_scope(snapshot.get("evaluation_cohort"), spec["slots"])
        == spec["question_scope"]
        for snapshot in (baseline, followup)
    )
    platforms = []
    for platform in sorted({row["platform"] for row in spec["slots"]}):
        cells, included, excluded = [], [], []
        for slot in [row for row in spec["slots"] if row["platform"] == platform]:
            key = (slot["prompt_version_id"], platform, 1)
            candidates = [index[key] for index in grouped]
            before, after = [rows[0] if len(rows) == 1 else None for rows in candidates]
            state, reasons, conditions = "comparable", [], {}
            if any(len(rows) != 1 for rows in candidates) or any(
                row and counts[row["id"]] != 1
                for row, counts in zip((before, after), duplicate_ids, strict=True)
            ):
                state, reasons = (
                    "missing",
                    [
                        "ambiguous_sample"
                        if any(len(rows) > 1 for rows in candidates)
                        else "sample_missing"
                    ],
                )
            elif before.get("sample_id") != slot["sample_id"]:
                state, reasons = "incompatible", ["baseline_sample_changed"]
            elif any(
                row.get("error") or row.get("answer_status") != "complete"
                for row in (before, after)
            ):
                state, reasons = "missing", ["incomplete_answer"]
            elif any(
                row.get("prompt_hash") != slot["prompt_hash"] for row in (before, after)
            ):
                state, reasons = "incompatible", ["prompt_version_changed"]
            elif not signature_equal or not rules_equal or not question_equal:
                state, reasons = (
                    "incompatible",
                    [
                        "analysis_signature_changed"
                        if not signature_equal
                        else "score_rule_changed"
                        if not rules_equal
                        else "question_scope_changed"
                    ],
                )
            else:
                btime, atime = (
                    timestamp(before.get("observed_at")),
                    timestamp(after.get("observed_at")),
                )
                if not btime or not atime or atime <= btime:
                    state, reasons = "incompatible", ["observation_not_later"]
                else:
                    state, conditions = condition_state(before, after)
                    reasons = (
                        ["conditions_changed"]
                        if conditions["changed"]
                        else ["unknown_conditions"]
                        if conditions["unknown"]
                        else []
                    )
            trace = {
                "baseline_sample_id": slot["sample_id"],
                "prompt_version_id": slot["prompt_version_id"],
                "prompt_hash": slot["prompt_hash"],
                "sample_index": 1,
                "baseline_response_id": before["id"] if before else None,
                "followup_response_id": after["id"] if after else None,
            }
            (included if state == "comparable" else excluded).append(
                {**trace, "reason": reasons[0] if reasons else None}
            )
            target_rows = []
            for frozen in spec["brand_targets"]:
                left, right = (
                    match_target(frozen, baseline),
                    match_target(frozen, followup),
                )
                identity_reason, proof = identity_pair(frozen, left, right, followup)
                metrics = []
                for metric in spec["metrics"]:
                    bvalue, avalue = (
                        metric_value(metric, baseline, left, before),
                        metric_value(metric, followup, right, after),
                    )
                    metric_state = "incompatible" if identity_reason else state
                    metric_reasons = (
                        [identity_reason] if identity_reason else list(reasons)
                    )
                    if metric_state == "comparable" and not (
                        bvalue["known"] and avalue["known"]
                    ):
                        metric_state = "missing"
                        metric_reasons = ["metric_unknown"]
                    metrics.append(
                        {
                            "metric": metric,
                            "unit": "presence_0_1"
                            if metric == "presence" or metric in SOURCE_METRICS
                            else "score_points_0_100",
                            "baseline": bvalue,
                            "followup": avalue,
                            "comparison_state": metric_state,
                            "reason_codes": metric_reasons,
                            "observed_delta": avalue["value"] - bvalue["value"]
                            if metric_state == "comparable"
                            else None,
                        }
                    )
                target_rows.append(
                    {
                        "brand_target_id": frozen["target_id"],
                        "baseline_target_id": left["target_id"] if left else None,
                        "followup_target_id": right["target_id"] if right else None,
                        "identity_equivalence": proof,
                        "metrics": metrics,
                    }
                )
            cells.append(
                {
                    **trace,
                    "comparison_state": state,
                    "reason_codes": reasons,
                    "condition_check": conditions,
                    "baseline_observed_at": before.get("observed_at")
                    if before
                    else None,
                    "followup_observed_at": after.get("observed_at") if after else None,
                    "baseline_conditions": deepcopy(before.get("conditions"))
                    if before
                    else None,
                    "followup_conditions": deepcopy(after.get("conditions"))
                    if after
                    else None,
                    "targets": target_rows,
                }
            )
        platforms.append(
            {
                "platform": platform,
                "status": "descriptive_observation",
                "expected_samples": len(cells),
                "prompt_clusters": len({row["prompt_version_id"] for row in cells}),
                "included": included,
                "excluded": excluded,
                "metrics": [],
                "observations": cells,
                "out_of_cohort": [
                    {"response_id": row["id"], "reason": "outside_frozen_cohort"}
                    for row in followup.get("responses", [])
                    if row["platform"] == platform
                    and (
                        row.get("prompt_version_id"),
                        platform,
                        row.get("sample_index"),
                    )
                    not in expected
                ],
            }
        )
    return {
        "version": VERSION,
        "comparison_mode": "descriptive_v1",
        "metric_version": VERSION,
        "brand_targets": deepcopy(spec["brand_targets"]),
        "analysis_signature_equal": signature_equal,
        "platforms": platforms,
        "method": {
            "name": "single_round_descriptive",
            "min_prompts": 1,
            "min_rounds": 1,
        },
        "causal_claim_allowed": False,
        "improvement_score": None,
        "business_metrics": {
            "clicks": "unknown",
            "conversions": "unknown",
            "roi": "unknown",
        },
        "limitations": [
            "One observed answer per frozen question/platform is not a stable effect estimate",
            "Unknown values or conditions never become zeros or an improvement score",
            "Delivery acceptance, publication and visibility observation are separate facts",
            "Captured source text presence does not prove that a model used that text",
        ],
    }
