"""Read-only comparison projections over one frozen analysis revision."""

from collections import Counter, defaultdict
from hashlib import sha256
from urllib.parse import urlsplit

from app.intelligence.answer_brands import (
    answer_brand_mentions,
    answer_prose,
    brand_target_visible,
    discovered_brands,
)
from app.intelligence.brand_ranking import verified_ranking
from app.intelligence.visibility_scoring import (
    RULE_VERSION,
    WEIGHTS,
    checked_span,
    score_rows,
    supports_observation,
)

RECOMMENDATION = {
    0: "not_recommended",
    1: "mentioned",
    2: "alternative",
    3: "recommended",
    4: "priority",
}
SOURCE_FIELDS = (
    "id",
    "title",
    "url",
    "kind",
    "fetch_status",
    "snapshot_id",
    "snippet",
    "ordinal",
)


def question_catalog(manifest):
    responses = manifest.get("responses", ())
    responses_by_sample = {
        row.get("sample_id"): row for row in responses if row.get("sample_id")
    }
    cohort = manifest.get("evaluation_cohort") or {}
    scenarios = {
        row["prompt_hash"]: row.get("primary_scenario")
        for row in cohort.get("cells", ())
    }
    planned = (manifest.get("visibility_score_scope") or {}).get("cells", [])
    questions, samples = {}, defaultdict(list)
    for row in planned or responses:
        response = responses_by_sample.get(row["sample_id"]) if planned else row
        prompt = row["prompt"]
        prompt_hash = (response or row).get("prompt_hash") or sha256(
            prompt.encode()
        ).hexdigest()
        key = row.get("prompt_version_id") or prompt_hash
        question = questions.setdefault(
            key,
            {
                "id": key,
                "prompt": prompt,
                "prompt_hash": prompt_hash,
                "scenario": row.get("scenario") or scenarios.get(prompt_hash),
                "scope": row.get("scope"),
                "platforms": [],
            },
        )
        if row["platform"] not in question["platforms"]:
            question["platforms"].append(row["platform"])
        samples[key].append((row, response))
    return list(questions.values()), samples


def comparison_targets(manifest, summary, brands):
    ids = {row["id"] for row in brands}
    scoring = summary.get("visibility_score") or {}
    targets = [
        {**target, "identity_method": target.get("identity_method", "unresolved")}
        for target in scoring.get(
            "targets", (summary.get("brand_presence") or {}).get("targets", ())
        )
        if brand_target_visible(target, ids)
    ]
    if not targets:
        targets = [
            {
                "target_id": item["target_id"],
                "registry_brand_id": item["registry_brand_id"],
                "entity_id": None,
                "name": item["name"],
                "role": item["role"],
                "identity_status": "provisional",
                "identity_method": "unresolved",
            }
            for item in manifest.get("brand_targets", ())
            if item.get("registry_brand_id")
        ]
    represented = {row.get("entity_id") for row in targets}
    targets.extend(
        {
            "target_id": "entity:" + row["id"],
            "entity_id": row["id"],
            "registry_brand_id": None,
            "name": row["name"],
            "role": "unregistered",
            "identity_status": row.get("identity_status", "provisional"),
            "identity_method": "unresolved",
        }
        for row in brands
        if row["id"] not in represented and row.get("identity_status") != "excluded"
    )
    return targets


def scope_matches(platform, scenario, selected_platform, selected_scenario):
    return (not selected_platform or platform == selected_platform) and (
        not selected_scenario
        or scenario
        == (None if selected_scenario == "unreviewed" else selected_scenario)
    )


def pair_scores(cells, own_rows, other_rows, weights):
    paired = [
        row
        for row in cells
        if row["scope"] == "eligible"
        and "dimensions" in own_rows.get(row["sample_id"], {})
        and "dimensions" in other_rows.get(row["sample_id"], {})
    ]
    if not paired:
        return {"count": 0, "own": None, "other": None}
    return {
        "count": len(paired),
        "own": score_rows(paired, own_rows, weights=weights),
        "other": score_rows(paired, other_rows, weights=weights),
    }


