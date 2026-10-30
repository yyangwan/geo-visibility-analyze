"""Frozen comparison denominators and deterministic scoring, independent of LLMs."""

from collections import Counter, defaultdict
from fractions import Fraction
from math import floor

from app.intelligence.brand_ranking import verified_ranking
from app.intelligence.question_context import VERSION as CONTEXT_VERSION
from app.intelligence.question_context import question_proposal
from app.intelligence.semantics import stable_hash

VERSION = "brand-visibility-v1"
COHORT_VERSION = "brand-cohort-v2"
WEIGHTS = {"presence": 30, "recommendation": 30, "position": 20, "reasons": 20}


def freeze_cohort(samples, brands):
    """All planned cells, including failed/pending ones; never use response count.

    Category is only a screening hint, not proof that a question is neutral.
    Human/semantic eligibility review must precede competitive total publication.
    """
    names = sorted(
        {
            name.strip().casefold()
            for brand in brands
            for name in [brand.get("name", ""), *(brand.get("aliases") or [])]
            if isinstance(name, str) and name.strip()
        }
    )
    cells, seen = [], set()
    for sample in sorted(
        samples, key=lambda item: (item["sample_index"], item["sample_id"])
    ):
        key = (sample["prompt_hash"], sample["platform"])
        if key in seen:
            continue
        seen.add(key)
        context = question_proposal(sample["prompt"], names)
        directed = any(row["fixed_exclusion"] for row in context["registered_references"])
        cells.append(
            {
                **sample,
                "question_context": context,
                "primary_scenario": None,
                "eligibility": "excluded" if directed else "needs_review",
                "eligibility_reason": "named_registered_brand"
                if directed
                else "registered_name_ambiguous" if context["registered_references"] else "neutrality_not_verified",
            }
        )
    candidates = [cell for cell in cells if cell["eligibility"] != "excluded"]
    platforms = {cell["platform"] for cell in candidates}
    categories = defaultdict(set)
    counts = Counter((cell["platform"], cell["category"]) for cell in candidates)
    for cell in candidates:
        categories[cell["platform"]].add(cell["category"])
    for cell in cells:
        denominator = (
            (
                len(platforms)
                * len(categories[cell["platform"]])
                * counts[(cell["platform"], cell["category"])]
            )
            if cell["eligibility"] != "excluded"
            else 1
        )
        cell["weight"] = {
            "numerator": 0 if cell["eligibility"] == "excluded" else 1,
            "denominator": denominator,
        }
    frozen = {
        "version": COHORT_VERSION,
        "rule_version": VERSION,
        "weight_policy": "equal-platform-category-prompt-v1",
        "eligibility_policy": "registered-reference-screen-review-required-v2",
        "question_context_version": CONTEXT_VERSION,
        "cells": sorted(
            cells, key=lambda item: (item["platform"], item["prompt_hash"])
        ),
        "status": "requires_prompt_review" if candidates else "no_competitive_prompts",
    }
    return {**frozen, "hash": stable_hash(frozen)}


def score_observation(
    *,
    complete,
    identity_confirmed,
    present,
    recommendation=None,
    rank=None,
    list_size=None,
    reasons=None,
    evidence_ids=(),
    mention_evidence_ids=(),
):
    dimensions = dict.fromkeys(WEIGHTS)
    if not identity_confirmed or present is None:
        return {"score": None, "dimensions": dimensions, "status": "pending"}
    if not present:
        if not complete:
            return {"score": None, "dimensions": dimensions, "status": "pending"}
        return {
            "score": 0,
            "dimensions": dict.fromkeys(WEIGHTS, 0),
            "status": "complete",
        }
    if not evidence_ids and not mention_evidence_ids:
        return {"score": None, "dimensions": dimensions, "status": "pending"}
    dimensions["presence"] = 100
    if not complete:
        return {"score": None, "dimensions": dimensions, "status": "partial"}
    if type(recommendation) is int and 0 <= recommendation <= 4:
        dimensions["recommendation"] = recommendation * 25
    if type(rank) is int and type(list_size) is int and 1 <= rank <= list_size:
        dimensions["position"] = (
            100 if list_size == 1 else 100 * (list_size - rank) / (list_size - 1)
        )
    dimensions["reasons"] = {
        "none": 0,
        "generic": 25,
        "specific": 75,
        "need_matched": 100,
    }.get(reasons)
    available = all(value is not None for value in dimensions.values())
    score = (
        floor(
            sum(dimensions[key] * weight / 100 for key, weight in WEIGHTS.items()) + 0.5
        )
        if available
        else None
    )
    return {
        "score": score,
        "dimensions": dimensions,
        "status": "complete" if available else "partial",
    }


def aggregate_scores(cohort, scores):
    """Scores keyed by frozen sample ID; missing cells keep their original weight."""
    cells = [cell for cell in cohort["cells"] if cell["weight"]["numerator"]]
    weights = [
        Fraction(cell["weight"]["numerator"], cell["weight"]["denominator"])
        for cell in cells
    ]
    if cells and (
        sum(weights) != 1
        or any(weight <= 0 for weight in weights)
        or len({cell["sample_id"] for cell in cells}) != len(cells)
    ):
        raise ValueError("Invalid frozen evaluation weights")
    total, covered = Fraction(), Fraction()
    missing = []
    for cell in cells:
        weight = Fraction(cell["weight"]["numerator"], cell["weight"]["denominator"])
        score = scores.get(cell["sample_id"])
        if (
            not isinstance(score, (int, float))
            or isinstance(score, bool)
            or not 0 <= score <= 100
        ):
            missing.append(cell["sample_id"])
            continue
        covered += weight
        total += weight * Fraction(str(score))
    publishable = (
        bool(cells)
        and not missing
        and all(cell["eligibility"] == "eligible" for cell in cells)
    )
    return {
        "rule_version": VERSION,
        "cohort_hash": cohort["hash"],
        "score": floor(total + Fraction(1, 2)) if publishable else None,
        "covered_weight": float(covered),
        "missing_sample_ids": missing,
        "status": "complete" if publishable else "pending",
    }


