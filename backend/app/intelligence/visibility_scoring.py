"""Deterministic v4 scores over frozen, verifiable answer samples."""

from collections import Counter, defaultdict
from fractions import Fraction

from fastapi import HTTPException

from app.intelligence.analysis_coverage import answer_coverage, related_span_defects
from app.intelligence.answer_brands import (
    answer_brand_mentions,
    answer_prose,
    brand_target_visible,
)
from app.intelligence.brand_ranking import declares_ranking, verified_ranking
from app.intelligence.competitive_comparison import TASKS
from app.intelligence.question_context import literal_matches
from app.intelligence.semantics import stable_hash

RULE_VERSION = "visibility-score-v4-draft-1"
WEIGHTS = {"presence": 25, "recommendation": 40, "position": 15, "reasons": 20}
REASON_VALUES = {"none": 0, "generic": 25, "specific": 50, "need_matched": 75}


def freeze_visibility_scope(samples, cohort):
    questions = {cell["prompt_hash"]: cell for cell in cohort["cells"]}
    cells = []
    for sample in samples:
        question = questions[sample["prompt_hash"]]
        context = question["question_context"]
        review = context.get("review")
        references = context["registered_references"]
        if question["eligibility"] == "excluded" or (
            review and review["brand_scope"] == "directed"
        ):
            scope = "directed_question"
        elif question["eligibility"] == "eligible":
            scope = "eligible"
        elif any(ref["screen"] == "directed" for ref in references):
            scope = "directed_question"
        elif references or (review and review["brand_scope"] == "uncertain"):
            scope = "question_scope_unresolved"
        else:
            scope = "eligible"
        suggestions = context.get("scenarios", [])
        scenario = question.get("primary_scenario")
        if scenario is None and len(suggestions) == 1:
            scenario = suggestions[0]["scenario"]
        cells.append(
            {
                "sample_id": sample["sample_id"],
                "platform": sample["platform"],
                "prompt_id": sample["prompt_id"],
                "prompt_version_id": sample["prompt_version_id"],
                "prompt": sample["prompt"],
                "scenario": scenario,
                "scope": scope,
            }
        )
    cells.sort(
        key=lambda cell: (cell["platform"], int(cell["prompt_id"]), cell["sample_id"])
    )
    return {
        "rule_version": RULE_VERSION,
        "weights": WEIGHTS.copy(),
        "scope_hash": stable_hash(cells),
        "cells": cells,
    }


def observed_names(target):
    return {name.strip().casefold() for name in target["names"] if name.strip()}


def score_targets(presence, entities, answer_mentions):
    known_entities = {entity["id"]: entity for entity in entities}
    answer_names = defaultdict(set)
    answer_entity_ids = set()
    for items in answer_mentions.values():
        for mention in items:
            answer_entity_ids.add(mention["entity_id"])
            if not mention.get("identity_ambiguity"):
                answer_names[mention["entity_id"]].add(
                    mention["observed_entity_name"].casefold()
                )
    targets = [
        row for row in presence["targets"] if brand_target_visible(row, answer_entity_ids)
    ]
    registry_names = set().union(
        *(
            observed_names(target)
            for target in targets
            if target.get("registry_brand_id")
        )
    )
    identities_by_name = defaultdict(set)
    for entity in entities:
        if entity["id"] not in answer_entity_ids:
            continue
        for name in {entity["name"], *entity.get("observed_names", [])}:
            identities_by_name[name.casefold()].add(entity["id"])

    bound_here = set()
    result = []
    for target in targets:
        entity_id = target.get("entity_id")
        entity = known_entities.get(entity_id, {})
        method = (
            "confirmed" if target["identity_status"] == "confirmed" else "unresolved"
        )
        candidates = target.get("candidate_entity_ids", [])
        names = observed_names(target)
        if (
            target.get("registry_brand_id")
            and target.get("reason_codes") == ["registered_binding_required"]
            and len(candidates) == 1
            and names & answer_names[candidates[0]]
        ):
            candidate = known_entities[candidates[0]]
            if candidate["identity_status"] in (
                "confirmed",
                "provisional",
            ) and not candidate.get("role_conflict"):
                entity_id, entity, method = (
                    candidate["id"],
                    candidate,
                    "exact_registered_name",
                )
                bound_here.add(entity_id)
        if not target.get("registry_brand_id"):
            unique_name = (
                bool(names)
                and all(len(identities_by_name[name]) <= 1 for name in names)
                and not (names & registry_names)
            )
            if (
                method == "unresolved"
                and unique_name
                and entity.get("identity_status") == "provisional"
                and entity.get("comparison_context_status") == "eligible"
                and names & answer_names[entity_id]
            ):
                method = "observed_name"
            if not unique_name and method != "confirmed":
                method = "unresolved"
        if entity.get("role_conflict"):
            method = "unresolved"
        result.append(
            {
                **{
                    key: target.get(key)
                    for key in (
                        "target_id",
                        "registry_brand_id",
                        "name",
                        "names",
                        "role",
                        "identity_status",
                        "source_mentions",
                    )
                },
                "entity_id": entity_id,
                "identity_method": method,
            }
        )
    return [
        target
        for target in result
        if target["registry_brand_id"] or target["entity_id"] not in bound_here
    ]