def gap_rows(cells, own_rows, other_rows, samples_by_question, weights):
    by_sample = {
        sample.get("sample_id"): key
        for key, rows in samples_by_question.items()
        for sample, _ in rows
    }
    picks, matrices = {}, {"platform": [], "scenario": []}
    for axis, matrix in matrices.items():
        groups = defaultdict(list)
        for cell in cells:
            groups[cell.get(axis)].append(cell)
        for value, group in groups.items():
            paired = pair_scores(group, own_rows, other_rows, weights)
            matrix.append(
                {
                    "key": value,
                    "planned": sum(row["scope"] == "eligible" for row in group),
                    "common": paired["count"],
                    "own": paired["own"]["value"] if paired["own"] else None,
                    "other": paired["other"]["value"] if paired["other"] else None,
                }
            )
    for row in cells:
        sample_id = row["sample_id"]
        own, other = own_rows.get(sample_id, {}), other_rows.get(sample_id, {})
        if (
            row["scope"] != "eligible"
            or "dimensions" not in own
            or "dimensions" not in other
        ):
            continue
        gap = sum(
            (other["dimensions"][dim] - own["dimensions"][dim]) * weight / 100
            for dim, weight in weights.items()
        )
        key = by_sample.get(sample_id)
        if gap > 0 and key:
            picks[sample_id] = {
                "question_id": key,
                "platform": row["platform"],
                "gap": round(gap, 1),
                "own_response_id": own.get("response_id"),
            }
    return matrices, sorted(picks.values(), key=lambda row: -row["gap"])[:10]


def brand_workspace(
    manifest,
    summary,
    targets,
    catalog,
    samples_by_question,
    own_target_id,
    comparison_target_id,
    platform,
    scenario,
    run_status,
):
    scoring = summary.get("visibility_score") or {}
    observations = scoring.get("observations", {})
    default_owns = [row["target_id"] for row in targets if row["role"] == "own"]
    own_target_id = own_target_id or (
        default_owns[0] if len(default_owns) == 1 else None
    )
    weights = scoring.get("weights") or WEIGHTS
    cells = [
        row
        for row in scoring.get("cells", ())
        if scope_matches(row["platform"], row.get("scenario"), platform, scenario)
    ]
    question_by_id = {row["id"]: row for row in catalog}
    scoped_answers = {
        response["id"]: response
        for key, rows in samples_by_question.items()
        for sample, response in rows
        if response
        and not response.get("error")
        and scope_matches(
            sample["platform"], question_by_id[key]["scenario"], platform, scenario
        )
    }
    mentions, recommendation_levels = defaultdict(set), defaultdict(set)
    anchored_ids = set()
    for row in answer_brand_mentions(
        manifest, summary.get("entities", ()), summary.get("mentions", ())
    ):
        if row["owner_id"] in scoped_answers:
            mentions[row["entity_id"]].add(row["owner_id"])
            if not row.get("identity_ambiguity"):
                anchored_ids.add(row["id"])
    for row in (summary.get("brand_signals") or {}).get("observations", ()):
        if (
            row.get("recommendation") is not None
            and row["response_id"] in mentions.get(row.get("entity_id"), ())
            and anchored_ids.intersection(row.get("mention_ids", ()))
        ):
            recommendation_levels[row["entity_id"], row["response_id"]].add(
                row["recommendation"]
            )
    signals = Counter(
        entity_id
        for (entity_id, _), levels in recommendation_levels.items()
        if levels in ({3}, {4})
    )
    own_rows = (
        observations.get(own_target_id, {}) if own_target_id in default_owns else {}
    )
    if not comparison_target_id and own_rows:
        comparable = [
            sample["sample_id"]
            for sample in cells
            if sample["scope"] == "eligible"
            and "dimensions" in own_rows.get(sample["sample_id"], {})
        ]
        alternatives = sorted(
            (row for row in targets if row["role"] != "own"),
            key=lambda row: row["role"] != "competitor",
        )
        comparison_target_id = next(
            (
                row["target_id"]
                for row in alternatives
                if any(
                    "dimensions"
                    in observations.get(row["target_id"], {}).get(sample, {})
                    for sample in comparable
                )
            ),
            None,
        )
    available_other = {row["target_id"] for row in targets if row["role"] != "own"}
    other_rows = (
        observations.get(comparison_target_id, {})
        if comparison_target_id in available_other
        else {}
    )
    matrices, gaps = gap_rows(cells, own_rows, other_rows, samples_by_question, weights)
    return {
        "rule_version": scoring.get("rule_version") or RULE_VERSION,
        "own_target_id": own_target_id,
        "comparison_target_id": comparison_target_id,
        "observations_available": run_status in ("ready", "partial")
        and "mentions" in summary,
        "own": score_rows(cells, own_rows, weights=weights)
        if own_target_id and scoring
        else None,
        "pair": pair_scores(cells, own_rows, other_rows, weights),
        "coverage": {
            "planned": sum(
                scope_matches(
                    sample["platform"],
                    question_by_id[key]["scenario"],
                    platform,
                    scenario,
                )
                for key, rows in samples_by_question.items()
                for sample, _ in rows
            ),
            "received": len(scoped_answers),
        },
        "matrices": matrices,
        "gaps": gaps,
        "brands": [
            {
                **brand,
                "answer_count": len(mentions[brand.get("entity_id")]),
                "recommended_count": signals[brand.get("entity_id")],
                "platforms": sorted(
                    {
                        scoped_answers[key]["platform"]
                        for key in mentions[brand.get("entity_id")]
                    }
                ),
                "score": score_rows(
                    cells, observations[brand["target_id"]], weights=weights
                )
                if brand["target_id"] in observations
                else None,
            }
            for brand in targets
        ],
    }


