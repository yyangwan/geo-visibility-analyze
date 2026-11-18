from types import SimpleNamespace

import pytest

from app.intelligence.brand_evaluation import freeze_cohort
from app.intelligence.brand_ranking import verified_ranking
from app.intelligence.visibility_scoring import (
    build_visibility_score,
    freeze_visibility_scope,
    link_action_opportunities,
    score_projection,
    score_rows,
)


def plan(sample_id, platform, prompt_id, *, prompt="仓库盘点工具如何选？"):
    return {
        "sample_id": sample_id,
        "platform": platform,
        "prompt_id": str(prompt_id),
        "prompt_version_id": f"version-{prompt_id}",
        "prompt_hash": f"question-{prompt_id}",
        "prompt": prompt,
        "category": "recommend",
        "sample_index": 1,
    }


def answer(cell, text, status="complete"):
    return {
        "id": "answer-" + cell["sample_id"],
        "sample_id": cell["sample_id"],
        "platform": cell["platform"],
        "text": text,
        "answer_status": status,
        "error": False,
    }


def mention(response, entity_id, name, *, ambiguous=False):
    start = response["text"].index(name)
    return {
        "id": f"{response['id']}-{entity_id}",
        "entity_id": entity_id,
        "observed_entity_name": name,
        "owner_kind": "answer",
        "owner_id": response["id"],
        "identity_ambiguity": ambiguous,
        "start": start,
        "end": start + len(name),
        "quote": name,
    }


def signal(response, item, strength, reason, rank=None):
    return {
        "id": f"signal-{item['id']}",
        "response_id": response["id"],
        "entity_id": item["entity_id"],
        "observed_name": item["observed_entity_name"],
        "mention_ids": [item["id"]],
        "recommendation": strength,
        "reason_level": reason,
        "explicit_rank": rank,
        "start": 0,
        "end": len(response["text"]),
        "quote": response["text"],
    }


def segments(responses):
    return [
        (
            SimpleNamespace(status="completed"),
            SimpleNamespace(
                owner_kind="answer",
                owner_id=response["id"],
                start_offset=0,
                end_offset=len(response["text"]),
                text=response["text"],
            ),
            SimpleNamespace(result={"brand_signals": [], "rejected_spans": 0}),
        )
        for response in responses
    ]