def checked_span(row, response):
    start, end = row["start"], row["end"]
    return (
        0 <= start < end <= len(response["text"])
        and response["text"][start:end] == row["quote"]
    )


def supports_observation(item, response, observed, signals, names):
    if any(item["answer_quote"] in row["quote"] for row in observed):
        return True
    verification = item.get("verification") or {}
    span = verification.get("answer_span")
    if (
        not span
        or span["quote"] != item["answer_quote"]
        or not checked_span(span, response)
        or (item.get("claim_subject") or "").strip().casefold() not in names
        or (verification.get("association") or {}).get("method")
        not in ("answer_anchor", "answer_reference_marker")
    ):
        return False
    # An extracted recommendation may quote a heading while the same product's
    # capability claim is below it. Do not cross into another product's section.
    preceding = [
        row
        for row in signals
        if row.get("segment_id") == item.get("answer_segment_id")
        and row["end"] <= span["start"]
        and checked_span(row, response)
    ]
    if not preceding:
        return False
    latest = max(row["end"] for row in preceding)
    return all(row in observed for row in preceding if row["end"] == latest)


def sample_observation(
    cell, response, target, proof, mentions, signals, ranking, source_evidence, entity
):
    if cell["scope"] != "eligible":
        return {"reason": cell["scope"]}
    if not response:
        return {"reason": "capture_missing"}
    if response.get("answer_status") != "complete" or response.get("error"):
        return {"reason": "answer_incomplete"}
    if not proof["input_processed"]:
        return {"reason": "analysis_incomplete"}
    if target["identity_method"] == "unresolved":
        return {"reason": "identity_unresolved"}
    if related_span_defects(proof, target["names"]):
        return {"reason": "relevant_evidence_unverified"}

    names = observed_names(target)
    entity_id = target["entity_id"]
    related = [mention for mention in mentions if mention["entity_id"] == entity_id]
    matched = [
        mention
        for mention in related
        if not mention.get("identity_ambiguity") and checked_span(mention, response)
    ]
    if any(mention.get("identity_ambiguity") for mention in related) or any(
        mention["observed_entity_name"].casefold() in names
        and (mention["entity_id"] != entity_id or mention.get("identity_ambiguity"))
        for mention in mentions
    ):
        return {"reason": "identity_unresolved"}
    if not matched:
        body = answer_prose(response["text"])
        if any(literal_matches(body, name) for name in names):
            return {"reason": "identity_unresolved"}
        return {
            "dimensions": dict.fromkeys(WEIGHTS, 0),
            "position_status": "brand_absent",
            "presence": "observed_absent",
        }
    if not target["registry_brand_id"] and "evaluated_solution" not in entity.get(
        "answer_context_roles", {}
    ).get(response["id"], []):
        return {"reason": "comparison_context_unverified"}

    observed = [
        row
        for row in signals
        if row["entity_id"] == entity_id
        and checked_span(row, response)
        and set(row["mention_ids"]).intersection(mention["id"] for mention in matched)
    ]
    strengths = {
        row["recommendation"] for row in observed if row["recommendation"] is not None
    }
    reasons = {
        row["reason_level"] for row in observed if row["reason_level"] is not None
    }
    ranks = (
        {row["explicit_rank"] for row in observed if row["explicit_rank"] is not None}
        if ranking is not None
        else set()
    )
    if len(strengths) != 1 or len(reasons) > 1 or len(ranks) > 1:
        return {"reason": "recommendation_unverified"}
    strength = next(iter(strengths))
    reason_level = next(iter(reasons), "none" if strength <= 1 else None)
    if reason_level not in REASON_VALUES:
        return {"reason": "recommendation_reason_unverified"}
    position = ranking.get(entity_id) if ranking else None
    if ranks and position is None:
        return {"reason": "position_unverified"}
    rank = position["rank"] if position else None

    evidence = [
        {
            "signal_id": row["id"],
            "quote": row["quote"],
            "start": row["start"],
            "end": row["end"],
            "mention_ids": row["mention_ids"],
        }
        for row in sorted(observed, key=lambda row: (row["start"], row["id"]))
    ]
    linked_support = [
        {
            "citation_id": item["citation_id"],
            "ordinal": item.get("ordinal"),
            "snapshot_id": item["snapshot_id"],
            "answer_quote": item["answer_quote"],
            "source_quote": item["source_quote"],
        }
        for item in source_evidence
        if item["relation"] == "supports"
        and item["snapshot_id"]
        and item.get("source_quote")
        and item.get("answer_quote")
        and supports_observation(item, response, observed, signals, names)
    ]
    reason_value = REASON_VALUES[reason_level]
    if (
        strength >= 2
        and reason_level == "need_matched"
        and any(
            support["answer_quote"] in row["quote"]
            for support in linked_support
            for row in observed
        )
    ):
        reason_value = 100
    return {
        "dimensions": {
            "presence": 100,
            "recommendation": strength * 25,
            "position": 100 / rank if rank else 0,
            "reasons": reason_value if strength >= 2 else 0,
        },
        "presence": "present",
        "rank": rank,
        "position_status": "ranked" if rank else "unranked",
        "evidence": evidence,
        "source_support": linked_support,
    }


