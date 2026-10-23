"""Same-cohort, evidence-linked differences; never causal or market-share claims."""

from collections import defaultdict
from copy import deepcopy
from fractions import Fraction

from app.intelligence.brand_evaluation import WEIGHTS, aggregate_scores
from app.intelligence.semantics import stable_hash

VERSION = "competitive-comparison-v1"
MIN_GAP = 10
CATEGORY_LABELS = {
    "recommend": "推荐选型",
    "compare": "对比",
    "evaluate": "评价",
    "scenario": "使用场景",
    "problem_solution": "问题解决",
    "alternative_finding": "替代方案",
    "decision_help": "决策辅助",
    "regret_avoidance": "风险规避",
    "performance_specs": "性能参数",
}
PLATFORM_LABELS = {
    "qwen": "千问",
    "deepseek": "DeepSeek",
    "doubao": "豆包",
    "kimi": "Kimi",
    "hunyuan": "腾讯元宝",
}
LABELS = {
    "presence": "品牌出现",
    "recommendation": "推荐强度",
    "position": "推荐位置",
    "reasons": "选择理由",
}
TASKS = {
    "presence": (
        "补充目标需求的品牌适用说明",
        "逐条回答当前问题，说明适用对象、适用条件及可核验案例；先核对是否已有对应内容。",
    ),
    "recommendation": (
        "补充选型决策依据",
        "核对竞品被推荐的原文理由，整理我方可证实的适用条件、差异与限制，补入选型内容。",
    ),
    "position": (
        "复核优先推荐的决策条件",
        "对照完整推荐列表，检查我方适用条件和对比证据是否表达清楚；不得把列表顺序直接当作检索原因。",
    ),
    "reasons": (
        "补充具体选择理由及证据",
        "针对当前需求补充参数、案例或实施结果，每项优势标注依据和适用边界，避免泛化宣传。",
    ),
}


def scoped_cohort(cohort, axis, value):
    cells = [
        deepcopy(cell)
        for cell in cohort["cells"]
        if cell["weight"]["numerator"]
        and (
            axis == "overall"
            or axis == "platform"
            and cell["platform"] == value
            or axis == "category"
            and cell["category"] == value
            or axis == "platform_category"
            and (cell["platform"], cell["category"]) == value
        )
    ]
    total = sum((Fraction(**cell["weight"]) for cell in cells), Fraction())
    for cell in cells:
        weight = Fraction(**cell["weight"]) / total
        cell["weight"] = {
            "numerator": weight.numerator,
            "denominator": weight.denominator,
        }
    # Conditioning on a named scope is explicit. Missing observations inside it
    # still retain their weights; a platform score never fills a missing overall score.
    return {
        **cohort,
        "cells": cells,
        "hash": stable_hash([cohort["hash"], axis, value, cells]),
    }


