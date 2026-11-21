from hashlib import sha256
from types import SimpleNamespace

from app.intelligence.comparison_workspace import project_comparison
from app.intelligence.visibility_scoring import RULE_VERSION, WEIGHTS, score_projection


def compared_run():
    prompt = "连锁门店如何选择库存系统？"
    prompt_hash = sha256(prompt.encode()).hexdigest()
    claim = "锐川支持跨店调拨"
    text = "优先推荐" + claim + "。云衡可了解。\n参考来源\n《只在来源中的品牌》"
    version = "c" * 32
    base = {
        "prompt": prompt,
        "prompt_version_id": version,
        "scenario": "implementation",
        "scope": "eligible",
    }
    cells = [
        {**base, "sample_id": "s1", "platform": "qwen"},
        {**base, "sample_id": "s2", "platform": "deepseek"},
        {**base, "sample_id": "s3", "platform": "kimi"},
        {
            **base,
            "sample_id": "s4",
            "platform": "doubao",
            "prompt_version_id": "d" * 32,
        },
    ]
    responses = [
        {
            "id": "r1",
            "sample_id": "s1",
            "prompt": prompt,
            "prompt_hash": prompt_hash,
            "platform": "qwen",
            "answer_status": "complete",
            "text": text,
            "conditions": {"searchMode": "enabled", "surface": "must-not-leak"},
        },
        {
            "id": "r3",
            "sample_id": "s3",
            "prompt": prompt,
            "prompt_hash": prompt_hash,
            "platform": "kimi",
            "answer_status": "partial",
            "text": "只有部分回答",
        },
    ]
    entities = [
        {
            "id": "rival",
            "name": "锐川",
            "kind": "brand",
            "identity_status": "provisional",
        },
        {"id": "own", "name": "云衡", "kind": "brand", "identity_status": "confirmed"},
        {"id": "source", "name": "只在来源中的品牌", "kind": "brand"},
    ]
    mentions = []
    for entity in entities:
        start = text.index(entity["name"])
        mentions.append(
            {
                "id": entity["id"],
                "entity_id": entity["id"],
                "entity_name": entity["name"],
                "owner_id": "r1",
                "owner_kind": "answer",
                "quote": entity["name"],
                "start": start,
                "end": start + len(entity["name"]),
            }
        )
    source = {
        "id": "c1",
        "response_id": "r1",
        "kind": "answer_reference",
        "snapshot_id": "snap",
        "title": "锐川使用手册",
        "url": "https://manual.example.com/stock",
        "ordinal": 1,
    }
    evidence = {
        "id": "evidence",
        "citation_id": "c1",
        "response_id": "r1",
        "snapshot_id": "snap",
        "answer_quote": claim,
        "source_quote": "跨店调拨支持门店间库存移动",
        "relation": "supports",
        "verification": {
            "answer_span": {"start": 4, "end": 4 + len(claim), "quote": claim}
        },
    }
    targets = [
        {
            "target_id": "registry:own",
            "registry_brand_id": "own",
            "entity_id": "own",
            "name": "云衡",
            "role": "own",
            "identity_status": "confirmed",
            "identity_method": "confirmed",
        },
        {
            "target_id": "entity:rival",
            "entity_id": "rival",
            "name": "锐川",
            "role": "unregistered",
            "identity_method": "observed_name",
            "identity_status": "provisional",
        },
    ]
    scoring = {
        "rule_version": RULE_VERSION,
        "weights": WEIGHTS,
        "cells": cells,
        "targets": targets,
        "scope_hash": "frozen",
        "source_coverage": {},
        "observations": {
            "registry:own": {
                "s1": {"response_id": "r1", "dimensions": dict.fromkeys(WEIGHTS, 25)},
                "s3": {"response_id": "r3", "dimensions": dict.fromkeys(WEIGHTS, 50)},
            },
            "entity:rival": {
                "s1": {"response_id": "r1", "dimensions": dict.fromkeys(WEIGHTS, 80)}
            },
        },
    }
    return SimpleNamespace(
        id="run",
        audit_id=1,
        revision=2,
        status="ready",
        manifest={
            "responses": responses,
            "visibility_score_scope": {"cells": cells},
            "sample_statuses": {"s2": "failed"},
            "sources": [
                source,
                {**source, "id": "card", "snapshot_id": None},
                {**source, "id": "search", "kind": "search_candidate"},
            ],
        },
        summary={
            "entities": entities,
            "mentions": mentions,
            "visibility_score": scoring,
            "brand_signals": {
                "observations": [
                    {
                        "id": "signal",
                        "response_id": "r1",
                        "entity_id": "rival",
                        "mention_ids": ["rival"],
                        "observed_name": "锐川",
                        "start": 0,
                        "end": 4 + len(claim),
                        "quote": "优先推荐" + claim,
                        "recommendation": 4,
                        "reason_level": "specific",
                    },
                ]
            },
            "claim_evidence": [
                evidence,
                {**evidence, "id": "wrong-answer", "response_id": "r3"},
            ],
            "source_observations": {
                "observations": [
                    {
                        "id": "c1",
                        "frozen_version_valid": True,
                        "content_family": {"id": "family"},
                        "content_coverage": {"stored_text_integrity": "verified"},
                    }
                ]
            },
        },
    )