def build_visibility_score(manifest, applications, summary):
    scope = manifest["visibility_score_scope"]
    if scope["rule_version"] != RULE_VERSION:
        raise ValueError("Unsupported visibility scoring rule")
    proofs = answer_coverage(manifest, applications)
    answer_mentions, signals, supporting_sources = (
        defaultdict(list),
        defaultdict(list),
        defaultdict(list),
    )
    for item in answer_brand_mentions(manifest, summary["entities"], summary["mentions"]):
        answer_mentions[item["owner_id"]].append(item)
    for row in summary["brand_signals"]["observations"]:
        signals[row["response_id"]].append(row)
    sources = manifest.get("sources", [])
    sources_by_id = {source["id"]: source for source in sources}
    claim_subjects = {
        (row["segment_id"], row["start"], row["end"], row["quote"]): row.get("subject")
        for row in summary.get("claims", ())
        if row["owner_kind"] == "answer"
    }
    for row in summary["claim_evidence"]:
        source = sources_by_id.get(row["citation_id"])
        if not source or source.get("snapshot_id") != row["snapshot_id"]:
            continue
        span = (row.get("verification") or {}).get("answer_span") or {}
        supporting_sources[row["response_id"]].append(
            {
                **row,
                "ordinal": source["ordinal"],
                "claim_subject": claim_subjects.get(
                    (
                        row.get("answer_segment_id"),
                        span.get("start"),
                        span.get("end"),
                        row["answer_quote"],
                    )
                ),
            }
        )
    responses = {row["sample_id"]: row for row in manifest["responses"]}
    ranks = {
        row["id"]: verified_ranking(
            row["text"],
            [item for item in signals[row["id"]] if checked_span(item, row)],
            allow_ties=True,
        )
        for row in manifest["responses"]
        if proofs[row["id"]]["input_processed"]
        and declares_ranking(row["text"], allow_ties=True)
    }
    entities = {row["id"]: row for row in summary["entities"]}
    targets = score_targets(
        summary["brand_presence"], summary["entities"], answer_mentions
    )
    observations = {}
    for target in targets:
        rows = {}
        for cell in scope["cells"]:
            # Universal eligibility/capture gaps come from the cell, independently
            # of the target. Only store observations where an answer was received.
            response = responses.get(cell["sample_id"])
            if cell["scope"] != "eligible" or response is None:
                continue
            response_id = response["id"]
            rows[cell["sample_id"]] = {
                "response_id": response_id,
                **sample_observation(
                    cell,
                    response,
                    target,
                    proofs[response_id],
                    answer_mentions[response_id],
                    signals[response_id],
                    ranks.get(response_id),
                    supporting_sources[response_id],
                    entities.get(target["entity_id"], {}),
                ),
            }
        observations[target["target_id"]] = rows
    return {
        **scope,
        "targets": targets,
        "observations": observations,
        "source_coverage": {
            "observed": len(sources),
            "archived": sum(bool(source.get("snapshot_id")) for source in sources),
        },
    }


