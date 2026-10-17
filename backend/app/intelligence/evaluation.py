"""Offline, blinded annotation and version/platform-scoped quality measurement.

No provider calls, database writes, qualification grants or publishing effects.
"""

from collections import Counter, defaultdict
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from app.intelligence.semantics import stable_hash

FORMAT = "citation-evaluation-1"
RELATIONS = ("supports", "partial", "contradicts", "unverified")
PRIORITY_LABELS = {
    "repair_citation_and_platform_binding": "先修复引用与平台归属，不能据串用证据制定品牌建议。",
    "repair_verbatim_spans_and_unicode_offsets": "检查逐字证据片段和Unicode位置，排除虚构或错位引用。",
    "resolve_annotation_disagreement": "先核对标注分歧，统一主体、范围和时间的判断标准。",
    "check_capture_budget_and_worker_completion": "检查采集、预算和worker完成状态，缺失分析不能当作品牌缺席。",
    "review_entity_false_positives_and_identity_context": "核查实体误报及同名上下文，避免把不同主体混在一起。",
    "review_missing_mentions_and_long_tail_coverage": "检查漏识别实体和长回答尾部，尤其是未登记品牌。",
    "review_support_scope_numbers_and_time": "支持精度观察值低于95%目标，重点复核适用范围、数字和时间；这不是发布门槛判定。",
}


class EvaluationInvalid(ValueError):
    pass


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Mention(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["brand", "organization", "product", "person", "other"]
    quote: str = Field(min_length=1, max_length=200)
    start: StrictInt = Field(ge=0)
    end: StrictInt = Field(gt=0)
    sentiment: Literal["positive", "neutral", "negative", "unknown"]


class Case(StrictModel):
    case_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    input_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    group_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    origin: Literal["stored_audit", "synthetic"]
    split: Literal["development", "holdout"]
    platform: str = Field(min_length=1, max_length=50)
    signature: str = Field(pattern=r"^[a-f0-9]{64}$")
    run_id: str = Field(min_length=1, max_length=64)
    task: Literal["entities", "relation"]
    owner_kind: Literal["answer", "citation", "snapshot"]
    owner_id: str = Field(min_length=1, max_length=64)
    segment_id: str = Field(min_length=1, max_length=64)
    observation_id: str = Field(min_length=1, max_length=64)
    global_offset: StrictInt = Field(ge=0)
    text: str = Field(min_length=1)
    prompt: str
    source_kind: str
    content_status: str
    answer_quote: str = ""
    evidence_quote: str = ""
    citation_eligible: StrictBool = False


class Prediction(StrictModel):
    case_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    input_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    signature: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["completed", "partial", "deferred", "missing"]
    entities: list[Mention] = Field(default_factory=list)
    relation: Literal["supports", "partial", "contradicts", "unverified"] | None = None
    source_quote: str = ""


class Annotation(StrictModel):
    case_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    input_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    reviewer_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{3,80}$")
    status: Literal["pending", "reviewed"]
    # Null is unreviewed, [] is an explicitly reviewed negative sample.
    entities: list[Mention] | None = None
    relation: Literal["supports", "partial", "contradicts", "unverified"] | None = None
    note: str = Field(default="", max_length=2000)


def freeze_case(**fields):
    value = {**fields, "case_id": "0" * 64, "input_hash": "0" * 64}
    case = Case.model_validate(value)
    payload = case.model_dump(exclude={"case_id", "input_hash"})
    key = stable_hash(payload)
    return case.model_copy(
        update={
            "case_id": key,
            "input_hash": stable_hash({"schema": FORMAT, **payload}),
        }
    )


def check_case(case):
    expected = freeze_case(**case.model_dump(exclude={"case_id", "input_hash"}))
    if case.case_id != expected.case_id or case.input_hash != expected.input_hash:
        raise EvaluationInvalid("Case input hash mismatch")
    if case.task == "relation" and not case.answer_quote:
        raise EvaluationInvalid("Relation case requires an answer claim")


def mention_key(item):
    return item.start, item.end, item.name, item.kind, item.quote


def valid_mention(text, item):
    return (
        item.start < item.end <= len(text)
        and text[item.start : item.end] == item.quote
        and item.name in item.quote
    )


def ratio(numerator, denominator):
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
    }


