"""Evidence-first opportunities independent of total scores or pre-registered rivals."""

from collections import defaultdict
from heapq import heappush, heapreplace
from itertools import combinations

from app.intelligence.blob import digest
from app.intelligence.opportunity_tasks import (
    UNIT_LABELS,
    VERSION,
    make_candidate,
    material_readiness,
    merge_candidates,
    rank_opportunities,
)
from app.intelligence.project_context import preference_projection
from app.intelligence.semantics import stable_hash
from app.intelligence.value_contracts import EvidenceRef

MAX_CANDIDATES = 1000
MAX_TASKS = 200


class Inputs:
    def __init__(self, run, summary, applications):
        self.run, self.summary = run, summary
        self.responses = {
            row["id"]: row
            for row in run.manifest["responses"]
            if not row.get("error") and row.get("prompt")
        }
        self.sources = {row["id"]: row for row in run.manifest["sources"]}
        self.segments = {segment.id: segment for _, segment, _ in applications}
        self.texts = {
            ("answer", row["id"]): row["text"] for row in self.responses.values()
        }
        self.texts.update(
            {
                ("citation", row["id"]): "\n".join(
                    value for value in (row.get("title"), row.get("snippet")) if value
                )
                for row in self.sources.values()
            }
        )
        owners = defaultdict(list)
        for segment in self.segments.values():
            if segment.owner_kind == "snapshot":
                owners[segment.owner_id].append(segment)
        for owner, segments in owners.items():
            position, parts = 0, []
            for segment in sorted(segments, key=lambda row: row.start_offset):
                if (
                    segment.start_offset != position
                    or segment.end_offset - position != len(segment.text)
                ):
                    break
                position = segment.end_offset
                parts.append(segment.text)
            else:
                self.texts[("snapshot", owner)] = "".join(parts)
        self.presence = summary.get("brand_presence", {})
        self.targets = {
            row["target_id"]: row for row in self.presence.get("targets", [])
        }
        self.own = [row for row in self.targets.values() if row["role"] == "own"]
        self.by_entity = defaultdict(list)
        for target in self.targets.values():
            if target.get("entity_id"):
                self.by_entity[target["entity_id"]].append(target)
        self.observations = {
            (row["target_id"], row["response_id"]): row
            for row in self.presence.get("observations", [])
            if row.get("response_id") in self.responses
        }
        self.source_proofs = {
            row["id"]: row
            for row in summary.get("source_observations", {}).get("observations", [])
        }
        self.units = summary.get("information_units", {}).get("observations", [])
        self.unit_index = {
            (row["segment_id"], row["local_id"]): row for row in self.units
        }
        self.candidate_heap, self.seen, self.deferred = [], set(), defaultdict(int)
        self.preferences = preference_projection(run.manifest.get("brand_context"))
        self.verified_cells = {}
        for cell in (run.manifest.get("evaluation_cohort") or {}).get("cells", []):
            self.verified_cells[(cell["prompt_hash"], cell["platform"])] = cell

    def scope(self, response):
        cell = self.verified_cells.get(
            (response["prompt_hash"], response["platform"]), {}
        )
        review = (cell.get("question_context") or {}).get("review") or {}
        confirmed = review.get("status") == "confirmed"

        def spans(field):
            return (
                [
                    row
                    for row in review.get(field, [])
                    if type(row.get("start")) is int
                    and type(row.get("end")) is int
                    and 0 <= row["start"] < row["end"] <= len(response["prompt"])
                    and response["prompt"][row["start"] : row["end"]] == row["quote"]
                ]
                if confirmed
                else []
            )

        return {
            "scenario_id": cell.get("primary_scenario") or "unreviewed",
            "review_state": "confirmed" if confirmed else "unreviewed",
            "neutral": confirmed
            and review.get("brand_scope") == "neutral"
            and cell.get("eligibility") == "eligible",
            "eligibility": cell.get("eligibility", "needs_review"),
            "audience": [row["quote"] for row in spans("audience")],
            "requirements": [*spans("requirements"), *spans("constraints")],
        }

    def ref(self, item):
        segment = self.segments.get(item.get("segment_id"))
        if not segment:
            return None
        if (
            item.get("owner_kind", segment.owner_kind) != segment.owner_kind
            or item.get("owner_id", segment.owner_id) != segment.owner_id
        ):
            return None
        start, end, quote = item.get("start"), item.get("end"), item.get("quote")
        text = self.texts.get((segment.owner_kind, segment.owner_id))
        if (
            type(start) is not int
            or type(end) is not int
            or text is None
            or not isinstance(quote, str)
            or not (segment.start_offset <= start < end <= segment.end_offset)
            or text[start:end] != quote
        ):
            return None
        if segment.owner_kind == "snapshot" and not any(
            row.get("snapshot_id") == segment.owner_id
            and row.get("frozen_version_valid")
            and (row.get("content_coverage") or {}).get("stored_text_integrity")
            == "verified"
            for row in self.source_proofs.values()
        ):
            return None
        return EvidenceRef(
            project_id=self.run.project_id,
            run_id=self.run.id,
            owner_kind=segment.owner_kind,
            owner_id=segment.owner_id,
            segment_id=segment.id,
            text_hash=digest(text.encode()),
            quote=quote,
            start=start,
            end=end,
        ).model_dump(mode="json")

    def answer_ref(self, response):
        segment = min(
            (
                row
                for row in self.segments.values()
                if row.owner_kind == "answer" and row.owner_id == response["id"]
            ),
            key=lambda row: row.ordinal,
            default=None,
        )
        if not segment:
            return None
        end = min(segment.end_offset, segment.start_offset + 900)
        return self.ref(
            {
                "segment_id": segment.id,
                "start": segment.start_offset,
                "end": end,
                "quote": response["text"][segment.start_offset : end],
            }
        )

    def subject_target(self, item):
        subject = item.get("subject")
        matches = [
            row
            for row in self.summary.get("mentions", [])
            if row["segment_id"] == item["segment_id"]
            and row.get("observed_entity_name") == subject
            and not row.get("identity_ambiguity")
            and item["start"] <= row["start"] < row["end"] <= item["end"]
        ]
        targets = {
            target["target_id"]: target
            for row in matches
            for target in self.by_entity.get(row["entity_id"], [])
        }
        return next(iter(targets.values())) if len(targets) == 1 else None

    def add(self, own, response, **kwargs):
        if response["platform"] not in (
            "deepseek",
            "qwen",
            "doubao",
            "kimi",
            "hunyuan",
        ):
            self.deferred["unsupported_platform"] += 1
            return
        if not kwargs.get("evidence"):
            self.deferred["no_valid_literal_evidence"] += 1
            return
        key = stable_hash(
            [
                own["target_id"],
                response["id"],
                kwargs["kind"],
                kwargs["goal"],
                kwargs.get("dimension"),
                kwargs["evidence"],
                kwargs.get("source_ids", []),
                kwargs.get("comparison", {}).get("target_id")
                if kwargs.get("comparison")
                else None,
            ]
        )
        if key in self.seen:
            return
        self.seen.add(key)
        candidate = make_candidate(
            self.run, own, response, self.scope(response), **kwargs
        )
        score = (
            candidate["urgency"],
            self.preferences["importance"].get(candidate["scenario_id"], 50),
            int(candidate["actionability"] == "ready_for_review"),
            -int(candidate["task_key"], 16),
        )
        entry = (score, key, candidate)
        if len(self.candidate_heap) < MAX_CANDIDATES:
            heappush(self.candidate_heap, entry)
        else:
            self.deferred["candidate_limit_priority_selection"] += 1
            if entry[:2] > self.candidate_heap[0][:2]:
                heapreplace(self.candidate_heap, entry)