def test_v4_weights_scored_platforms_and_comparison_uses_only_pairs():
    samples = [
        plan("s1", "qwen", 1),
        plan("s2", "qwen", 2, prompt="仓库工具价格和预算如何评估？"),
        plan("s3", "deepseek", 1),
        plan("s4", "kimi", 1),
        plan("s5", "qwen", 3, prompt="对比自有牌和其他产品"),
    ]
    cohort = freeze_cohort(samples, [{"name": "自有牌"}])
    scope = freeze_visibility_scope(samples, cohort)
    assert scope["cells"][-1]["scope"] == "directed_question"

    a1 = answer(samples[0], "推荐锐川，两个仓库之间可以自动同步库存。")
    a2 = answer(samples[1], "明确推荐自有牌，具备常见盘点能力。")
    a3 = answer(samples[2], "优先推荐自有牌，它支持跨仓库同步。锐川的报价待核实。")
    responses = [a1, a2, a3]
    m1, m2, m3 = (
        mention(a1, "other", "锐川"),
        mention(a2, "own", "自有牌"),
        mention(a3, "own", "自有牌"),
    )
    entities = [
        {
            "id": "own",
            "name": "自有牌",
            "kind": "brand",
            "identity_status": "provisional",
        },
        {
            "id": "other",
            "name": "锐川",
            "kind": "brand",
            "identity_status": "provisional",
            "comparison_context_status": "eligible",
            "answer_context_roles": {a1["id"]: ["evaluated_solution"]},
        },
        {
            "id": "source-author",
            "name": "文章品牌",
            "kind": "brand",
            "source_mentions": 2,
            "identity_status": "provisional",
            "comparison_context_status": "not_solution",
        },
    ]
    summary = {
        "entities": entities,
        "mentions": [m1, m2, m3],
        "brand_presence": {
            "targets": [
                {
                    "target_id": "registry:own",
                    "registry_brand_id": "own",
                    "name": "自有牌",
                    "role": "own",
                    "names": ["自有牌"],
                    "identity_status": "pending",
                    "candidate_entity_ids": ["own"],
                    "reason_codes": ["registered_binding_required"],
                },
                {
                    "target_id": "registry:competitor",
                    "registry_brand_id": "competitor",
                    "name": "登记竞品",
                    "role": "competitor",
                    "names": ["登记竞品"],
                    "identity_status": "confirmed",
                },
                {
                    "target_id": "entity:other",
                    "entity_id": "other",
                    "name": "锐川",
                    "role": "unregistered",
                    "names": ["锐川"],
                    "identity_status": "provisional",
                },
            ]
        },
        "brand_signals": {
            "observations": [
                signal(a1, m1, 4, "need_matched"),
                signal(a2, m2, 3, "generic"),
                signal(a3, m3, 4, "need_matched"),
            ]
        },
        "claim_evidence": [
            {
                "response_id": a1["id"],
                "snapshot_id": "frozen-source",
                "citation_id": "citation-1",
                "relation": "supports",
                "answer_quote": "两个仓库之间可以自动同步库存",
                "source_quote": "系统会同步两个仓库的库存数量。",
            }
        ],
    }
    payload = build_visibility_score(
        {
            "responses": responses,
            "visibility_score_scope": scope,
            "sources": [
                {"id": "citation-1", "ordinal": 2, "snapshot_id": "frozen-source"}
            ],
        },
        segments(responses),
        summary,
    )
    opportunity = {
        "id": "inventory-evidence-action",
        "own_target_id": "registry:own",
        "subtargets": [
            {
                "platform": "qwen",
                "response_id": a1["id"],
                "comparison_target_id": "entity:other",
                "scenario_id": "selection",
            }
        ],
        "task_pack": {
            "diagnostic_evidence": [
                {
                    "owner_kind": "answer",
                    "owner_id": a1["id"],
                    "quote": "两个仓库之间可以自动同步库存",
                }
            ]
        },
    }
    result = score_projection(payload, opportunities=[opportunity])
    assert result["rule_version"] == "visibility-score-v4-draft-1"
    assert result["score"]["value"] == 55
    assert result["score"]["coverage"]["scored"] == 3
    assert result["score"]["coverage"]["planned"] == 4
    assert result["score"]["coverage"]["scored_platforms"] == 2
    assert result["score"]["coverage"]["planned_platforms"] == 3
    assert result["score"]["coverage"]["missing_reasons"] == {
        "directed_question": 1,
        "capture_missing": 1,
    }
    platforms = {
        row["platform"]: row for row in result["score"]["coverage"]["platforms"]
    }
    assert platforms["qwen"]["value"] == 30
    assert platforms["deepseek"]["value"] == 80
    assert platforms["kimi"]["value"] is None

    paired = result["comparison"]
    assert paired["target_id"] == "entity:other"
    assert paired["sample_ids"] == ["s1", "s2"]
    assert paired["own"]["value"] == 30
    assert paired["other"]["value"] == 43
    assert paired["own_only_count"] == 1
    assert paired["comparison_only_count"] == 0
    assert {row["platform"] for row in result["recommendations"]} == {"qwen"}
    recommendation = result["recommendations"][0]
    assert recommendation["sample_id"] == "s1"
    assert recommendation["evidence"][0]["quote"] == a1["text"]
    assert recommendation["source_basis"] == "linked_source"
    assert recommendation["source_support"][0] == {
        "citation_id": "citation-1",
        "snapshot_id": "frozen-source",
        "ordinal": 2,
        "source_quote": "系统会同步两个仓库的库存数量。",
        "answer_quote": "两个仓库之间可以自动同步库存",
    }
    assert recommendation["action_opportunity"] == {
        "id": "inventory-evidence-action",
        "scenario": "selection",
    }
    changed = {
        **opportunity,
        "subtargets": [{**opportunity["subtargets"][0], "platform": "deepseek"}],
    }
    wrong_platform = score_projection(payload, opportunities=[changed])[
        "recommendations"
    ]
    assert all("action_opportunity" not in item for item in wrong_platform)
    assert recommendation["retest"]["prompt_id"] == "1"

    filtered = score_projection(
        payload, comparison_target_id="registry:competitor", platform="deepseek"
    )
    assert filtered["score"] == result["score"]
    assert filtered["scope_score"]["value"] == 80
    assert filtered["comparison"]["common_sample_count"] == 1
    assert filtered["comparison"]["other"]["value"] == 0
    source_only = score_projection(payload, comparison_target_id="entity:source-author")
    assert source_only["comparison"]["no_score_reason"] == "no_common_verified_samples"
    assert (
        next(
            row
            for row in result["targets"]
            if row["target_id"] == "entity:source-author"
        )["identity_method"]
        == "source_only"
    )


def test_partial_answer_and_identity_uncertainty_do_not_turn_into_zero():
    samples = [plan("partial", "qwen", 1), plan("complete", "qwen", 2)]
    scope = freeze_visibility_scope(samples, freeze_cohort(samples, []))
    responses = [
        answer(samples[0], "只采集到开头", "partial"),
        answer(samples[1], "自有牌提供服务。"),
    ]
    summary = {
        "entities": [],
        "mentions": [],
        "brand_presence": {
            "targets": [
                {
                    "target_id": "registry:own",
                    "registry_brand_id": "own",
                    "name": "自有牌",
                    "role": "own",
                    "names": ["自有牌"],
                    "identity_status": "confirmed",
                }
            ]
        },
        "brand_signals": {"observations": []},
        "claim_evidence": [],
    }
    payload = build_visibility_score(
        {"responses": responses, "visibility_score_scope": scope},
        segments(responses),
        summary,
    )
    score = score_projection(payload)["score"]
    assert score["value"] is None
    assert score["coverage"]["planned"] == 2
    assert score["coverage"]["missing_reasons"] == {
        "identity_unresolved": 1,
        "answer_incomplete": 1,
    }
    only_zero = score_rows(
        scope["cells"],
        {
            "complete": {
                "dimensions": dict.fromkeys(
                    ("presence", "recommendation", "position", "reasons"), 0
                )
            },
        },
    )
    assert only_zero["coverage"]["scored"] == 1
    assert only_zero["value"] == 0