def canonical_annotation(case, annotation):
    if case.task == "entities":
        if annotation.entities is None or annotation.relation is not None:
            raise EvaluationInvalid(
                "Entity annotation needs an explicit complete mention list"
            )
        if any(not valid_mention(case.text, item) for item in annotation.entities):
            raise EvaluationInvalid("Gold mention is not an exact input span")
        keys = [mention_key(item) for item in annotation.entities]
        if len(keys) != len(set(keys)):
            raise EvaluationInvalid("Gold annotation contains duplicate mentions")
        return sorted(
            (mention_key(item), item.sentiment) for item in annotation.entities
        )
    if annotation.relation is None or annotation.entities is not None:
        raise EvaluationInvalid("Relation annotation needs an explicit relation")
    if annotation.relation != "unverified" and (
        not case.evidence_quote or case.evidence_quote not in case.text
    ):
        raise EvaluationInvalid(
            "Gold support or conflict needs an exact evidence quote"
        )
    return annotation.relation


def evaluate(cases, predictions, annotations, *, development_group_ids=()):
    cases = [Case.model_validate(row) for row in cases]
    predictions = [Prediction.model_validate(row) for row in predictions]
    annotations = [Annotation.model_validate(row) for row in annotations]
    index = {}
    for case in cases:
        check_case(case)
        if case.case_id in index:
            raise EvaluationInvalid("Duplicate evaluation case")
        index[case.case_id] = case
    development = set(development_group_ids) | {
        case.group_id for case in cases if case.split == "development"
    }
    if any(case.split == "holdout" and case.group_id in development for case in cases):
        raise EvaluationInvalid("Holdout prompt group overlaps development inputs")
    predicted = {}
    for prediction in predictions:
        case = index.get(prediction.case_id)
        if (
            case is None
            or prediction.case_id in predicted
            or prediction.input_hash != case.input_hash
            or prediction.signature != case.signature
        ):
            raise EvaluationInvalid("Prediction scope, version or input mismatch")
        if case.task == "entities" and (
            prediction.relation is not None or prediction.source_quote
        ):
            raise EvaluationInvalid("Wrong prediction task")
        if case.task == "relation" and prediction.entities:
            raise EvaluationInvalid("Wrong prediction task")
        if (
            case.task == "relation"
            and prediction.status in ("completed", "partial")
            and prediction.source_quote != case.evidence_quote
        ):
            raise EvaluationInvalid(
                "Prediction evidence quote differs from the reviewed input"
            )
        if prediction.status in ("deferred", "missing") and (
            prediction.entities
            or prediction.relation is not None
            or prediction.source_quote
        ):
            raise EvaluationInvalid("Missing prediction cannot carry results")
        predicted[prediction.case_id] = prediction
    labels, reviewers = defaultdict(list), set()
    for annotation in annotations:
        case = index.get(annotation.case_id)
        key = annotation.case_id, annotation.reviewer_id
        if case is None or annotation.input_hash != case.input_hash or key in reviewers:
            raise EvaluationInvalid("Annotation scope, input or reviewer duplication")
        reviewers.add(key)
        if annotation.status == "reviewed":
            canonical_annotation(case, annotation)
            labels[case.case_id].append(annotation)
        elif annotation.entities is not None or annotation.relation is not None:
            raise EvaluationInvalid("Pending annotation cannot carry gold labels")
    groups = defaultdict(list)
    for case in cases:
        groups[
            (
                case.signature,
                case.platform,
                case.task,
                case.owner_kind,
                case.source_kind,
                case.content_status,
                case.split,
                case.origin,
            )
        ].append(case)
    results = []
    for key, population in sorted(groups.items()):
        coverage = Counter()
        kind_counts = defaultdict(Counter)
        tp = fp = fn = sentiment_correct = sentiment_total = invalid_spans = 0
        support_correct = support_total = citation_violations = 0
        relation_correct = relation_total = abstained = 0
        confusion = {
            gold: {name: 0 for name in (*RELATIONS, "missing")} for gold in RELATIONS
        }
        case_results = []
        for case in population:
            reviews = labels[case.case_id]
            values = [canonical_annotation(case, row) for row in reviews]
            review_status = (
                "unreviewed"
                if not values
                else "single_review"
                if len(values) < 2
                else "agreed"
                if all(value == values[0] for value in values)
                else "disagreement"
            )
            coverage[review_status] += 1
            prediction = predicted.get(case.case_id)
            available = bool(
                prediction and prediction.status in ("completed", "partial")
            )
            coverage["prediction_available" if available else "prediction_missing"] += 1
            invalid = 0
            if available and case.task == "entities":
                invalid = sum(
                    not valid_mention(case.text, item) for item in prediction.entities
                )
                invalid_spans += invalid
            if (
                available
                and case.task == "relation"
                and (
                    (
                        prediction.source_quote
                        and prediction.source_quote not in case.text
                    )
                    or (
                        prediction.relation in ("supports", "partial", "contradicts")
                        and (not case.citation_eligible or not prediction.source_quote)
                    )
                )
            ):
                citation_violations += 1
                invalid = 1
            case_results.append(
                {
                    "case_id": case.case_id,
                    "review_status": review_status,
                    "prediction_status": prediction.status if prediction else "missing",
                    "structural_error": bool(invalid),
                }
            )
            if review_status != "agreed":
                continue
            if case.task == "entities":
                gold = Counter(mention_key(item) for item in reviews[0].entities)
                observed = (
                    Counter(mention_key(item) for item in prediction.entities)
                    if available
                    else Counter()
                )
                matched = gold & observed
                tp += sum(matched.values())
                fp += sum((observed - gold).values())
                fn += sum((gold - observed).values())
                for counter, field in (
                    (matched, "tp"),
                    (observed - gold, "fp"),
                    (gold - observed, "fn"),
                ):
                    for mention, count in counter.items():
                        kind_counts[mention[3]][field] += count
                predicted_sentiments = defaultdict(list)
                if available:
                    for item in prediction.entities:
                        predicted_sentiments[mention_key(item)].append(item.sentiment)
                for item in reviews[0].entities:
                    if item.sentiment != "unknown" and matched[mention_key(item)]:
                        sentiment_total += 1
                        # Repeated predictions cannot choose the most favorable sentiment.
                        sentiment_correct += (
                            predicted_sentiments[mention_key(item)][0] == item.sentiment
                        )
            else:
                actual = (
                    prediction.relation
                    if available and prediction.relation
                    else "missing"
                )
                gold = reviews[0].relation
                confusion[gold][actual] += 1
                relation_total += 1
                relation_correct += actual == gold
                abstained += actual == "unverified"
                if actual == "supports":
                    support_total += 1
                    support_correct += gold == "supports" and not invalid
        priorities = []
        for condition, code in [
            (citation_violations, "repair_citation_and_platform_binding"),
            (invalid_spans, "repair_verbatim_spans_and_unicode_offsets"),
            (coverage["disagreement"], "resolve_annotation_disagreement"),
            (
                coverage["prediction_missing"],
                "check_capture_budget_and_worker_completion",
            ),
            (fp, "review_entity_false_positives_and_identity_context"),
            (fn, "review_missing_mentions_and_long_tail_coverage"),
            (
                support_total and support_correct / support_total < 0.95,
                "review_support_scope_numbers_and_time",
            ),
        ]:
            if condition:
                priorities.append(code)
        results.append(
            {
                "signature": key[0],
                "platform": key[1],
                "task": key[2],
                "owner_kind": key[3],
                "source_kind": key[4],
                "content_status": key[5],
                "split": key[6],
                "origin": key[7],
                "cases": len(population),
                "prompt_groups": len({row.group_id for row in population}),
                "annotation_coverage": dict(coverage),
                "case_results": case_results,
                "review_priorities": priorities,
                "entity_precision": ratio(tp, tp + fp),
                "entity_recall": ratio(tp, tp + fn),
                "entity_counts": {
                    "true_positive": tp,
                    "false_positive": fp,
                    "false_negative": fn,
                },
                "entity_by_kind": {
                    kind: {
                        "precision": ratio(counts["tp"], counts["tp"] + counts["fp"]),
                        "recall": ratio(counts["tp"], counts["tp"] + counts["fn"]),
                    }
                    for kind, counts in sorted(kind_counts.items())
                },
                "matched_entity_sentiment_accuracy": ratio(
                    sentiment_correct, sentiment_total
                ),
                "invalid_prediction_spans": invalid_spans,
                "citation_chain_violations": citation_violations,
                "relation_accuracy": ratio(relation_correct, relation_total),
                "support_precision": ratio(support_correct, support_total),
                "unverified_predictions_in_reviewed_pairs": abstained,
                "relation_confusion": confusion,
            }
        )
    return {
        "schema": FORMAT,
        "dataset_hash": stable_hash([case.model_dump() for case in cases]),
        "prediction_hash": stable_hash([row.model_dump() for row in predictions]),
        "annotation_hash": stable_hash([row.model_dump() for row in annotations]),
        "case_count": len(cases),
        "prompt_groups": len({row.group_id for row in cases}),
        "strata": results,
        "release_eligible": False,
        "qualification": "manual_review_required",
        "limitations": [
            "release_threshold_policy_not_frozen",
            "reviewer_independence_is_operator_declared",
            "synthetic_cases_are_not_real_model_validation",
            "stored_pairs_do_not_measure_alignment_recall",
            "identity_disambiguation_and_action_usefulness_not_scored",
        ],
        "origin_counts": dict(Counter(case.origin for case in cases)),
    }