def comparison_opportunities(data):
    for own in data.own:
        for response in data.responses.values():
            current = data.observations.get((own["target_id"], response["id"]))
            if not current:
                continue
            scope = data.scope(response)
            topic = (
                scope["requirements"][0]["quote"]
                if scope["requirements"]
                else response["prompt"]
            )
            rivals = [
                (target, data.observations.get((target["target_id"], response["id"])))
                for target in data.targets.values()
                if target["role"]
                in ("competitor", "comparison", "unregistered", "observed")
                and target.get("identity_status") != "excluded"
            ]
            rivals = [
                (target, row)
                for target, row in rivals
                if row and row["state"] == "present"
            ]
            if (
                current["state"] == "observed_absent"
                and current.get("coverage_complete")
                and own.get("identity_status") == "confirmed"
                and response.get("answer_status", "complete") == "complete"
            ):
                ref = data.answer_ref(response)
                for rival in [target for target, _ in rivals] or [None]:
                    data.add(
                        own,
                        response,
                        kind="scenario_coverage",
                        goal=["need_coverage", topic],
                        topic=topic,
                        finding=f"本次{response['platform']}回答的完整处理结果中，{own['name']}未出现"
                        + (
                            f"，同时观察到{rival['name']}。"
                            if rival
                            else "；这不证明产品不适用或缺少能力。"
                        ),
                        evidence=[ref] if ref else [],
                        comparison=rival,
                        dimension="presence",
                        needs_scope_review=not scope["neutral"],
                        unknowns=["引用来源与缺席之间的原因关系尚未成立"],
                    )
            for rival, other in rivals:
                for dimension in ("recommendation", "position", "reasons"):
                    left, right = (
                        current.get("dimensions", {}).get(dimension),
                        other.get("dimensions", {}).get(dimension),
                    )
                    if left is None or right is None or right <= left:
                        continue
                    refs = [
                        data.ref(row)
                        for row in data.summary.get("mentions", [])
                        if row["id"] in other.get("evidence_ids", [])
                    ]
                    refs = [row for row in refs if row]
                    counter = [
                        data.ref(row)
                        for row in data.summary.get("mentions", [])
                        if row["id"] in current.get("evidence_ids", [])
                    ]
                    data.add(
                        own,
                        response,
                        kind="recommendation_strength",
                        goal=["decision_basis", topic],
                        topic=topic,
                        finding=f"本次{response['platform']}同一回答的{dimension}维度，{own['name']}为{left}、{rival['name']}为{right}；仅为本次观察差异。",
                        evidence=refs,
                        counterevidence=[row for row in counter if row],
                        comparison=rival,
                        dimension=dimension,
                        needs_scope_review=not scope["neutral"],
                        unknowns=["总分缺项不补零，排序差异不等于平台检索原因"],
                    )


