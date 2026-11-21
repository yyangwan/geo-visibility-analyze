"""Brand candidates anchored in the answer body; citations stay evidence."""

import re
import unicodedata
from collections import defaultdict

from app.intelligence.question_context import literal_matches

REFERENCE_SECTION = re.compile(
    r"^\s*(?:#{1,6}\s+)?(?:\*\*|__)?(?:参考来源|参考文献|参考资料|参考链接|引用来源|"
    r"引用链接|参考资料与来源|参考与来源|参考来源列表|信源列表|sources|references|citations)"
    r"(?:\*\*|__)?\s*(?:[:：]|$)",
    re.IGNORECASE,
)
REFERENCE_LINE = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:\*\*|__)?(?:来源|信源|参考来源|引用来源|source|citation)"
    r"\s*(?:\*\*|__)?\s*[:：]",
    re.IGNORECASE,
)
NON_BRANDS = frozenset(
    "AI SEO GEO AEO LLM LLMO SaaS B2B B2C CRM ERP API AI搜索 AI可见度 品牌口碑 "
    "大模型 主流平台 国内主流平台 合作伙伴 问题分类体系 自助工具 自助优化 "
    "内容优化 引用来源 可见度监测 品牌分析 商品卡监测 搜索引擎 知识库".casefold().split()
)
DESCRIPTIVE_SEGMENT = re.compile(
    r"^(?:推荐|支持|适合|可核验能力|适用场景|主流|企业专属)|"
    r"(?:标准版|企业版|专业版|旗舰版|基础版|入门版|免费版|体验版|高级版|会员版|"
    r"套餐|选型指南|品牌监测功能|监测看板)$"
)
PRODUCT_LABEL = re.compile(
    r"\s+(?:toolkit|brand radar|(?:ai )?visibility checker|aeo grader|lite)$",
    re.IGNORECASE,
)


def normalized_name(name):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", name).casefold())


def body_end(text):
    offset = 0
    for line in text.splitlines(keepends=True):
        if REFERENCE_SECTION.match(line):
            return offset
        offset += len(line)
    return len(text)


def answer_prose(text):
    """Mask reference lines without shifting the offsets of the answer prose."""
    return "".join(
        " " * len(line) if REFERENCE_LINE.match(line) else line
        for line in text[: body_end(text)].splitlines(keepends=True)
    )


def answer_brand_mentions(manifest, entities, mentions):
    registry = {
        normalized_name(name)
        for target in manifest.get("brand_targets", ())
        for name in target["names"]
    }
    entity_map = {row["id"]: row for row in entities}
    responses = {
        row["id"]: row.get("text") or ""
        for row in manifest.get("responses", ())
        if not row.get("error")
    }
    prose = {response_id: answer_prose(text) for response_id, text in responses.items()}
    for mention in mentions:
        if mention["owner_kind"] != "answer":
            continue
        entity = entity_map.get(mention["entity_id"])
        body = prose.get(mention["owner_id"])
        if not entity or body is None:
            continue
        name = mention.get("observed_entity_name") or entity["name"]
        key = normalized_name(name)
        registered = key in registry
        if not registered and (
            mention.get("observed_entity_kind", entity.get("kind")) != "brand"
            or not any(char.isalpha() for char in key)
            or key in NON_BRANDS
            or DESCRIPTIVE_SEGMENT.search(key)
            or PRODUCT_LABEL.search(unicodedata.normalize("NFKC", name))
            or any(mark in name for mark in "，。；！？\n")
        ):
            continue
        start, end, quote = (
            mention.get("start"),
            mention.get("end"),
            mention.get("quote"),
        )
        if (
            isinstance(start, int)
            and isinstance(end, int)
            and 0 <= start < end <= len(body)
            and responses[mention["owner_id"]][start:end] == quote
            and literal_matches(body[start:end], name)
        ):
            yield mention


def discovered_brands(manifest, summary):
    """Read-side projection also cleans already published analysis revisions."""
    entities = {row["id"]: row for row in summary.get("entities", ())}
    responses = {row["id"]: row["platform"] for row in manifest.get("responses", ())}
    counts = defaultdict(lambda: defaultdict(set))
    observed_members = defaultdict(set)
    observed_names = defaultdict(set)
    for mention in answer_brand_mentions(
        manifest, entities.values(), summary.get("mentions", ())
    ):
        entity_id, response_id = mention["entity_id"], mention["owner_id"]
        counts[entity_id][responses[response_id]].add(response_id)
        observed_members[entity_id].add(mention.get("observed_entity_id") or entity_id)
        observed_names[entity_id].add(
            mention.get("observed_entity_name") or entities[entity_id]["name"]
        )
    return [
        {
            **entities[entity_id],
            "member_ids": sorted(observed_members[entity_id]),
            "observed_entities": [
                row
                for row in entities[entity_id].get("observed_entities", ())
                if row["id"] in observed_members[entity_id]
            ],
            "observed_names": sorted(observed_names[entity_id]),
            "response_count": sum(len(ids) for ids in platforms.values()),
            "platform_response_counts": {
                platform: len(ids) for platform, ids in platforms.items()
            },
        }
        for entity_id, platforms in counts.items()
    ]


def brand_target_visible(target, body_entity_ids):
    return (
        bool(target.get("registry_brand_id"))
        or target.get("entity_id") in body_entity_ids
    )


def restrict_brand_presence(presence, brands):
    entity_ids = {row["id"] for row in brands}
    targets = [
        row for row in presence["targets"] if brand_target_visible(row, entity_ids)
    ]
    target_ids = {row["target_id"] for row in targets}
    return {
        **presence,
        "targets": targets,
        "observations": [
            row
            for row in presence.get("observations", ())
            if row["target_id"] in target_ids
        ],
        "scope_scores": [
            row
            for row in presence.get("scope_scores", ())
            if row["target_id"] in target_ids
        ],
    }
