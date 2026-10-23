"""Bridge scored differences to existing evidence review and action approvals."""

from app.intelligence.semantics import stable_hash


def competitive_diagnoses(run, summary):
    comparison = summary.get("brand_evaluation", {}).get("comparison", {})
    signals = {
        item["id"]: item
        for item in summary.get("brand_signals", {}).get("observations", [])
    }
    mentions = {item["id"]: item for item in summary.get("mentions", [])}
    result = {}
    for gap in comparison.get("gaps", []):
        bound = [signals[item] for item in gap["evidence_ids"] if item in signals]
        evidence_ids = sorted(
            {
                item
                for signal in bound
                for item in signal["mention_ids"]
                if item in mentions
            }
        )
        if not evidence_ids:
            continue
        action = gap["action"]
        diagnosis = {
            **gap,
            "id": stable_hash([run.id, gap["id"], "competitive-action-v1"]),
            "kind": "competitive_" + gap["dimension"],
            "method": "competitive-action-v1",
            "prompt": "；".join(gap["prompts"]),
            "signal_evidence_ids": gap["evidence_ids"],
            "evidence_ids": evidence_ids,
            "source_ids": [],
            "action": {**action, "success_check": action["retest"]["success_check"]},
        }
        result[diagnosis["id"]] = diagnosis
        # Only positive observed source evidence. We do not interpret missing
        # articles or mentions as own-brand absence, or citation as causality.
        for source in run.manifest["sources"]:
            if (
                source["response_id"] not in gap["response_ids"]
                or source["platform"] != gap["platform"]
                or source["kind"] != "answer_reference"
                or not source.get("snapshot_id")
                or source.get("fetch_status") != "captured"
            ):
                continue
            source_mentions = [
                item
                for item in mentions.values()
                if item["owner_kind"] == "snapshot"
                and item["owner_id"] == source["snapshot_id"]
                and item["entity_id"] == gap["comparison_entity_id"]
                and not item.get("identity_ambiguity")
            ]
            if not source_mentions:
                continue
            key = stable_hash(
                [
                    run.id,
                    gap["own_entity_id"],
                    gap["comparison_entity_id"],
                    gap["platform"],
                    source["snapshot_id"],
                    "competitor-source-v1",
                ]
            )
            source_diagnosis = result.setdefault(
                key,
                {
                    "id": key,
                    "kind": "competitor_source_evidence",
                    "method": "competitor-source-v1",
                    "platform": gap["platform"],
                    "own_entity_id": gap["own_entity_id"],
                    "comparison_entity_id": gap["comparison_entity_id"],
                    "finding": "本次回答实际引用的已保存文章中出现了对比品牌，可检查其主题和证据表达；尚不能据此判断推荐差距的原因。",
                    "response_ids": [],
                    "source_ids": [],
                    "evidence_ids": sorted(item["id"] for item in source_mentions),
                    "snapshot_id": source["snapshot_id"],
                    "review_state": "needs_review",
                    "cause_status": "not_established",
                    "action": {
                        "status": "proposed",
                        "task": "核对该引用文章的品牌观点、适用条件和事实依据，评估我方可补充的原创案例或选型材料。",
                        "materials_needed": [
                            "已保存的引用文章与品牌原文片段",
                            "我方可核验的资料与案例",
                        ],
                        "success_check": "形成附原文依据的内容实验方案，再在同平台同问题的独立审计中验证；不保证被引用。",
                    },
                },
            )
            source_diagnosis["source_ids"] = sorted(
                set(source_diagnosis["source_ids"]) | {source["id"]}
            )
            source_diagnosis["response_ids"] = sorted(
                set(source_diagnosis["response_ids"]) | {source["response_id"]}
            )
    return sorted(result.values(), key=lambda item: item["id"])