def unit_opportunities(data):
    for context in data.summary.get("context_analysis", {}).get("observations", []):
        if context.get("status") != "completed" or not context.get("interpretation"):
            continue
        observed = context["context"]["observation"]
        response = data.responses.get(observed["response_id"])
        if not response:
            continue
        source = data.sources.get(observed.get("source_id"))
        if source and (
            source["kind"] not in ("answer_reference", "search_candidate", "text_link")
            or source.get("url_resolution") != "exact"
        ):
            continue
        matches = defaultdict(list)
        for match in context["interpretation"].get("requirement_matches", []):
            matches[match["unit_id"]].append(match)
        units = [
            row
            for (segment_id, _), row in data.unit_index.items()
            if segment_id == context["segment_id"]
        ]
        for unit in units:
            ref = data.ref(unit)
            if not ref:
                continue
            target = data.subject_target(unit)
            bound = matches[unit["local_id"]]
            addressed = [row for row in bound if row["relation"] == "addresses"]
            conflicts = [row for row in bound if row["relation"] == "contradicts"]
            topic = (
                (addressed or conflicts)[0]["question_evidence"]["quote"]
                if addressed or conflicts
                else unit.get("value") or UNIT_LABELS[unit["kind"]]
            )
            for own in data.own:
                if (
                    target
                    and target["target_id"] == own["target_id"]
                    and addressed
                    and not conflicts
                    and material_readiness(data.run.manifest, own, topic)[
                        "approved_fact_candidates"
                    ]
                ):
                    continue  # Existing positive material is not an invented weakness.
                if not source and not addressed and not conflicts:
                    continue
                actual = source is not None and source["kind"] == "answer_reference"
                kind = "source_channel" if actual else "content_evidence"
                data.add(
                    own,
                    response,
                    kind=kind,
                    goal=[
                        "unit",
                        unit["kind"],
                        topic,
                        unit.get("value"),
                        unit.get("conditions"),
                        unit.get("effective_time"),
                        unit["quote"],
                    ],
                    topic=topic,
                    finding=(
                        "本次实际引用的保存正文"
                        if actual
                        else "本次检索候选/文本链接的保存正文"
                        if source
                        else "本次回答"
                    )
                    + f"包含{UNIT_LABELS[unit['kind']]}：「{unit['quote']}」。"
                    + (
                        "该片段在当前问题中被判定为条件冲突，待核对。"
                        if conflicts
                        else "这是原文表述，尚非我方已批准事实。"
                    ),
                    evidence=[ref],
                    comparison=target
                    if target and target["target_id"] != own["target_id"]
                    else None,
                    intent="material_verification"
                    if conflicts or not addressed
                    else "content_create",
                    unit_kind=unit["kind"],
                    urgency=1 if conflicts else 0,
                    source_ids=[source["id"]] if source else [],
                    observation_kind=source["kind"] if source else "answer",
                    needs_scope_review=not addressed
                    or data.scope(response)["review_state"] != "confirmed",
                    unknowns=[
                        "来源被引用不证明该段是推荐原因",
                        "限定条件未知时不能推广到所有客户/产品",
                    ]
                    if source
                    else ["没有抽取到我方对应信息不等于没有该能力"],
                )