def source_rows(response, manifest, summary, reviews):
    response_id = response["id"]
    observed = {
        row["id"]: row
        for row in (summary.get("source_observations") or {}).get("observations", ())
    }
    evidence = defaultdict(list)
    claim_subjects = {
        (row["segment_id"], row["start"], row["end"], row["quote"]): row.get("subject")
        for row in summary.get("claims", ())
        if row["owner_kind"] == "answer" and row["owner_id"] == response_id
    }
    for item in summary.get("claim_evidence", ()):
        if item.get("response_id") == response_id:
            evidence[item.get("citation_id")].append(item)
    results = []
    for source in manifest.get("sources", ()):
        if (
            source["response_id"] != response_id
            or source.get("kind") != "answer_reference"
        ):
            continue
        observation = observed.get(source["id"], {})
        snapshot_id = source.get("snapshot_id")
        frozen = bool(
            snapshot_id
            and observation.get("frozen_version_valid")
            and (observation.get("content_coverage") or {}).get("stored_text_integrity")
            == "verified"
        )
        proof = []
        if snapshot_id and frozen:
            for item in evidence[source["id"]]:
                if (
                    item.get("snapshot_id") != snapshot_id
                    or not item.get("answer_quote")
                    or not item.get("source_quote")
                ):
                    continue
                verification = item.get("verification") or {}
                span = verification.get("answer_span") or {}
                if (
                    span.get("quote") != item["answer_quote"]
                    or not all(field in span for field in ("start", "end"))
                    or not checked_span(span, response)
                ):
                    continue
                review = reviews.get(item["id"], {})
                proof.append(
                    {
                        "id": item["id"],
                        "answer_quote": item["answer_quote"],
                        "quote": review.get("source_quote") or item["source_quote"],
                        "relation": review.get("relation") or item["relation"],
                        "reviewed": bool(review),
                        "start": span["start"],
                        "end": span["end"],
                        "answer_segment_id": item.get("answer_segment_id"),
                        "association": (verification.get("association") or {}).get(
                            "method"
                        ),
                        "claim_subject": claim_subjects.get(
                            (
                                item.get("answer_segment_id"),
                                span["start"],
                                span["end"],
                                span["quote"],
                            )
                        ),
                    }
                )
        url = source.get("url") or ""
        try:
            domain = urlsplit(url).hostname
        except ValueError:
            domain = None
        source_type = "unknown"
        title = (source.get("title") or "").casefold()
        for category, cues in (
            ("case_study", ("实施案例", "客户案例", "客户故事", "case study")),
            ("review", ("测评", "评测", "横评", "review")),
            ("documentation", ("使用手册", "帮助中心", "操作文档", "user guide")),
            ("discussion", ("问答", "讨论帖", "forum")),
        ):
            if any(cue in title for cue in cues):
                source_type = category
                break
        results.append(
            {
                **{key: source.get(key) for key in SOURCE_FIELDS},
                "domain": domain,
                "content_type": source_type,
                "family_id": (observation.get("content_family") or {}).get("id"),
                "frozen": frozen,
                "anchor": bool(source.get("answer_anchor")),
                "evidence": proof,
            }
        )
    return results


