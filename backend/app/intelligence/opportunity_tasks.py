"""Concrete, immutable task specifications and transparent business ordering."""

from copy import deepcopy

from pydantic import ValidationError

from app.intelligence.project_context import preference_projection
from app.intelligence.semantics import stable_hash
from app.intelligence.value_contracts import TaskPackV2

VERSION = "value-opportunities-v1"
PRIORITY_VERSION = "opportunity-priority-v1"
KINDS = {
    "scenario_coverage": "场景覆盖",
    "recommendation_strength": "推荐竞争力",
    "brand_perception": "品牌认知",
    "source_channel": "来源渠道",
    "content_evidence": "内容证据",
    "platform_difference": "平台差异",
}
UNIT_LABELS = {
    "feature": "功能说明",
    "parameter": "参数表",
    "price": "费用与报价边界",
    "audience": "适用客户说明",
    "use_case": "场景适用说明",
    "implementation": "实施步骤与限制",
    "case_study": "案例与结果依据",
    "test_method": "测试方法与结果",
    "risk": "风险与限制",
    "faq": "具体问题解答",
    "comparison": "选型对照表",
    "selection_reason": "选择理由与依据",
}


UNIT_FORMATS = {
    "parameter": "参数表字段：参数名称、取值、单位、适用产品/版本、限制条件和批准事实编号。",
    "price": "报价表字段：收费项目、计费单位、包含项、额外费用、合同适用期和核价依据。",
    "case_study": "案例字段：客户类型、实施范围、时间、基线、结果及可公开证据；缺授权或结果依据时保持待核实。",
    "test_method": "测试记录字段：测试环境、样本、方法、指标、复现步骤及原始记录；不虚构测试结果。",
    "implementation": "实施说明字段：前置条件、接入步骤、职责、权限边界、验证办法和不适用情况。",
    "risk": "限制清单字段：具体限制、触发条件、受影响对象、替代办法和证据版本。",
    "comparison": "选型表字段：原问题需求、适用条件、不适用条件、我方批准事实和待核对的竞品原文。",
    "feature": "功能说明字段：用户任务、操作方式、输入输出、适用版本、限制条件及可验证实例。",
}


def label(value):
    return value if len(value) <= 120 else value[:120] + "…"


def material_readiness(manifest, own, topic):
    assets = [
        row
        for row in manifest.get("owned_asset_revisions", [])
        if row["target_id"] == own["target_id"]
    ]
    facts = [
        row
        for row in manifest.get("approved_facts", {}).values()
        if row.get("effective_status") == "valid"
        and (
            row.get("target_id") == own["target_id"]
            or own.get("entity_id")
            and row.get("entity_id") == own["entity_id"]
        )
    ]
    # Literal topic overlap is only a review candidate, never semantic entailment.
    matched = [
        row
        for row in facts
        if len(topic) >= 4 and topic in row["statement"] and topic in row["scope"]
    ]
    related_assets = [
        row
        for row in assets
        if row["status"] == "captured"
        and row.get("decision") == "verified"
        and len(topic) >= 4
        and topic
        in (row.get("title", "") + " " + (row.get("verification_scope") or ""))
    ]
    matched_ids = {row.get("owned_asset_revision_id") for row in matched}
    selected = [row for row in related_assets if row["id"] in matched_ids]
    asset = selected[0] if len(selected) == 1 else None
    return {
        "state": "approved_topic_candidates"
        if matched
        else "materials_need_fact_review"
        if assets
        else "no_registered_material",
        "assets": related_assets,
        "registered_asset_count": len(assets),
        "unmatched_asset_count": len(assets) - len(related_assets),
        "approved_fact_candidates": [
            {
                key: row.get(key)
                for key in (
                    "id",
                    "version",
                    "statement",
                    "scope",
                    "valid_from",
                    "valid_until",
                    "source_kind",
                )
            }
            for row in matched
        ],
        "unmatched_approved_fact_count": len(facts) - len(matched),
        "match_basis": "literal_topic_in_approved_statement_and_scope_not_entailment",
        "selected_asset": asset,
        "unknowns": [
            "材料未匹配不等于产品没有该能力",
            "事实是否满足当前需求及限定条件仍须审核",
        ],
    }