def render_report(report):
    """Explain internal analysis checks in plain language, retaining exact counts."""

    def safe(value):
        return (
            str(value)
            .replace("|", "\\|")
            .replace("\n", " ")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    platforms = {
        "qwen": "通义千问",
        "deepseek": "DeepSeek",
        "doubao": "豆包",
        "hunyuan": "腾讯元宝",
        "kimi": "Kimi",
        "wenxin": "文心一言",
    }
    owners = {
        "answer": "平台回答",
        "citation": "来源标题与摘要",
        "snapshot": "来源文章段落",
    }
    statuses = {
        "complete": "回答完整",
        "partial": "内容不完整",
        "captured": "正文已保存",
        "blocked": "来源访问受限",
        "unknown": "完整性尚未确认",
        "unavailable": "来源不可用",
        "pending": "正文尚未取得",
    }
    sources = {
        "answer_reference": "回答实际引用",
        "search_candidate": "搜索候选，不能当作实际引用",
        "text_link": "普通文本链接",
        "named_inference": "推测来源",
    }
    synthetic_only = (
        bool(report["case_count"])
        and report["origin_counts"].get("synthetic", 0) == report["case_count"]
    )
    lines = [
        "# 分析结果读对了吗？",
        "",
        "这份报告检查分析系统是否正确识别采集内容中的品牌、主体及证据关系，帮助团队修正分析功能。平台名称表示样本来自哪里。",
        "",
        "## 先看结论",
        "",
    ]
    if synthetic_only:
        lines.append(
            "**这是合成演示，不能据此判断真实分析质量，也不能判断哪个平台更好。** 样本和对照答案均为预设，用来展示发现问题后该怎么看、怎么处理。"
        )
    elif not report["case_count"]:
        lines.append(
            "**本次没有可评测样本，暂时不能得出分析质量结论。** 请先检查审计状态及是否已有分析结果。"
        )
    else:
        lines.append(
            "**本次结果仍需人工复核。** 下面分别说明各平台样本中已核对的问题，不合并成一个总分，也不自动批准内容材料。"
        )
    lines.extend(
        [
            "",
            f"本次有 **{report['case_count']} 个检查样本**，来自 **{report['prompt_groups']} 个问题**。同题的不同平台回答、正文片段或多轮结果，不代表更多独立问题。",
            "",
            "阅读时重点看：有没有误认或遗漏主体、有没有把不支持的证据判为支持，以及下一步先修什么。",
            "",
            "## 逐项看问题",
            "",
        ]
    )
    for number, row in enumerate(report["strata"], 1):
        platform = safe(platforms.get(row["platform"], row["platform"]))
        task = "品牌与主体识别" if row["task"] == "entities" else "观点与引用核对"
        synthetic = row["origin"] == "synthetic"
        coverage = row["annotation_coverage"]
        agreed = coverage.get("agreed", 0)
        lines.extend([f"### 检查 {number}：{platform}来源样本 · {task}", ""])
        context = f"检查内容：{owners.get(row['owner_kind'], '其他内容')}；{statuses.get(row['content_status'], '采集状态待确认')}；{'合成演示' if synthetic else '审计记录'}。"
        if row["source_kind"] != "answer":
            context += (
                f" 来源类型：{sources.get(row['source_kind'], '尚未确认的来源类型')}。"
            )
        lines.append(context)
        label = "预设对照答案一致" if synthetic else "两位及以上标注者结果一致"
        lines.extend(
            [
                "",
                f"本项 {row['cases']} 个样本、{row['prompt_groups']} 个问题；{label}的样本 {agreed} 个，只有一份标注 {coverage.get('single_review', 0)} 个、尚未核对 {coverage.get('unreviewed', 0)} 个、有分歧 {coverage.get('disagreement', 0)} 个。分析结果缺失 {coverage.get('prediction_missing', 0)} 个。",
                "",
            ]
        )
        if row["task"] == "entities":
            counts = row["entity_counts"]
            tp, fp, fn = (
                counts["true_positive"],
                counts["false_positive"],
                counts["false_negative"],
            )
            if not agreed:
                lines.append(
                    "**暂时无法评价识别表现**：还没有一致的对照答案，不能把未核对当作没有品牌。"
                )
            elif not (tp or fp or fn):
                lines.append(
                    "**这组已核对内容没有需要识别的主体，系统也没有报出主体。** 因此这一项无法说明品牌识别能力；空分母（分母为0）不会算成0分或满分。"
                )
            else:
                lines.extend(
                    [
                        "| 检查结果 | 本次记录 |",
                        "| --- | --- |",
                        f"| 识别正确 | {tp} 处 |",
                        f"| 误认：把不应识别的内容报成主体 | {fp} 处 |",
                        f"| 遗漏：原文有主体，系统没有识别出来 | {fn} 处 |",
                        "",
                        f"在已核对样本中，系统报出的 {tp + fp} 处提及里，{tp} 处正确；对照答案中的 {tp + fn} 处提及里，找到了 {tp} 处。",
                    ]
                )
            if row["invalid_prediction_spans"]:
                lines.extend(
                    [
                        "",
                        f"另有 **{row['invalid_prediction_spans']} 处识别结果无法与原文位置对应**，需核查是否虚构、文字错位或位置计算错误。",
                    ]
                )
        else:
            support = row["support_precision"]
            accuracy = row["relation_accuracy"]
            if support["denominator"]:
                lines.append(
                    f"系统判为“有原文支持”的 **{support['denominator']} 条**已核对结果中，**{support['numerator']} 条**同时满足“证据支持观点”和“引用归属有效”。"
                )
                wrong = row["relation_confusion"]
                problems = [
                    (wrong["contradicts"]["supports"], "与所选证据矛盾"),
                    (wrong["partial"]["supports"], "所选证据只支持一部分"),
                    (wrong["unverified"]["supports"], "证据不足以确认"),
                ]
                details = "；".join(
                    f"{count} 条{description}"
                    for count, description in problems
                    if count
                )
                if details:
                    lines.extend(["", f"**需要纠正的支持判断：{details}。**"])
                if support["denominator"] == support["numerator"]:
                    lines.extend(
                        [
                            "",
                            "本次这些判断对上了，但样本结果不能直接代表稳定能力，尤其不能据此给平台排名。",
                        ]
                    )
            else:
                lines.append(
                    "本项暂时没有经过一致核对的“有原文支持”判断，不能评价这类判断是否可靠。"
                )
            if accuracy["denominator"]:
                lines.extend(
                    [
                        "",
                        f"把支持、部分支持、矛盾、待核实一起看，全部 {accuracy['denominator']} 条已核对结果中，{accuracy['numerator']} 条判断一致；缺失的分析结果也计入检查。",
                    ]
                )
            if row["citation_chain_violations"]:
                lines.extend(
                    [
                        "",
                        f"**另有 {row['citation_chain_violations']} 条引用归属或证据片段不满足要求。** 例如搜索候选、其他平台的来源或无法匹配原文的引用，不能当作已验证支持。",
                    ]
                )
        lines.extend(["", "**下一步怎么处理**", ""])
        if row["review_priorities"]:
            lines.extend(
                f"- {PRIORITY_LABELS[code]}" for code in row["review_priorities"]
            )
        elif not agreed:
            lines.append("- 先完成独立标注并解决分歧，再评价分析质量。")
        else:
            lines.append(
                "- 继续补充不同问题和真实样本，观察是否还有同类错误；当前没有发现的错误不等于以后不会出现。"
            )
        lines.extend(
            [
                "",
                f"追溯信息：分析版本 `{row['signature'][:12]}`；{'留出检查' if row['split'] == 'holdout' else '开发调试'}。",
                "",
            ]
        )
    lines.extend(
        [
            "## 这份报告还不能回答什么",
            "",
            "- 不能证明真实模型已经达到上线质量，也不能用合成样本比较平台优劣。",
            "- 只检查已经保存的观点对，尚不能判断遗漏了多少观点或没有找到多少支持材料。",
            "- 同名主体消歧、建议是否可执行，还需要单独核查。",
            "- 当前报告不会自动批准材料或发布；真实独立样本、审核标准和每类样本数量仍需确认。",
            "",
            "JSON文件保留详细分子分母、逐条检查状态和记录编号，供开发团队追查；读者不必理解这些技术字段才能阅读上面的结论。",
            "",
        ]
    )
    return "\n".join(lines)
