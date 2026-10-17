"""Descriptive, evidence-bound platform tasks; no inferred retrieval causality."""

from collections import Counter
from urllib.parse import urlsplit

from app.intelligence.semantics import stable_hash

DIAGNOSIS_VERSION = "platform-evidence-rules-1"


def platform_diagnoses(run, summary):
    sources = run.manifest["sources"]
    groups = {}
    for response in run.manifest["responses"]:
        if response.get("error"):
            continue
        group_id = stable_hash(
            [
                response["prompt_hash"],
                response["platform"],
                response.get("conditions", {}),
                response.get("surface"),
            ]
        )
        groups.setdefault(group_id, []).append(response)
    result = []
    entities = {entity["id"]: entity for entity in summary["entities"]}
    for group_id, responses in groups.items():
        response_ids = {response["id"] for response in responses}
        platform = responses[0]["platform"]
        group_sources = [
            source
            for source in sources
            if source["response_id"] in response_ids and source["platform"] == platform
        ]
        citations = {source["id"]: source for source in group_sources}
        mentions = [
            mention
            for mention in summary["mentions"]
            if mention["owner_kind"] == "answer" and mention["owner_id"] in response_ids
        ]

        def add(
            kind,
            finding,
            task,
            *,
            evidence_ids=None,
            material=None,
            group_id=group_id,
            platform=platform,
            responses=responses,
            response_ids=response_ids,
            citations=citations,
        ):
            result.append(
                {
                    "id": stable_hash(
                        [run.id, group_id, kind, evidence_ids or [], DIAGNOSIS_VERSION]
                    ),
                    "group_id": group_id,
                    "platform": platform,
                    "prompt_hash": responses[0]["prompt_hash"],
                    "prompt": responses[0]["prompt"],
                    "conditions": responses[0].get("conditions", {}),
                    "sample_count": len(response_ids),
                    "kind": kind,
                    "finding": finding,
                    "response_ids": sorted(response_ids),
                    "source_ids": sorted(citations),
                    "evidence_ids": evidence_ids or [],
                    "method": DIAGNOSIS_VERSION,
                    "review_state": "needs_review",
                    "action": {
                        "status": "proposed",
                        "task": task,
                        "materials_needed": material or [],
                        "platform": platform,
                        "success_check": "以同提示词版本和实际条件重新独立采样，复核证据变化；不保证可见性提升。",
                    },
                }
            )

        incomplete = [
            response
            for response in responses
            if response.get("answer_status") != "complete"
        ]
        if incomplete or any(
            source.get("fetch_status") != "captured" for source in group_sources
        ):
            add(
                "capture_gap",
                "本组存在不完整回答或未取得正文的来源，不能把未提及推定为品牌缺席。",
                "补齐回答与来源正文后再复核本组结论。",
            )
        unresolved = [
            mention
            for mention in mentions
            if entities[mention["entity_id"]]["identity_status"] == "provisional"
            or mention.get("identity_ambiguity")
        ]
        if unresolved:
            names = sorted({mention["entity_name"] for mention in unresolved})
            add(
                "identity_review",
                f"本组观察到 {len(names)} 个身份或片段归属未确认实体，包含：{'、'.join(names[:5])}。",
                "逐条确认实体身份及同名歧义，再决定是否登记为对比品牌。",
                evidence_ids=[mention["id"] for mention in unresolved],
                material=["企业/产品正式名称及原文上下文"],
            )
        negative = [
            mention
            for mention in mentions
            if mention["sentiment"] == "negative"
            and not mention.get("identity_ambiguity")
            and entities[mention["entity_id"]]["identity_status"] != "excluded"
        ]
        if negative:
            add(
                "negative_observation",
                "本组存在针对具体实体的负面表述，需核实原文对象及事实限定。",
                "核对负面表述的适用时间、产品和范围，整理可公开验证的事实材料。",
                evidence_ids=[mention["id"] for mention in negative],
                material=["已确认的产品规格、服务政策或公开案例"],
            )
        conflicts = [
            item
            for item in summary["claim_evidence"]
            if item["citation_id"] in citations
            and item["platform"] == platform
            and item["relation"] in ("contradicts", "partial")
        ]
        if conflicts:
            add(
                "claim_review",
                "本组回答与其实际引用原文存在模型判定的冲突或仅部分支持，尚待人工核实。",
                "对照观点与引用段落，核实数值、时间和限定条件；确认后制定内容修订任务。",
                evidence_ids=[item["id"] for item in conflicts],
                material=["观点与引用原文逐字片段", "可批准的事实依据"],
            )
        domains = Counter(
            urlsplit(source["url"]).hostname
            for source in group_sources
            if source["kind"] == "answer_reference"
            and source.get("url")
            and source.get("url_resolution") == "exact"
        )
        if sum(domains.values()) >= 2:
            domain, count = domains.most_common(1)[0]
            if count / sum(domains.values()) >= 0.75:
                add(
                    "source_concentration",
                    f"本组实际引用观测中 {count}/{sum(domains.values())} 来自 {domain}；这不代表平台普遍偏好该站点。",
                    "检查这些具体文章的主题、事实与内容结构，形成该平台的内容实验假设。",
                    material=["被引用文章清单与正文", "待验证的内容实验和复测口径"],
                )
    return result