def make_candidate(
    run,
    own,
    response,
    scope,
    *,
    kind,
    goal,
    topic,
    finding,
    evidence,
    comparison=None,
    dimension="reasons",
    intent="content_create",
    unit_kind=None,
    counterevidence=(),
    unknowns=(),
    urgency=0,
    needs_scope_review=False,
    source_ids=(),
    observation_kind="answer",
):
    materials = material_readiness(run.manifest, own, topic)
    asset = materials["selected_asset"]
    task_intent = "content_update" if asset and intent == "content_create" else intent
    need_identity = (
        own.get("identity_status") != "confirmed"
        or comparison is not None
        and comparison.get("identity_status") != "confirmed"
    )
    readiness = (
        "needs_identity"
        if need_identity
        else "insufficient_evidence"
        if needs_scope_review
        else "ready_for_review"
        if materials["approved_fact_candidates"]
        else "needs_material"
    )
    if scope["eligibility"] == "excluded" and kind in (
        "scenario_coverage",
        "recommendation_strength",
    ):
        readiness = "not_applicable"
    specific = UNIT_LABELS.get(unit_kind, "问题适用说明与事实依据")
    topic_label = label(topic)
    missing = (
        []
        if materials["approved_fact_candidates"]
        else [
            f"针对「{topic_label}」提供我方{specific}，具名核实原文、适用范围和有效期，再批准可用事实。"
        ]
    )
    if needs_scope_review:
        missing.append(
            "审核本问题的主场景及中性/品牌定向范围，当前不作为确定的竞争弱势结论。"
        )
    if need_identity:
        missing.append(
            "核实本条涉及的品牌身份及同名片段归属，保留未登记品牌的观察身份。"
        )
    delivery = {
        "asset_title": asset.get("title")
        if asset
        else f"{own['name']} · {topic_label} · {specific}",
        "asset_kind": "existing_material" if asset else "new_deliverable",
        "material_revision_id": asset["id"] if asset else None,
        "material_version": asset["version"] if asset else None,
        "reference_url": asset.get("url") if asset else None,
        "audience": scope["audience"],
        "audience_status": "reviewed_prompt" if scope["audience"] else "unknown",
        "outline": [
            f"需求与对象：逐条回应「{topic_label}」，保留原问题中的客户、场景及限制。",
            f"主体内容：整理我方{specific}；将已批准事实、待核实事项和不适用情况分开。",
            "证据与边界：每项产品说法关联我方有效批准事实，注明材料版本、范围和时间；竞品文字只作诊断参考。",
            "交付核对：保留本次平台原文对照，确认待审材料后再决定发布与一次同题观察。",
            UNIT_FORMATS.get(
                unit_kind,
                "选型依据字段：原问题要求、我方适用条件、限制与不适用情况、批准事实及待核实事项。",
            ),
        ],
        "channel": "material_library"
        if task_intent == "material_verification"
        else "own_website",
        "channel_reason": "先形成可核对的我方资料及具体问题解答；本次平台引用只提供观察依据，不证明发布渠道偏好或收录保证。",
        "questions": [
            {"prompt_hash": response["prompt_hash"], "text": response["prompt"]}
        ],
    }
    if asset and asset.get("url"):
        delivery["channel_reason"] = (
            "核对已具名确认的这份材料版本并准备修订；URL 是参考地址，发布权限和最终发布仍须人工确认。"
        )
    asset_key = (
        "material:" + asset["asset_key"]
        if asset
        else "new:" + stable_hash([own["target_id"], goal])[:32]
    )
    fact_change_key = stable_hash(["literal-change-v1", goal])
    task_key = stable_hash(
        [run.project_id, own["target_id"], asset_key, fact_change_key, task_intent]
    )
    return {
        "kind": kind,
        "own_target_id": own["target_id"],
        "own_entity_id": own.get("entity_id"),
        "comparison_target_id": comparison["target_id"] if comparison else None,
        "comparison_entity_id": comparison.get("entity_id") if comparison else None,
        "platform": response["platform"],
        "prompt_hash": response["prompt_hash"],
        "prompt": response["prompt"],
        "response_id": response["id"],
        "conditions": response.get("conditions", {}),
        "scenario_id": scope["scenario_id"],
        "scenario_review_state": scope["review_state"],
        "finding": finding,
        "observed_facts": [finding],
        "hypotheses": [
            "改善该问题下的内容与证据表达可能影响后续回答，需要独立观察验证。"
        ],
        "unknowns": sorted(
            {*unknowns, *materials["unknowns"], "不保证被引用或可见性提升"}
        ),
        "actionability": readiness,
        "material_readiness": materials,
        "evidence": evidence,
        "counterevidence": list(counterevidence),
        "source_ids": list(source_ids),
        "observation_kind": observation_kind,
        "goal": goal,
        "task_key": task_key,
        "asset_key": asset_key,
        "fact_change_key": fact_change_key,
        "intent": task_intent,
        "deliverable": f"围绕「{topic_label}」交付{own['name']}的{specific}及逐项依据，附待核实清单与适用边界。",
        "delivery": delivery,
        "missing_materials": missing,
        "ai_target": {
            "platform": response["platform"],
            "prompt_hash": response["prompt_hash"],
            "scenario_id": scope["scenario_id"],
            "dimensions": [dimension],
        },
        "urgency": urgency,
    }