def raw_dimension(row, key):
    if key == "position" and row.get("rank"):
        return Fraction(100, row["rank"])
    return Fraction(row["dimensions"][key])


def rounded(value):
    return int(value + Fraction(1, 2))


def weighted(dimensions, weights):
    return sum(dimensions[key] * weight / 100 for key, weight in weights.items())


def score_rows(cells, observations, *, weights=WEIGHTS):
    by_platform = defaultdict(list)
    reasons = Counter()
    for cell in cells:
        row = observations.get(cell["sample_id"], {})
        reason = cell["scope"] if cell["scope"] != "eligible" else row.get("reason")
        if not row and cell["scope"] == "eligible":
            reason = "capture_missing"
        by_platform[cell["platform"]].append((cell, row, reason))
        if reason:
            reasons[reason] += 1
    platforms, valid = [], []
    for platform, entries in by_platform.items():
        usable = [row for _, row, reason in entries if reason is None]
        dimensions = (
            {
                key: sum(raw_dimension(row, key) for row in usable) / len(usable)
                for key in weights
            }
            if usable
            else None
        )
        if dimensions is not None:
            valid.append(dimensions)
        platform_reasons = Counter(reason for _, _, reason in entries if reason)
        platforms.append(
            {
                "platform": platform,
                "value": rounded(weighted(dimensions, weights)) if dimensions else None,
                "planned": sum(cell["scope"] == "eligible" for cell, _, _ in entries),
                "scored": len(usable),
                "missing_reasons": dict(platform_reasons),
            }
        )
    dimensions = (
        {key: sum(row[key] for row in valid) / len(valid) for key in weights}
        if valid
        else None
    )
    eligible = sum(item["planned"] for item in platforms)
    return {
        "value": rounded(weighted(dimensions, weights)) if dimensions else None,
        "no_score_reason": None
        if dimensions
        else ("no_verified_samples" if eligible else "no_eligible_questions"),
        "dimensions": [
            {
                "dimension": key,
                "weight": weight,
                "value": rounded(dimensions[key]) if dimensions else None,
                "points": round(float(dimensions[key] * weight / 100), 1)
                if dimensions
                else None,
            }
            for key, weight in weights.items()
        ],
        "coverage": {
            "capture_planned": len(cells),
            "planned": eligible,
            "scored": sum(item["scored"] for item in platforms),
            "scored_sample_ids": [
                cell["sample_id"]
                for entries in by_platform.values()
                for cell, _, reason in entries
                if reason is None
            ],
            "planned_platforms": sum(item["planned"] > 0 for item in platforms),
            "scored_platforms": len(valid),
            "platforms": platforms,
            "missing_reasons": dict(reasons),
        },
    }