def reason_categories(units, names, start, end):
    return sorted(
        {
            unit["kind"]
            for unit in units
            if unit.get("subject") in names
            and start <= unit["start"] < unit["end"] <= end
        }
    )


def linked_reasons(response, row, trusted, sources, units, names, mentions, text):
    primary = {
        "id": row["id"],
        "quote": row["quote"],
        "categories": reason_categories(units, names, row["start"], row["end"]),
        "evidence": [],
    }
    attached = {}
    folded = {name.strip().casefold() for name in names if name}
    for source in sources:
        for proof in source["evidence"]:
            start, end = proof["start"], proof["end"]
            if end > len(text):
                continue
            reference = {"source_id": source["id"], **proof}
            if row["start"] <= start < end <= row["end"]:
                if not any(
                    other["entity_id"] != row["entity_id"]
                    and other["start"] < end
                    and start < other["end"]
                    for other in mentions
                ):
                    primary["evidence"].append(reference)
                continue
            candidate = {
                **proof,
                "verification": {
                    "answer_span": {
                        "start": start,
                        "end": end,
                        "quote": proof["answer_quote"],
                    },
                    "association": {"method": proof["association"]},
                },
            }
            if row["end"] <= start and supports_observation(
                candidate, response, [row], trusted, folded
            ):
                reason = attached.setdefault(
                    (start, end),
                    {
                        "id": f"{row['id']}:{start}:{end}",
                        "quote": proof["answer_quote"],
                        "categories": reason_categories(units, names, start, end),
                        "evidence": [],
                    },
                )
                reason["evidence"].append(reference)
    return [primary, *attached.values()]


def answer_findings(response, mentions, signals, sources, units, targets):
    text = answer_prose(response["text"])
    target_by_entity = {
        row["entity_id"]: row for row in targets if row.get("entity_id")
    }
    mention_ids = {row["id"] for row in mentions if not row.get("identity_ambiguity")}
    trusted = [
        row
        for row in signals
        if row.get("entity_id")
        and checked_span(row, response)
        and row["end"] <= len(text)
        and mention_ids.intersection(row.get("mention_ids", ()))
    ]
    ranks = (
        verified_ranking(text, trusted, allow_ties=True)
        if response.get("answer_status") == "complete"
        else None
    )
    duplicate_ranks = Counter(row["rank"] for row in (ranks or {}).values())
    by_entity = defaultdict(list)
    for item in mentions:
        by_entity[item["entity_id"]].append(item)
    findings = []
    for entity, hits in by_entity.items():
        related = [row for row in trusted if row["entity_id"] == entity]
        levels = {
            row["recommendation"]
            for row in related
            if row.get("recommendation") is not None
        }
        state = (
            RECOMMENDATION.get(next(iter(levels)), "unknown")
            if len(levels) == 1
            else (
                "unknown"
                if levels or any(row.get("identity_ambiguity") for row in hits)
                else "mentioned"
            )
        )
        rank = (ranks or {}).get(entity)
        if (
            rank
            and state in ("priority", "recommended")
            and duplicate_ranks[rank["rank"]] > 1
        ):
            state = "tied"
        target = target_by_entity.get(entity)
        reasons = []
        for row in related:
            if (
                row.get("reason_level") not in ("generic", "specific", "need_matched")
                or (row.get("recommendation") or 0) < 2
            ):
                continue
            names = {
                name
                for name in (row.get("observed_name"), (target or {}).get("name"))
                if name
            }
            reasons.extend(
                linked_reasons(
                    response, row, trusted, sources, units, names, mentions, text
                )
            )
        findings.append(
            {
                "entity_id": entity,
                "target_id": (target or {}).get("target_id"),
                "name": (target or {}).get("name") or hits[0]["entity_name"],
                "identity_status": (target or {}).get("identity_status", "provisional"),
                "recommendation": state,
                "rank": rank["rank"] if rank else None,
                "quote": (related or hits)[0]["quote"],
                "reasons": reasons,
            }
        )
    return findings