def merge_candidates(run, candidates):
    groups = {}
    for candidate in candidates:
        groups.setdefault(candidate["task_key"], []).append(candidate)
    items = []
    for task_key, rows in sorted(groups.items()):
        excluded = [row for row in rows if row["actionability"] == "not_applicable"]
        rows = [row for row in rows if row["actionability"] != "not_applicable"] or rows
        first = rows[0]
        targets, questions, refs = {}, {}, {}
        for row in rows:
            ai = row["ai_target"]
            key = (ai["platform"], ai["prompt_hash"], ai["scenario_id"])
            target = targets.setdefault(key, {**ai, "dimensions": []})
            target["dimensions"] = sorted(
                set(target["dimensions"]) | set(ai["dimensions"])
            )
            for question in row["delivery"]["questions"]:
                questions[question["prompt_hash"]] = question
            for ref in [*row["evidence"], *row["counterevidence"]]:
                refs[stable_hash(ref)] = ref
        states = {row["actionability"] for row in rows}
        state = next(
            value
            for value in (
                "not_applicable",
                "needs_identity",
                "insufficient_evidence",
                "needs_material",
                "ready_for_review",
            )
            if value in states
        )
        delivery = {
            **first["delivery"],
            "questions": [questions[key] for key in sorted(questions)],
        }
        delivery["audience"] = sorted(
            {value for row in rows for value in row["delivery"]["audience"]}
        )
        delivery["audience_status"] = (
            "reviewed_prompt" if delivery["audience"] else "unknown"
        )
        task_data = {
            "project_id": run.project_id,
            "own_target_id": first["own_target_id"],
            "asset_key": first["asset_key"],
            "fact_change_key": first["fact_change_key"],
            "intent": first["intent"],
            "deliverable": first["deliverable"],
            "delivery": delivery,
            "acceptance": [
                "逐条回应保留的原问题与目标场景，未知限制不补成已知。",
                "每项我方产品主张具有当前有效批准事实和原文依据，竞品证据不转成我方能力。",
                "完成具体交付物及人工核对，发布和同题单轮观察分别登记；不以草稿生成代替可见性改善。",
            ],
            "ai_targets": [targets[key] for key in sorted(targets)],
            "diagnostic_evidence": [refs[key] for key in sorted(refs)],
            "own_fact_ids": sorted(
                {
                    fact["id"]
                    for row in rows
                    for fact in row["material_readiness"]["approved_fact_candidates"]
                }
            ),
            "missing_materials": sorted(
                {value for row in rows for value in row["missing_materials"]}
            ),
        }
        pack, error = None, None
        try:
            pack = TaskPackV2.model_validate(task_data)
        except ValidationError:
            state, error = (
                "insufficient_evidence",
                "task_contract_capacity_or_provenance",
            )
        payload = {
            "id": stable_hash([run.id, VERSION, task_key]),
            "version": VERSION,
            "kind": first["kind"],
            "kinds": sorted({row["kind"] for row in rows}),
            "kind_labels": sorted({KINDS[row["kind"]] for row in rows}),
            "method": VERSION,
            "review_state": "needs_review",
            "publication_allowed": False,
            "task_key": task_key,
            "task_pack": pack.model_dump(mode="json") if pack else None,
            "task_signature": pack.revision_signature() if pack else None,
            "contract_error": error,
            "own_target_id": first["own_target_id"],
            "own_entity_id": first["own_entity_id"],
            "comparison_target_ids": sorted(
                {
                    row["comparison_target_id"]
                    for row in rows
                    if row["comparison_target_id"]
                }
            ),
            "platform": first["platform"],
            "platforms": sorted({row["platform"] for row in rows}),
            "prompt_hash": first["prompt_hash"],
            "prompt": first["prompt"],
            "conditions": first["conditions"],
            "response_ids": sorted({row["response_id"] for row in rows}),
            "source_ids": sorted(
                {value for row in rows for value in row["source_ids"]}
            ),
            "evidence_ids": [],
            "finding": first["finding"],
            "observed_facts": sorted({row["finding"] for row in rows}),
            "hypotheses": sorted(
                {value for row in rows for value in row["hypotheses"]}
            ),
            "unknowns": sorted({value for row in rows for value in row["unknowns"]}),
            "counterevidence": [ref for row in rows for ref in row["counterevidence"]],
            "subtargets": [
                {
                    key: row[key]
                    for key in (
                        "platform",
                        "prompt_hash",
                        "response_id",
                        "comparison_target_id",
                        "scenario_id",
                        "scenario_review_state",
                        "finding",
                        "conditions",
                        "actionability",
                        "observation_kind",
                    )
                }
                for row in rows
            ],
            "actionability": state,
            "excluded_scopes": [
                {
                    key: row[key]
                    for key in (
                        "platform",
                        "prompt_hash",
                        "response_id",
                        "scenario_id",
                        "actionability",
                        "finding",
                    )
                }
                for row in excluded
            ],
            "material_readiness": first["material_readiness"],
            "priority_inputs": {
                "correction_urgency": max(row["urgency"] for row in rows),
                "scenarios": sorted({row["scenario_id"] for row in rows}),
                "evidence_readiness": int(bool(pack))
                + int(state == "ready_for_review"),
                "target_coverage": len(targets),
                "controllability": 2
                if first["delivery"]["channel"] != "reference_publisher"
                else 0,
                "outline_steps": len(delivery["outline"]),
            },
            "action": {
                "task": first["deliverable"],
                "materials_needed": task_data["missing_materials"],
                "success_check": task_data["acceptance"][-1],
            }
            if pack and state != "not_applicable"
            else None,
        }
        items.append(payload)
    return items