def summarize_answer_scores(manifest, applications, entities, mentions, signals):
    """Conservative whole-answer projection; no majority vote across contradictions."""
    from app.intelligence.analysis_coverage import answer_coverage, related_span_defects

    proofs = answer_coverage(manifest, applications)
    rows = []
    cohort = manifest.get("evaluation_cohort")
    for response in manifest["responses"]:
        proof = proofs[response["id"]]
        complete = proof["input_processed"]
        answer_mentions = [
            item
            for item in mentions
            if item["owner_kind"] == "answer" and item["owner_id"] == response["id"]
        ]
        ranking = (
            verified_ranking(
                response["text"],
                [
                    item
                    for item in signals["observations"]
                    if item["response_id"] == response["id"]
                ],
            )
            if complete
            else {}
        )
        for entity in entities:
            if entity["kind"] not in ("brand", "organization", "product"):
                continue
            if (entity.get("role", "unregistered") == "unregistered"
                and entity.get("comparison_context_status") == "not_solution"):
                continue
            observed = [
                item
                for item in signals["observations"]
                if item["response_id"] == response["id"]
                and item["entity_id"] == entity["id"]
            ]
            presence = any(
                item["entity_id"] == entity["id"] for item in answer_mentions
            )
            identity_ok = (
                entity["identity_status"] == "confirmed"
                and not entity.get("role_conflict")
                and not (entity.get("role", "unregistered") == "unregistered"
                         and entity.get("comparison_context_status") == "unknown")
                and not (entity.get("role", "unregistered") == "unregistered"
                         and "answer_context_roles" in entity
                         and presence
                         and "evaluated_solution" not in entity["answer_context_roles"].get(response["id"], []))
                and not any(
                    item["identity_ambiguity"]
                    for item in answer_mentions
                    if item["entity_id"] == entity["id"]
                )
            )
            values = {}
            conflicts = []
            for field in ("recommendation", "reason_level", "explicit_rank"):
                distinct = {item[field] for item in observed if item[field] is not None}
                values[field] = next(iter(distinct)) if len(distinct) == 1 else None
                if len(distinct) > 1:
                    conflicts.append(field)
            result = score_observation(
                complete=complete
                and not related_span_defects(
                    proof,
                    [
                        entity.get("name", ""),
                        entity.get("canonical_name", ""),
                        *entity.get("observed_names", []),
                    ],
                ),
                identity_confirmed=identity_ok,
                present=presence,
                recommendation=values["recommendation"],
                reasons=values["reason_level"],
                rank=ranking.get(entity["id"], {}).get("rank")
                if not conflicts
                else None,
                list_size=ranking.get(entity["id"], {}).get("list_size")
                if not conflicts
                else None,
                evidence_ids=[item["id"] for item in observed],
                mention_evidence_ids=[
                    item["id"]
                    for item in answer_mentions
                    if item["entity_id"] == entity["id"] and item.get("id")
                ],
            )
            rows.append(
                {
                    "entity_id": entity["id"],
                    "response_id": response["id"],
                    "sample_id": response.get("sample_id"),
                    "platform": response["platform"],
                    **result,
                    "conflicting_fields": conflicts,
                    "evidence_ids": [item["id"] for item in observed],
                    "position_status": "verified_explicit_list"
                    if entity["id"] in ranking and not conflicts
                    else "whole_answer_list_not_verified",
                    "position_evidence": ranking.get(entity["id"]),
                    "interpretation": "model_extracted_unreviewed",
                }
            )
    brand_scores = []
    if cohort:
        for entity in entities:
            if entity["kind"] not in ("brand", "organization", "product"):
                continue
            if (entity.get("role", "unregistered") == "unregistered"
                and entity.get("comparison_context_status") == "not_solution"):
                continue
            entity_rows = [row for row in rows if row["entity_id"] == entity["id"]]
            by_sample = defaultdict(list)
            for row in entity_rows:
                if row["sample_id"]:
                    by_sample[row["sample_id"]].append(row["score"])
            aggregate = aggregate_scores(
                cohort,
                {
                    key: values[0]
                    for key, values in by_sample.items()
                    if len(values) == 1
                },
            )
            brand_scores.append({"entity_id": entity["id"], **aggregate})
    from app.intelligence.competitive_comparison import competitive_breakdown

    return {
        "version": VERSION,
        "weights": WEIGHTS,
        "comparison": competitive_breakdown(cohort, rows, entities),
        "cohort_hash": cohort["hash"] if cohort else None,
        "status": (
            "complete"
            if brand_scores
            and all(item["status"] == "complete" for item in brand_scores)
            else "partial"
        )
        if cohort and cohort["status"] == "reviewed"
        else "no_competitive_prompts"
        if cohort and cohort["status"] == "no_competitive_prompts"
        else "requires_prompt_review"
        if cohort
        else "missing_planned_cohort",
        "score": None,
        "answer_scores": rows,
        "brand_scores": brand_scores,
    }