def test_v4_explicit_tie_and_missing_rank_proof():
    text = "推荐顺序：\n1. 甲牌和乙牌并列\n3. 丙牌"
    signals = [
        {
            "entity_id": key,
            "observed_name": name,
            "explicit_rank": rank,
            "recommendation": 3,
            "start": text.index(name),
            "end": text.index(name) + len(name),
        }
        for key, name, rank in [("a", "甲牌", 1), ("b", "乙牌", 1), ("c", "丙牌", 3)]
    ]
    ranked = verified_ranking(text, signals, allow_ties=True)
    assert {entity_id: row["rank"] for entity_id, row in ranked.items()} == {
        "a": 1,
        "b": 1,
        "c": 3,
    }
    assert verified_ranking(text, signals) == {}
    assert verified_ranking(text, [signals[0], signals[2]], allow_ties=True) == {}


@pytest.mark.parametrize(
    ("heading", "score", "reason"),
    [
        ("两个方案适合不同的团队。", 70, None),
        ("两个方案不分先后。", 70, None),
        ("推荐顺序：", None, "position_unverified"),
    ],
)
def test_v4_keeps_recommendations_from_ordinary_numbered_lists(heading, score, reason):
    cell = plan("source-list", "qwen", 1)
    scope = freeze_visibility_scope([cell], freeze_cohort([cell], []))
    captured = answer(
        cell, f"{heading}\n1. 示例工具，适合没有专职分析师的团队。\n2. 另一款工具。"
    )
    matched = mention(captured, "other", "示例工具")
    summary = {
        "entities": [
            {
                "id": "other",
                "name": "示例工具",
                "kind": "brand",
                "identity_status": "provisional",
                "comparison_context_status": "eligible",
                "answer_context_roles": {captured["id"]: ["evaluated_solution"]},
            },
            {
                "id": "source-other",
                "name": "示例工具",
                "kind": "brand",
                "identity_status": "provisional",
                "source_mentions": 1,
            },
        ],
        "mentions": [matched],
        "brand_presence": {
            "targets": [
                {
                    "target_id": "registry:own",
                    "registry_brand_id": "own",
                    "name": "自有牌",
                    "role": "own",
                    "names": ["自有牌"],
                    "identity_status": "confirmed",
                },
                {
                    "target_id": "entity:other",
                    "entity_id": "other",
                    "name": "示例工具",
                    "role": "unregistered",
                    "names": ["示例工具"],
                    "identity_status": "provisional",
                },
            ]
        },
        "brand_signals": {
            "observations": [signal(captured, matched, 3, "need_matched", rank=1)],
        },
        "claim_evidence": [],
    }
    result = build_visibility_score(
        {"responses": [captured], "visibility_score_scope": scope},
        segments([captured]),
        summary,
    )
    observation = result["observations"]["entity:other"][cell["sample_id"]]
    projection = score_projection(result, comparison_target_id="entity:other")
    assert result["observations"]["entity:source-other"][cell["sample_id"]][
        "reason"
    ] == ("brand_observed_only_in_sources")
    assert observation.get("reason") == reason
    if reason:
        assert projection["comparison"]["common_sample_count"] == 0
        return
    assert observation["position_status"] == "unranked"
    assert projection["comparison"]["other"]["value"] == score
    assert {row["dimension"] for row in projection["recommendations"]} == {
        "presence",
        "recommendation",
        "reasons",
    }


def test_action_link_requires_matching_brands_scope_and_answer_quote():
    suggestion = {
        "platform": "qwen",
        "response_id": "response-1",
        "evidence": [{"quote": "这个品牌可以跨门店同步库存。"}],
    }
    base = {
        "own_target_id": "registry:own",
        "id": "opt-1",
        "subtargets": [
            {
                "platform": "qwen",
                "response_id": "response-1",
                "comparison_target_id": "entity:other",
                "scenario_id": "unreviewed",
            }
        ],
        "task_pack": {
            "diagnostic_evidence": [
                {
                    "owner_kind": "answer",
                    "owner_id": "response-1",
                    "quote": "跨门店同步库存",
                }
            ]
        },
    }
    assert link_action_opportunities(
        [suggestion.copy()], [base], "registry:own", "entity:another"
    ) == [suggestion]
    assert link_action_opportunities(
        [suggestion.copy()], [base], "registry:own", "entity:other"
    )[0]["action_opportunity"] == {"id": "opt-1", "scenario": "unreviewed"}