def rank_opportunities(items, context):
    result = deepcopy(items)
    preferences = preference_projection(context)
    policy = PRIORITY_VERSION + ":" + preferences["priority_policy_version"]
    for row in result:
        factors = row["priority_inputs"]
        importance = max(
            (preferences["importance"].get(key, 50) for key in factors["scenarios"]),
            default=50,
        )
        key = [
            factors["correction_urgency"],
            importance,
            factors["evidence_readiness"],
            factors["target_coverage"],
            factors["controllability"],
            -factors["outline_steps"],
        ]
        row["priority"] = {
            "policy_version": policy,
            "preference_mode": preferences["mode"],
            "context_version": preferences["context_version"],
            "scenario_importance": importance,
            "ordering_tuple": key,
            "basis": [
                "事实纠错紧迫性",
                "已设置场景重要性，未设置取中性50",
                "证据和材料准备度",
                "目标覆盖数",
                "交付对象可控性",
                "提纲步骤数，非工时估计",
            ],
            "roi": None,
            "success_probability": None,
            "queue": "review"
            if row["actionability"] == "ready_for_review"
            else "preparation",
        }
    result.sort(
        key=lambda row: (
            tuple(-value for value in row["priority"]["ordering_tuple"]),
            row["task_key"],
        )
    )
    for index, row in enumerate(result, 1):
        row["priority"]["rank"] = index
    return result, {
        "version": policy,
        "preferences": preferences,
        "meaning": "rule_order_not_roi_or_success_probability",
    }