def test_question_uses_frozen_version_and_anchors_sources_to_its_answer():
    run = compared_run()
    report = project_comparison(run, mode="platforms", question_id="c" * 32)
    assert [row["id"] for row in report["questions"]] == ["c" * 32, "d" * 32]
    qwen, deepseek, kimi = report["detail"]["samples"]
    assert (qwen["status"], deepseek["status"], kimi["status"]) == (
        "received",
        "failed",
        "partial",
    )
    assert {row["name"] for row in qwen["findings"]} == {"锐川", "云衡"}
    recommended = qwen["findings"][0]
    assert recommended["recommendation"] == "priority"
    assert recommended["rank"] is None
    assert [row["id"] for row in recommended["reasons"][0]["evidence"]] == ["evidence"]
    archived, card = qwen["sources"]
    assert archived["frozen"] and archived["content_type"] == "documentation"
    assert card["evidence"] == [] and not card["frozen"]
    assert qwen["search_mode"] == "enabled" and "surface" not in qwen
    assert (
        project_comparison(run, mode="platforms", question_id="f" * 32)["detail"]
        is None
    )
    assert (
        project_comparison(run, mode="platforms", question_id="d" * 32)["detail"][
            "samples"
        ][0]["findings"]
        == []
    )


def test_brand_scores_match_audit_rule_with_partial_and_common_samples():
    run = compared_run()
    brands = project_comparison(
        run, mode="brands", own="registry:own", other="entity:rival"
    )["brands"]
    audit = score_projection(
        run.summary["visibility_score"],
        own_target_id="registry:own",
        comparison_target_id="entity:rival",
    )
    assert brands["own"] == audit["score"]
    assert brands["pair"]["count"] == 1
    assert brands["pair"]["own"] == audit["comparison"]["own"]
    assert brands["pair"]["other"] == audit["comparison"]["other"]
    assert brands["coverage"] == {"planned": 4, "received": 2}
    assert brands["gaps"][0]["question_id"] == "c" * 32
    assert brands["gaps"][0]["gap"] == 55
    assert {row["name"] for row in brands["brands"]} == {"云衡", "锐川"}


def test_reference_marker_can_support_a_following_claim_for_the_same_brand():
    run = compared_run()
    response = run.manifest["responses"][0]
    header, claim = "优先推荐锐川", "实施时支持跨店调拨"
    text = header + "\n" + claim + "。云衡可了解。"
    response["text"] = text
    for mention in run.summary["mentions"][:2]:
        mention["start"] = text.index(mention["quote"])
        mention["end"] = mention["start"] + len(mention["quote"])
    signal = run.summary["brand_signals"]["observations"][0]
    signal.update(quote=header, start=0, end=len(header), segment_id="segment")
    span = {
        "start": text.index(claim),
        "end": text.index(claim) + len(claim),
        "quote": claim,
    }
    evidence = run.summary["claim_evidence"][0]
    evidence.update(
        answer_quote=claim,
        answer_segment_id="segment",
        verification={
            "answer_span": span,
            "association": {"method": "answer_reference_marker"},
        },
    )
    frozen_claim = {
        "owner_id": "r1",
        "owner_kind": "answer",
        "segment_id": "segment",
        "subject": "锐川",
        **span,
    }
    run.summary["claims"] = [frozen_claim]
    workspace = project_comparison(run, mode="platforms")
    reasons = workspace["detail"]["samples"][0]["findings"][0]["reasons"]
    assert reasons[0]["evidence"] == []
    assert (
        reasons[1]["quote"] == claim and reasons[1]["evidence"][0]["id"] == "evidence"
    )
    frozen_claim["subject"] = "云衡"
    workspace = project_comparison(run, mode="platforms")
    reasons = workspace["detail"]["samples"][0]["findings"][0]["reasons"]
    assert len(reasons) == 1 and reasons[0]["evidence"] == []