def perception_and_conflict_opportunities(data):
    for mention in data.summary.get("mentions", []):
        if mention["owner_kind"] != "answer" or mention.get("sentiment") != "negative":
            continue
        response = data.responses.get(mention["owner_id"])
        ref = data.ref(mention)
        for own in data.by_entity.get(mention["entity_id"], []):
            if response and own["role"] == "own" and ref:
                data.add(
                    own,
                    response,
                    kind="brand_perception",
                    goal=["perception_review", mention["quote"]],
                    topic="品牌负面表述与适用边界",
                    finding=f"本次{response['platform']}回答中出现针对已绑定主体{own['name']}的负面表述：「{mention['quote']}」。尚未判断客观真伪。",
                    evidence=[ref],
                    intent="material_verification",
                    urgency=2,
                    unknowns=["负面表述需要核对对象、时间和适用条件，不能直接判为错误"],
                )
    for evidence in data.summary.get("claim_evidence", []):
        if evidence["relation"] not in ("contradicts", "partial"):
            continue
        response = data.responses.get(evidence.get("response_id"))
        span = evidence.get("verification", {}).get("answer_span")
        source_span = evidence.get("verification", {}).get("source_span")
        if not response or not span or not source_span:
            continue
        answer = data.ref({**span, "segment_id": evidence.get("answer_segment_id")})
        original = data.ref(
            {**source_span, "segment_id": evidence["source_segment_id"]}
        )
        if not answer or not original:
            continue
        matching = [
            row
            for row in data.summary.get("claims", [])
            if row["owner_kind"] == "answer"
            and row["owner_id"] == response["id"]
            and row["start"] == span["start"]
            and row["end"] == span["end"]
        ]
        target = data.subject_target(matching[0]) if len(matching) == 1 else None
        for own in data.own:
            data.add(
                own,
                response,
                kind="content_evidence",
                goal=[
                    "verify_conflict",
                    evidence["answer_quote"],
                    evidence["source_quote"],
                ],
                topic="回答观点与来源原文的冲突核对",
                finding=f"本次回答「{evidence['answer_quote']}」与实际引用保存片段「{evidence['source_quote']}」的模型关系为{evidence['relation']}，尚待人工核实。",
                evidence=[answer],
                counterevidence=[original],
                source_ids=[evidence["citation_id"]],
                comparison=target
                if target and target["target_id"] != own["target_id"]
                else None,
                intent="material_verification",
                urgency=3 if target and target["target_id"] == own["target_id"] else 1,
                unknowns=[
                    "冲突对象未确认属于我方时，只生成核查材料任务",
                    "模型冲突判断不是事实审批，不可据此直接对外纠错",
                ],
            )