def competitive_breakdown(cohort, rows, entities):
    empty = {
        "version": VERSION,
        "scope_scores": [],
        "gaps": [],
        "candidate_gap_count": 0,
        "minimum_gap_points": MIN_GAP,
        "interpretation": "single_audit_observation",
        "ordering_basis": "observed_gap_not_business_priority",
        "threshold_note": "10分为诊断筛选阈值，不代表统计显著性；单次审计不证明长期差距。",
    }
    if not cohort:
        return empty
    brands = {
        entity["id"]: entity
        for entity in entities
        if entity["kind"] in ("brand", "organization", "product")
    }
    index = defaultdict(list)
    for row in rows:
        if row.get("sample_id"):
            index[(row["entity_id"], row["sample_id"])].append(row)
    scopes = [
        (axis, value)
        for axis in ("platform", "category")
        for value in sorted(
            {cell[axis] for cell in cohort["cells"] if cell["weight"]["numerator"]}
        )
    ]
    scopes.insert(0, ("overall", "all"))
    scopes += [
        ("platform_category", value)
        for value in sorted(
            {
                (cell["platform"], cell["category"])
                for cell in cohort["cells"]
                if cell["weight"]["numerator"]
            }
        )
    ]
    scores, gaps = [], []
    for axis, value in scopes:
        subset = scoped_cohort(cohort, axis, value)
        samples = {cell["sample_id"] for cell in subset["cells"]}
        evaluated = {}
        for entity_id, entity in brands.items():
            observations = {
                sample: index[(entity_id, sample)][0]
                for sample in samples
                if len(index[(entity_id, sample)]) == 1
            }
            dimensions = {
                dimension: aggregate_scores(
                    subset,
                    {
                        sample: row["dimensions"].get(dimension)
                        for sample, row in observations.items()
                    },
                )
                for dimension in WEIGHTS
            }
            item = {
                "entity_id": entity_id,
                "axis": axis,
                "scope": list(value) if isinstance(value, tuple) else value,
                "total": aggregate_scores(
                    subset,
                    {sample: row["score"] for sample, row in observations.items()},
                ),
                "dimensions": dimensions,
                "sample_count": len(samples),
            }
            scores.append(item)
            evaluated[entity_id] = (entity, item, observations)
        # Generate tasks only at the most precise platform/category intersection.
        if axis != "platform_category":
            continue
        for own_id, (own, own_score, own_rows) in evaluated.items():
            if (
                own.get("role") != "own"
                or own.get("identity_status") != "confirmed"
                or own.get("role_conflict")
            ):
                continue
            for other_id, (other, other_score, other_rows) in evaluated.items():
                if (
                    other_id == own_id
                    or other.get("role") == "own"
                    or other.get("identity_status") != "confirmed"
                    or other.get("role_conflict")
                ):
                    continue
                for dimension in WEIGHTS:
                    ours = own_score["dimensions"][dimension]["score"]
                    theirs = other_score["dimensions"][dimension]["score"]
                    if ours is None or theirs is None or theirs - ours < MIN_GAP:
                        continue
                    # Include only answer pairs that exhibit this difference.
                    supporting = [
                        sample
                        for sample in sorted(samples)
                        if other_rows[sample]["dimensions"][dimension]
                        > own_rows[sample]["dimensions"][dimension]
                    ]
                    evidence_ids = sorted(
                        {
                            evidence
                            for sample in supporting
                            for row in (own_rows[sample], other_rows[sample])
                            for evidence in row["evidence_ids"]
                        }
                    )
                    if not evidence_ids:
                        continue
                    title, task = TASKS[dimension]
                    prompts = sorted(
                        {
                            cell["prompt"]
                            for cell in subset["cells"]
                            if cell["sample_id"] in supporting
                        }
                    )
                    gaps.append(
                        {
                            "id": stable_hash(
                                [VERSION, subset["hash"], own_id, other_id, dimension]
                            ),
                            "own_entity_id": own_id,
                            "comparison_entity_id": other_id,
                            "platform": value[0],
                            "category": value[1],
                            "dimension": dimension,
                            "own_score": ours,
                            "comparison_score": theirs,
                            "gap_points": theirs - ours,
                            "cohort_hash": subset["hash"],
                            "sample_count": len(samples),
                            "supporting_sample_ids": supporting,
                            "prompts": prompts,
                            "response_ids": sorted(
                                {
                                    row["response_id"]
                                    for sample in supporting
                                    for row in (own_rows[sample], other_rows[sample])
                                }
                            ),
                            "evidence_ids": evidence_ids,
                            "finding": f"在本次{PLATFORM_LABELS.get(value[0], value[0])}的{CATEGORY_LABELS.get(value[1], '其他需求')}类问题中，{other.get('name', other_id)}的{LABELS[dimension]}分高于{own.get('name', own_id)} {theirs - ours} 分。",
                            "cause_status": "not_established",
                            "review_state": "needs_review",
                            "action": {
                                "status": "proposed",
                                "title": title,
                                "task": task,
                                "target_prompts": prompts,
                                "owner_role": "品牌内容团队",
                                "materials_needed": [
                                    "我方已发布的相关内容",
                                    "可核验的产品资料或客户案例",
                                    "本条差距关联的原始回答",
                                ],
                                "acceptance": "逐条回应目标问题；每项产品主张附可核验依据及适用条件；复核关联竞品原文，禁止照搬未经确认的主张。",
                                "retest": {
                                    "platform": value[0],
                                    "prompt_hashes": sorted(
                                        {
                                            cell["prompt_hash"]
                                            for cell in subset["cells"]
                                            if cell["sample_id"] in supporting
                                        }
                                    ),
                                    "dimension": dimension,
                                    "success_check": "在独立后续审计中比较同问题、同平台、同规则的差距变化，不把单次改善解释为因果。",
                                },
                            },
                        }
                    )
    gaps.sort(key=lambda gap: (-gap["gap_points"], gap["id"]))
    return {
        **empty,
        "scope_scores": scores,
        "gaps": gaps[:50],
        "candidate_gap_count": len(gaps),
    }