def recommendations(cells, own_rows, other_rows, weights):
    groups = {}
    scope_counts = Counter((cell["platform"], cell["scenario"]) for cell in cells)
    gap_counts = Counter()
    for cell in cells:
        sample_id = cell["sample_id"]
        own, other = own_rows[sample_id], other_rows[sample_id]
        if not other.get("evidence"):
            continue
        for dimension in weights:
            title, task = TASKS[dimension]
            gap = raw_dimension(other, dimension) - raw_dimension(own, dimension)
            if gap <= 0:
                continue
            key = (cell["platform"], cell["scenario"], dimension)
            gap_counts[key] += 1
            priority = gap * weights[dimension] / 100
            suggestion = {
                "id": stable_hash(
                    [
                        "visibility-recommendation",
                        sample_id,
                        other["evidence"][0]["signal_id"],
                        dimension,
                    ]
                ),
                "dimension": dimension,
                "gap": rounded(gap),
                "priority_points": rounded(priority),
                "platform": cell["platform"],
                "scenario": cell["scenario"],
                "prompt_id": cell["prompt_id"],
                "prompt": cell["prompt"],
                "sample_id": sample_id,
                "response_id": other["response_id"],
                "title": title,
                "deliverable": task,
                "observed_reason": other["evidence"][0]["quote"],
                "own_dimension_value": rounded(raw_dimension(own, dimension)),
                "comparison_dimension_value": rounded(raw_dimension(other, dimension)),
                "success_check": "形成可访问、依据可核验的内容条目，并用同平台同题型独立复测。",
                "retest": {
                    "platform": cell["platform"],
                    "prompt_id": cell["prompt_id"],
                    "metric": dimension,
                },
                "evidence": other["evidence"][:3],
                "source_support": other["source_support"][:3],
                "source_basis": "linked_source"
                if other["source_support"]
                else "answer_only",
            }
            if key not in groups or priority > groups[key][0]:
                groups[key] = (priority, suggestion)
    return [
        {
            **item,
            "observed_gap_samples": gap_counts[
                (item["platform"], item["scenario"], item["dimension"])
            ],
            "common_samples_in_scope": scope_counts[item["platform"], item["scenario"]],
        }
        for _, item in sorted(
            groups.values(), key=lambda value: (-value[0], value[1]["id"])
        )
    ]


def link_action_opportunities(
    suggestions, opportunities, own_target, comparison_target
):
    """Link only existing tasks with the same scope and verified answer span."""
    by_scope = defaultdict(list)
    for opportunity in opportunities:
        pack = opportunity.get("task_pack")
        if opportunity.get("own_target_id") != own_target or not pack:
            continue
        answer_refs, snapshot_refs = defaultdict(list), defaultdict(list)
        for item in pack.get("diagnostic_evidence", ()):
            if item["owner_kind"] == "answer" and item.get("quote"):
                answer_refs[item["owner_id"]].append(item["quote"])
            if item["owner_kind"] == "snapshot" and item.get("quote"):
                snapshot_refs[item["owner_id"]].append(item["quote"])
        for scope in opportunity.get("subtargets", ()):
            if scope.get("comparison_target_id") == comparison_target:
                by_scope[scope["platform"], scope["response_id"]].append(
                    (
                        opportunity["id"],
                        scope["scenario_id"],
                        answer_refs[scope["response_id"]],
                        snapshot_refs,
                    )
                )
    for suggestion in suggestions:
        quotes = [item["quote"] for item in suggestion["evidence"]]
        for opportunity_id, scenario, proofs, snapshots in by_scope[
            suggestion["platform"], suggestion["response_id"]
        ]:
            if not any(
                proof in quote or quote in proof for proof in proofs for quote in quotes
            ):
                continue
            support = suggestion.get("source_support", ())
            if support and not any(
                row["source_quote"] in quote
                for row in support
                for quote in snapshots[row["snapshot_id"]]
            ):
                continue
            suggestion["action_opportunity"] = {
                "id": opportunity_id,
                "scenario": scenario,
            }
            break
    return suggestions


