"""Registry targets survive empty extraction; observations never invent entities."""

from collections import Counter, defaultdict

from app.intelligence.brand_evaluation import (
    WEIGHTS,
    aggregate_scores,
    score_observation,
)
from app.intelligence.project_context import bound_roles
from app.intelligence.semantics import stable_hash

VERSION = "brand-presence-v1"


def freeze_registered_targets(brands):
    return [
        {
            "target_id": "registry:" + str(brand["id"]),
            "registry_brand_id": str(brand["id"]),
            "name": brand["name"],
            "names": sorted(
                {
                    name.strip()
                    for name in [brand["name"], *(brand.get("aliases") or [])]
                    if isinstance(name, str) and name.strip()
                }
            ),
            "role": "comparison"
            if brand.get("analysis_role") == "comparison"
            else "competitor"
            if brand.get("is_competitor")
            else "own",
            "binding_entity_id": brand.get("entity_id"),
            "identity_version": brand.get("identity_version"),
        }
        for brand in sorted(brands, key=lambda item: str(item.get("id", "")))
        if brand.get("id") and brand.get("name", "").strip()
    ]


def build_brand_presence(
    manifest, applications, entities, mentions, signals, evaluation
):
    registered = manifest.get("brand_targets", [])
    entity_index = {
        entity["id"]: entity
        for entity in entities
        if entity["kind"] in ("brand", "organization", "product")
    }
    name_counts = Counter(
        name
        for target in registered
        for name in {name.casefold() for name in target["names"]}
    )
    binding_counts = Counter(
        target["binding_entity_id"]
        for target in registered
        if target.get("binding_entity_id")
    )
    targets, consumed = [], set()
    for frozen in registered:
        target = {
            **frozen,
            "entity_id": None,
            "identity_status": "confirmed",
            "reason_codes": [],
        }
        binding = frozen.get("binding_entity_id")
        if any(name_counts[name.casefold()] > 1 for name in frozen["names"]):
            target.update(
                identity_status="ambiguous", reason_codes=["registered_name_collision"]
            )
        elif binding:
            brand = {
                "entity_id": binding,
                "identity_version": frozen["identity_version"],
                "is_competitor": frozen["role"] == "competitor",
                "analysis_role": "comparison"
                if frozen["role"] == "comparison"
                else "registered",
            }
            scoped_manifest = {**manifest, "brand_context": {"brands": [brand]}}
            if binding_counts[binding] != 1 or frozen["role"] not in bound_roles(
                scoped_manifest, binding
            ):
                target.update(
                    identity_status="pending",
                    reason_codes=["registered_binding_not_confirmed"],
                )
            else:
                target["entity_id"] = binding
                consumed.add(binding)
        else:
            names = {name.casefold() for name in frozen["names"]}
            candidates = [
                entity["id"]
                for entity in entity_index.values()
                if names.intersection(
                    name.casefold()
                    for name in [entity["name"], *(entity.get("observed_names") or [])]
                )
            ]
            if candidates:
                target.update(
                    identity_status="pending",
                    reason_codes=["registered_binding_required"],
                    candidate_entity_ids=sorted(candidates),
                )
        bound_entity = entity_index.get(target["entity_id"])
        if bound_entity:
            target["names"] = sorted(
                set(target["names"])
                | {
                    name
                    for name in [
                        bound_entity["name"],
                        bound_entity.get("canonical_name"),
                        *(bound_entity.get("observed_names") or []),
                    ]
                    if name
                }
            )
        targets.append(target)
    for entity_id, entity in sorted(entity_index.items()):
        if entity_id not in consumed:
            targets.append(
                {
                    "target_id": "entity:" + entity_id,
                    "registry_brand_id": None,
                    "entity_id": entity_id,
                    "name": entity["name"],
                    "names": sorted(
                        {
                            name
                            for name in [
                                entity["name"],
                                entity.get("canonical_name"),
                                *(entity.get("observed_names") or []),
                            ]
                            if name
                        }
                    ),
                    "role": entity.get("role", "unregistered"),
                    "identity_status": "ambiguous"
                    if entity.get("role_conflict")
                    else entity.get("identity_status", "provisional"),
                    "reason_codes": [],
                }
            )
    from app.intelligence.analysis_coverage import answer_coverage, related_span_defects

    proofs = answer_coverage(manifest, applications)
    complete = {key: value["input_processed"] for key, value in proofs.items()}
    cohort = manifest.get("evaluation_cohort")
    cells = (
        cohort["cells"]
        if cohort
        else [
            {
                "sample_id": response.get("sample_id") or response["id"],
                "platform": response["platform"],
            }
            for response in manifest["responses"]
        ]
    )
    by_sample = defaultdict(list)
    for response in manifest["responses"]:
        by_sample[response.get("sample_id") or response["id"]].append(response)
    entity_scores = {
        (row["entity_id"], row["response_id"]): row
        for row in evaluation.get("answer_scores", [])
    }
    observations = []
    mapping_hash = stable_hash(targets)
    for target in targets:
        for cell in cells:
            responses = by_sample[cell["sample_id"]]
            response = responses[0] if len(responses) == 1 else None
            related = [
                mention
                for mention in mentions
                if response
                and mention["owner_kind"] == "answer"
                and mention["owner_id"] == response["id"]
                and target["entity_id"] is not None
                and mention["entity_id"] == target["entity_id"]
            ]
            reasons = list(target["reason_codes"])
            if not response:
                reasons.append(
                    "answer_missing" if not responses else "multiple_answer_records"
                )
            elif (
                response["error"]
                or response.get("answer_status", "complete") != "complete"
            ):
                reasons.append("answer_incomplete")
            if response and not complete.get(response["id"], False):
                reasons.append("semantic_coverage_incomplete")
            identity_ok = target["identity_status"] == "confirmed" and not any(
                mention.get("identity_ambiguity") for mention in related
            )
            if not identity_ok:
                reasons.append("identity_unconfirmed")
            target_complete = bool(response and complete.get(response["id"]))
            if response and related_span_defects(
                proofs[response["id"]], target["names"]
            ):
                target_complete = False
                reasons.append("related_semantic_span_unresolved")
            state, evidence_ids = "unknown", []
            if related and identity_ok:
                state = "present"
                evidence_ids = sorted({mention["id"] for mention in related})
            elif response and target_complete and identity_ok:
                unmatched_name = any(
                    name.casefold() in response["text"].casefold()
                    for name in target["names"]
                )
                unmatched_signal = any(
                    item.get("entity_id") == target["entity_id"]
                    and target["entity_id"] is not None
                    and item["response_id"] == response["id"]
                    for item in signals.get("observations", [])
                )
                if unmatched_name or unmatched_signal:
                    reasons.append("name_or_signal_without_resolved_mention")
                else:
                    state = "observed_absent"
            result = score_observation(
                complete=False, identity_confirmed=identity_ok, present=None
            )
            if state == "observed_absent":
                result = score_observation(
                    complete=True, identity_confirmed=True, present=False
                )
            elif state == "present" and response:
                result = entity_scores.get(
                    (target["entity_id"], response["id"]), result
                )
            observations.append(
                {
                    "target_id": target["target_id"],
                    "entity_id": target["entity_id"],
                    "sample_id": cell["sample_id"],
                    "response_id": response["id"] if response else None,
                    "platform": cell["platform"],
                    "state": state,
                    "reason_codes": sorted(set(reasons)),
                    "evidence_ids": evidence_ids,
                    "coverage_complete": target_complete,
                    "score": result["score"],
                    "dimensions": result["dimensions"],
                }
            )
    scores = []
    if cohort:
        from app.intelligence.competitive_comparison import scoped_cohort

        for platform in ["all", *sorted({cell["platform"] for cell in cells})]:
            subset = scoped_cohort(
                cohort, "overall" if platform == "all" else "platform", platform
            )
            for target in targets:
                rows = [
                    row
                    for row in observations
                    if row["target_id"] == target["target_id"]
                    and (platform == "all" or row["platform"] == platform)
                ]
                scores.append(
                    {
                        "target_id": target["target_id"],
                        "platform": platform,
                        "total": aggregate_scores(
                            subset, {row["sample_id"]: row["score"] for row in rows}
                        ),
                        "dimensions": {
                            dimension: aggregate_scores(
                                subset,
                                {
                                    row["sample_id"]: row["dimensions"][dimension]
                                    for row in rows
                                },
                            )
                            for dimension in WEIGHTS
                        },
                    }
                )
    for target in targets:
        rows = [row for row in observations if row["target_id"] == target["target_id"]]
        target["presence_counts"] = dict(Counter(row["state"] for row in rows))
        target["source_mentions"] = sum(
            mention["owner_kind"] != "answer"
            and target["entity_id"] is not None
            and mention["entity_id"] == target["entity_id"]
            for mention in mentions
        )
    return {
        "version": VERSION,
        "mapping_hash": mapping_hash,
        "targets": targets,
        "observations": observations,
        "scope_scores": scores,
    }