def platform_opportunities(data):
    for own in data.own:
        grouped = defaultdict(list)
        for response in data.responses.values():
            observation = data.observations.get((own["target_id"], response["id"]))
            if observation and observation["state"] != "unknown":
                grouped[response["prompt_hash"]].append((response, observation))
        for prompt_hash, rows in grouped.items():
            for (left, first), (right, second) in combinations(rows, 2):
                if left["platform"] == right["platform"]:
                    continue
                differences = [
                    key
                    for key in ("presence", "recommendation", "position", "reasons")
                    if first["dimensions"].get(key) is not None
                    and second["dimensions"].get(key) is not None
                    and first["dimensions"][key] != second["dimensions"][key]
                ]
                for dimension in differences:
                    weak, strong = (
                        (left, right)
                        if first["dimensions"][dimension]
                        < second["dimensions"][dimension]
                        else (right, left)
                    )
                    ref, counter = data.answer_ref(weak), data.answer_ref(strong)
                    data.add(
                        own,
                        weak,
                        kind="platform_difference",
                        goal=["platform_observation", prompt_hash, dimension],
                        topic=weak["prompt"],
                        finding=f"同一问题的本次回答中，{own['name']}的{dimension}维度在{weak['platform']}与{strong['platform']}不同；保留两边原文及条件，不判断平台固有偏好。",
                        evidence=[ref] if ref else [],
                        counterevidence=[counter] if counter else [],
                        dimension=dimension,
                        needs_scope_review=not data.scope(weak)["neutral"]
                        or not data.scope(strong)["neutral"],
                        unknowns=[
                            f"参考平台为{strong['platform']}；采集条件需逐项核对，跨平台差异不作因果或稳定效果判断"
                        ],
                    )


def build_opportunities(run, summary, applications):
    data = Inputs(run, summary, applications)
    comparison_opportunities(data)
    unit_opportunities(data)
    perception_and_conflict_opportunities(data)
    platform_opportunities(data)
    items, policy = rank_opportunities(
        merge_candidates(
            run,
            [
                row[2]
                for row in sorted(
                    data.candidate_heap, key=lambda item: item[:2], reverse=True
                )
            ],
        ),
        run.manifest.get("brand_context"),
    )
    deferred_tasks = max(0, len(items) - MAX_TASKS)
    input_quality = {
        "information_units": summary.get("information_units", {}).get(
            "status", "pending"
        ),
        "context_analysis": summary.get("context_analysis", {}).get(
            "status", "pending"
        ),
        "unknown_presence": sum(
            row["state"] == "unknown" for row in data.observations.values()
        ),
        "skipped_responses": len(run.manifest["responses"]) - len(data.responses),
    }
    status = (
        "needs_own_target"
        if not data.own
        else "partial"
        if (
            data.deferred
            or deferred_tasks
            or input_quality["information_units"] != "processed"
            or input_quality["context_analysis"] != "processed"
            or input_quality["unknown_presence"]
            or input_quality["skipped_responses"]
        )
        else "evaluated"
    )
    return {
        "version": VERSION,
        "status": status,
        "own_target_count": len(data.own),
        "candidate_count": len(data.seen),
        "candidate_limit": MAX_CANDIDATES,
        "task_limit": MAX_TASKS,
        "deferred_candidates": dict(data.deferred),
        "deferred_task_count": deferred_tasks,
        "policy": policy,
        "input_quality": input_quality,
        "items": items[:MAX_TASKS],
        "no_findings_meaning": "no_qualified_findings_in_processed_input_not_proof_of_no_opportunity",
    }