def platform_workspace(
    manifest, summary, targets, question, planned, reviews, run_status
):
    mentions, signals, units = defaultdict(list), defaultdict(list), defaultdict(list)
    response_ids = {row["id"] for _, row in planned if row and not row.get("error")}
    for row in answer_brand_mentions(
        manifest, summary.get("entities", ()), summary.get("mentions", ())
    ):
        if row["owner_id"] in response_ids:
            mentions[row["owner_id"]].append(row)
    for row in (summary.get("brand_signals") or {}).get("observations", ()):
        if row["response_id"] in response_ids:
            signals[row["response_id"]].append(row)
    for row in (summary.get("information_units") or {}).get("observations", ()):
        if row["owner_kind"] == "answer" and row["owner_id"] in response_ids:
            units[row["owner_id"]].append(row)
    result = []
    statuses = manifest.get("sample_statuses", {})
    for cell, response in planned:
        sample_id = cell.get("sample_id")
        if not response or response.get("error"):
            result.append(
                {
                    "sample_id": sample_id,
                    "platform": cell["platform"],
                    "response_id": None,
                    "status": "failed"
                    if response
                    else statuses.get(sample_id, "missing"),
                    "sources": [],
                    "findings": [],
                    "search_mode": None,
                }
            )
            continue
        response_id = response["id"]
        sources = source_rows(response, manifest, summary, reviews)
        analysis_ready = run_status in ("ready", "partial")
        findings = (
            answer_findings(
                response,
                mentions[response_id],
                signals[response_id],
                sources,
                units[response_id],
                targets,
            )
            if analysis_ready
            else []
        )
        observations = (summary.get("visibility_score") or {}).get("observations", {})
        absent_targets = [
            target["target_id"]
            for target in targets
            if analysis_ready
            and observations.get(target["target_id"], {})
            .get(sample_id, {})
            .get("presence")
            == "observed_absent"
        ]
        result.append(
            {
                "sample_id": sample_id,
                "platform": cell["platform"],
                "response_id": response_id,
                "status": "received"
                if response.get("answer_status") == "complete"
                else "partial",
                "search_mode": (response.get("conditions") or {}).get("searchMode"),
                "analysis_ready": analysis_ready,
                "absent_targets": absent_targets,
                "findings": findings,
                "sources": sources,
            }
        )
    return {"question": question, "samples": result}


def project_comparison(
    run,
    *,
    mode,
    own=None,
    other=None,
    platform=None,
    scenario=None,
    question_id=None,
    reviews=None,
):
    manifest, summary = run.manifest, run.summary or {}
    brands = summary.get("answer_brands")
    if brands is None:
        brands = discovered_brands(manifest, summary)
    targets = comparison_targets(manifest, summary, brands)
    questions, samples = question_catalog(manifest)
    result = {
        "run_id": run.id,
        "audit_id": str(run.audit_id),
        "revision": run.revision,
        "targets": targets,
        "questions": questions,
    }
    if mode == "brands":
        result["brands"] = brand_workspace(
            manifest,
            summary,
            targets,
            questions,
            samples,
            own,
            other,
            platform,
            scenario,
            run.status,
        )
    else:
        selected = next((row for row in questions if row["id"] == question_id), None)
        if not question_id:
            selected = next(
                (
                    row
                    for row in questions
                    if scope_matches("", row["scenario"], None, scenario)
                ),
                None,
            )
        result["detail"] = (
            platform_workspace(
                manifest,
                summary,
                targets,
                selected,
                samples[selected["id"]],
                reviews or {},
                run.status,
            )
            if selected
            else None
        )
    return result