def score_projection(
    payload,
    *,
    own_target_id=None,
    comparison_target_id=None,
    platform=None,
    scenario=None,
    opportunities=(),
    body_entity_ids=None,
):
    targets = {
        row["target_id"]: row for row in payload["targets"]
        if body_entity_ids is None or brand_target_visible(row, body_entity_ids)
    }
    own = [row for row in targets.values() if row["role"] == "own"]
    if own_target_id is None and own:
        own_target_id = own[0]["target_id"]
    if own_target_id is not None and own_target_id not in {
        row["target_id"] for row in own
    }:
        raise HTTPException(404, "Own target not found for this audit")
    if comparison_target_id is not None and comparison_target_id not in targets:
        raise HTTPException(404, "Comparison target not found for this audit")
    if comparison_target_id is not None and comparison_target_id == own_target_id:
        raise HTTPException(400, "Choose a different comparison target")

    cells = payload["cells"]
    scoped = [
        cell
        for cell in cells
        if (platform is None or cell["platform"] == platform)
        and (scenario is None or cell["scenario"] == scenario)
    ]
    observations = payload["observations"]
    weights = payload["weights"]
    own_rows = observations.get(own_target_id, {})
    score = score_rows(cells, own_rows, weights=weights)
    scope_score = (
        score_rows(scoped, own_rows, weights=weights) if platform or scenario else None
    )
    if own_target_id is None:
        score["no_score_reason"] = "own_brand_not_configured"
        if scope_score:
            scope_score["no_score_reason"] = "own_brand_not_configured"
    own_samples = {
        sample_id for sample_id, row in own_rows.items() if "dimensions" in row
    }
    scope_ids = {cell["sample_id"] for cell in scoped if cell["scope"] == "eligible"}
    own_samples &= scope_ids
    if comparison_target_id is None and own_samples:
        candidates = []
        for target_id, target in targets.items():
            if target["role"] == "own":
                continue
            candidate_rows = observations[target_id]
            paired = [
                sample_id
                for sample_id in sorted(own_samples)
                if "dimensions" in candidate_rows.get(sample_id, {})
            ]
            if paired:
                gap = sum(
                    weighted(
                        {
                            key: raw_dimension(candidate_rows[sample_id], key)
                            - raw_dimension(own_rows[sample_id], key)
                            for key in weights
                        },
                        weights,
                    )
                    for sample_id in paired
                ) / len(paired)
                candidates.append((-int(gap > 0), -len(paired), -gap, target_id))
        if candidates:
            comparison_target_id = min(candidates)[-1]
    other_rows = observations.get(comparison_target_id, {})
    other_samples = {
        sample_id
        for sample_id, row in other_rows.items()
        if sample_id in scope_ids and "dimensions" in row
    }
    common_ids = own_samples & other_samples
    common = [cell for cell in scoped if cell["sample_id"] in common_ids]
    comparison = None
    if comparison_target_id is not None:
        comparison = {
            "target_id": comparison_target_id,
            "common_sample_count": len(common),
            "sample_ids": [cell["sample_id"] for cell in common],
            "scope_planned": len(scope_ids),
            "own_only_count": len(own_samples - other_samples),
            "comparison_only_count": len(other_samples - own_samples),
            "no_score_reason": None if common else "no_common_verified_samples",
            "own": score_rows(common, own_rows, weights=weights) if common else None,
            "other": score_rows(common, other_rows, weights=weights)
            if common
            else None,
        }
    return {
        "rule_version": payload["rule_version"],
        "scope_hash": payload["scope_hash"],
        "source_coverage": payload["source_coverage"],
        "own_target_id": own_target_id,
        "targets": list(targets.values()),
        "score": score,
        "scope_score": scope_score,
        "comparison": comparison,
        "recommendations": link_action_opportunities(
            recommendations(common, own_rows, other_rows, weights),
            opportunities,
            own_target_id,
            comparison_target_id,
        ),
    }
