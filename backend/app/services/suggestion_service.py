"""AI-powered optimization suggestion service.

Two-pass LLM generation:
  Pass 1 — Generate 5-8 strategic suggestions from audit data.
  Pass 2 — Expand each suggestion into a detailed action plan
           (channel, outline, timeline, keywords, competitor reference).
"""

import json
import logging
from collections import Counter, defaultdict

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.models import (
    Audit,
    PlatformResponseRecord,
    Prompt,
    QueryResult,
    Report,
    ResponseAnalysis,
    SourceCitation,
    Suggestion,
)
from app.services.audit_service import BrandData
from app.services.source_quality import clean_cited_sources

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pass 1 — strategic suggestions
# ---------------------------------------------------------------------------

_PASS1_SYSTEM = """你是一个AI搜索优化专家。根据品牌在各AI平台的可见性审计报告，生成具体可操作的优化建议。

每条建议必须：
1. 直接回应审计证据，不要泛泛谈内容建设或SEO。
2. 明确指出在哪个渠道/平台执行（如小红书、知乎、官网博客、微信公众号、B站、抖音）
3. 指出针对哪个AI平台的优化目标（如Kimi、DeepSeek、通义千问）
4. 给出具体动作（如"发布产品对比评测文章"、"优化官网FAQ页面添加结构化数据"）
5. 说明该动作对应哪条审计发现，以及完成后用什么指标验收。

优先覆盖这些维度：
- 平台缺口：哪个AI平台低分、未提及、推荐排名靠后。
- 场景缺口：哪些prompt/用户问题下品牌缺席。
- 竞品压制：哪些问题里竞品被提及或推荐而本品牌缺席。
- 内容资产：需要补什么页面、文章、问答、测评、案例或结构化数据。
- 渠道分发：该内容应该发布到官网、知乎、小红书、B站、公众号还是行业媒体。
- 证据闭环：建议必须能回指到审计样本，并给出下次审计的目标指标。

请以JSON数组格式返回建议，每条建议包含：
- category: 分类（content_optimization/seo_strategy/platform_focus/competitive_strategy）
- title: 建议标题（简短，20字内）
- description: 详细说明（含具体行动步骤，220字内，必须包含执行渠道、目标AI平台、对应审计问题）
- priority: 优先级（high/medium/low）
- target_platforms: 目标AI平台列表，如["kimi","deepseek"]
- evidence_sources: 证据引用来源网站数组，必须来自审计证据样本中的真实域名或URL，如["zhihu.com","36kr.com","brand.com/docs"]
- evidence_channels: 证据来源渠道数组，可由 evidence_sources 推导，如["知乎","官网博客","百度百科"]
- action_sources: 实际行动落点网站数组，1-3个，必须到域名/站点或具体栏目层级，如["zhihu.com","brand.com/blog","36kr.com"]
- action_channels: 实际执行渠道数组，1-3个，必须与 action_sources 对应
- action_type: 具体动作类型，如"发布评测文章"、"优化FAQ页面"、"补充Schema标记"、"创建对比页面"
- audit_findings: 该建议回应的审计发现数组，2-4条，每条要包含平台或prompt线索
- evidence_summary: 一句话说明建议来自哪类审计证据
- success_metric: 下次审计可验证的成功指标，如"DeepSeek相关prompt提及率从0%提升到40%"

只返回JSON数组，不要其他文字。"""

_PASS1_USER = """请分析以下品牌AI可见性报告并给出优化建议：

项目信息：{project_info}
综合评分：{overall_score}/100
品牌提及率：{mention_rate:.1%}
竞品排名：{competitor_rank}
各平台评分：{platform_scores}
已有洞察：{insights}

竞品对比：{competitor_info}
低分平台详情：{weak_platforms}
{analysis_context}
{evidence_context}

请生成5-8条优化建议。每条建议必须明确：在哪个渠道做什么、优化哪个AI平台、回应哪条审计证据、使用什么关键词、如何验收。"""

# ---------------------------------------------------------------------------
# Pass 2 — detailed action plan
# ---------------------------------------------------------------------------

_PASS2_SYSTEM = """你是一个AI搜索优化执行顾问。用户会给一条优化建议和相关的审计数据，请将其扩展为详细可执行的行动方案。

请以JSON对象格式返回（不要数组），包含：
- evidence_sources: 证据引用来源网站，数组，必须使用审计证据样本中的真实域名或URL，不能只写"知乎/官网博客/小红书"
- evidence_channels: 证据来源渠道（如小红书、知乎、官网博客），必须能从 evidence_sources 推导
- action_sources: 执行落点网站，数组，必须到域名/站点或具体栏目层级，如"brand.com/blog"、"zhihu.com"、"36kr.com"
- action_channels: 执行渠道（如小红书、知乎、官网博客），必须与 action_sources 对应
- action_type: 动作类型（如发布评测文章、优化FAQ页面、增加Schema标记、创建对比页面）
- outline: 内容大纲，3-5个要点组成的数组
- keywords: 建议使用的关键词列表，5-8个
- timeline: 执行时间线，数组，每项包含 week（第几周）和 task（该周任务）
- competitor_ref: 参考竞品的做法说明
- expected_outcome: 预期效果
- audit_evidence: 数组，列出本方案对应的审计证据，必须包含平台/prompt/发现
- acceptance_criteria: 数组，列出可验收标准，必须能用下一次审计结果验证
- measurement_plan: 复测方案，说明要观察哪些AI平台、哪些prompt类型、哪些指标

只返回JSON对象，不要其他文字。"""

_PASS2_USER = """优化建议：{title}
建议详情：{description}
目标AI平台：{target_platforms}
证据引用来源网站：{evidence_sources}
证据来源渠道：{evidence_channels}
行动落点网站：{action_sources}
执行渠道：{action_channels}
动作类型：{action_type}

审计背景：
项目信息：{project_info}
各平台评分：{platform_scores}
竞品对比：{competitor_info}
{analysis_context}
{evidence_context}

该建议的审计发现：{audit_findings}
建议依据：{evidence_summary}
成功指标：{success_metric}

请扩展为详细的行动方案。方案必须与审计证据逐条呼应，不要输出通用营销建议；证据来源和行动建议必须到引用来源网站/域名层级，不能停留在"知乎、官网博客、小红书"这类泛渠道。"""

MAX_EVIDENCE_ITEMS = 12

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def generate_suggestions(db: AsyncSession, report: Report) -> list[Suggestion]:
    """Two-pass suggestion generation: strategy then detail."""
    project_id = report.project_id

    # Gather context — load brands from the audit snapshot
    audit = await db.get(Audit, report.audit_id)
    brands = [
        BrandData(
            id=b.get("id", ""),
            name=b.get("name", ""),
            aliases=b.get("aliases", []),
            is_competitor=b.get("is_competitor", False),
        )
        for b in ((audit.brands_json or []) if audit else [])
    ]
    brand_names = [b.name for b in brands if not b.is_competitor]
    competitors = [b.name for b in brands if b.is_competitor]

    project_info = f"品牌: {', '.join(brand_names)}, 行业: 通用"
    platform_scores = json.dumps(report.platform_scores, ensure_ascii=False)
    competitor_info = f"竞品: {', '.join(competitors)}" if competitors else "无竞品数据"
    weak_platforms = _get_weak_platforms(report.platform_scores)

    # Enrich with response analysis data
    analysis_context = await _get_analysis_context(db, report.audit_id)
    analysis_block = f"\n深度分析数据：\n{analysis_context}" if analysis_context else ""
    evidence_context = await _get_evidence_context(db, report.audit_id, brands)
    evidence_block = f"\n审计证据样本：\n{evidence_context}" if evidence_context else ""

    # --- Pass 1: generate strategic suggestions ---
    pass1_prompt = _PASS1_USER.format(
        project_info=project_info,
        overall_score=report.overall_score,
        mention_rate=report.mention_rate,
        competitor_rank=f"第{report.competitor_rank}名" if report.competitor_rank else "未知",
        platform_scores=platform_scores,
        insights="; ".join(report.insights or []),
        competitor_info=competitor_info,
        weak_platforms=weak_platforms,
        analysis_context=analysis_block,
        evidence_context=evidence_block,
    )

    fallback_mode = False
    stubs = await _call_llm(pass1_prompt, system=_PASS1_SYSTEM)
    if not stubs:
        stubs = _build_fallback_suggestion_stubs(
            report=report,
            competitors=competitors,
        )
        if not stubs:
            return []
        logger.info("Using deterministic fallback suggestions for report %s", report.id)
        fallback_mode = True

    use_stub_details_only = fallback_mode or not _has_llm_config()
    if not isinstance(stubs, list):
        logger.warning("Pass 1 returned non-list suggestions: %s", type(stubs))
        return []

    # --- Pass 2: expand each suggestion into detail ---
    logger.info(f"Pass 1 generated {len(stubs)} stubs, expanding each...")
    suggestions: list[Suggestion] = []
    for stub in stubs:
        if not isinstance(stub, dict):
            logger.warning("Skipping non-dict suggestion stub: %s", type(stub))
            continue
        detail = None
        if not use_stub_details_only:
            detail = await _expand_detail(
                stub=stub,
                project_info=project_info,
                platform_scores=platform_scores,
                competitor_info=competitor_info,
                analysis_context=analysis_context,
                evidence_context=evidence_context,
            )
        logger.info(f"Pass 2 for '{stub.get('title', '?')[:20]}': detail={'found' if detail else 'None'}")

        s = Suggestion(
            project_id=project_id,
            report_id=report.id,
            category=stub.get("category", "content_optimization"),
            title=stub.get("title", ""),
            description=stub.get("description", ""),
            priority=stub.get("priority", "medium"),
            detail=_merge_detail_with_stub(detail, stub),
        )
        db.add(s)
        suggestions.append(s)

    if suggestions:
        await db.commit()
        for s in suggestions:
            await db.refresh(s)

    return suggestions


def _has_llm_config() -> bool:
    api_key, _, _ = settings.get_llm_config()
    return bool(api_key)


def _build_fallback_suggestion_stubs(
    *,
    report: Report,
    competitors: list[str],
) -> list[dict]:
    """Build deterministic suggestions when no LLM is available."""
    platform_scores = {
        platform: score
        for platform, score in (report.platform_scores or {}).items()
        if isinstance(score, (int, float))
    }
    ranked_platforms = sorted(platform_scores.items(), key=lambda item: item[1])

    stubs: list[dict] = []
    for platform, score in ranked_platforms[:2]:
        label = _platform_label(platform)
        rounded_score = int(round(score))
        stubs.append(
            {
                "category": "platform_focus",
                "title": f"补强 {label} 的低分场景",
                "description": f"{label} 当前得分 {rounded_score}，优先补齐高频问题、对比页和 FAQ。",
                "priority": "high" if rounded_score < 40 else "medium",
                "target_platforms": [platform],
                "evidence_channels": [label],
                "action_channels": ["官网", "FAQ"],
                "action_type": "发布 FAQ / 对比页",
                "audit_findings": [f"{label} 当前得分 {rounded_score}，存在可见性缺口"],
                "evidence_summary": f"基于 {label} 审计结果的兜底建议",
                "success_metric": f"{label} 的品牌提及率提升",
                "outline": [
                    "回答高频问题",
                    "补充可引用的事实和数据",
                    "加入竞品对比和结构化数据",
                ],
                "keywords": [label, "FAQ", "对比页"],
                "weekly_tasks": [
                    {"week": "Week 1", "task": f"整理 {label} 的低分场景和高频问题"},
                    {"week": "Week 2", "task": "发布 FAQ / 对比页并补充结构化数据"},
                ],
                "competitor_ref": f"重点对比：{', '.join(competitors)}" if competitors else "",
                "expected_outcome": f"{label} 的推荐位和提及率提升",
                "measurement_plan": f"复测 {label} 的相关 prompt 命中率和推荐率",
                "acceptance_criteria": [
                    "完成一篇可直接引用的内容",
                    "页面可被 AI 平台检索和引用",
                    "下次审计能看到提及率提升",
                ],
            }
        )

    if report.competitor_rank and report.competitor_rank > 1:
        stubs.append(
            {
                "category": "competitive_strategy",
                "title": "补一页竞品对比内容",
                "description": f"当前竞品排名第 {report.competitor_rank}，建议增加官网对比页和案例页，抢回推荐位。",
                "priority": "high",
                "target_platforms": list(platform_scores.keys())[:2],
                "evidence_channels": ["官网", "对比页"],
                "action_channels": ["官网", "FAQ"],
                "action_type": "创建竞品对比页",
                "audit_findings": [f"当前竞品排名第 {report.competitor_rank}"],
                "evidence_summary": "基于当前审计排名的兜底建议",
                "success_metric": "竞品排名下降，品牌推荐位前移",
                "outline": [
                    "对比核心能力",
                    "列出常见问题答案",
                    "补充案例和数据证明",
                ],
                "keywords": ["竞品对比", "案例", "FAQ"],
                "weekly_tasks": [
                    {"week": "Week 1", "task": "整理竞品差异和可引用证据"},
                    {"week": "Week 2", "task": "上线对比页并补充案例"},
                ],
                "competitor_ref": f"参考 {', '.join(competitors)}" if competitors else "",
                "expected_outcome": "AI 平台更容易推荐本品牌对比页",
                "measurement_plan": "复测对比类 prompt 的提及率和推荐率",
                "acceptance_criteria": [
                    "有一页明确的竞品对比内容",
                    "页面包含可引用的事实和数据",
                    "下次审计能验证效果",
                ],
            }
        )

    if report.insights:
        insight = str(report.insights[0]).strip()
        if insight:
            stubs.append(
                {
                    "category": "content_optimization",
                    "title": "把当前洞察落成一篇内容",
                    "description": insight,
                    "priority": "medium",
                    "target_platforms": list(platform_scores.keys())[:1],
                    "evidence_channels": ["官网", "博客"],
                    "action_channels": ["官网", "博客"],
                    "action_type": "发布洞察复用内容",
                    "audit_findings": [insight],
                    "evidence_summary": "来自现有审计洞察的兜底建议",
                    "success_metric": "洞察相关 prompt 的品牌提及率提升",
                    "outline": [
                        "把洞察拆成用户问题",
                        "补充可引用来源",
                        "加入结构化标题和 FAQ",
                    ],
                    "keywords": ["洞察", "内容复用", "FAQ"],
                    "weekly_tasks": [
                        {"week": "Week 1", "task": "把洞察拆成可执行内容提纲"},
                        {"week": "Week 2", "task": "完成内容发布和内链补充"},
                    ],
                    "expected_outcome": "让当前洞察更容易被 AI 平台引用",
                    "measurement_plan": "复测洞察相关 prompt 的提及变化",
                    "acceptance_criteria": [
                        "内容能直接回答高频问题",
                        "包含可引用的事实和来源",
                        "发布后可进入下一次审计验证",
                    ],
                }
            )

    if not stubs:
        stubs.append(
            {
                "category": "content_optimization",
                "title": "补一篇可被 AI 引用的 FAQ",
                "description": "当前没有可直接复用的 LLM 建议时，先补一篇 FAQ / 对比页，避免页面为空。",
                "priority": "medium",
                "target_platforms": list(platform_scores.keys())[:1],
                "evidence_channels": ["官网"],
                "action_channels": ["官网", "FAQ"],
                "action_type": "发布 FAQ / 对比页",
                "audit_findings": ["当前建议服务未拿到 LLM 输出"],
                "evidence_summary": "兜底建议，确保页面有内容可看",
                "success_metric": "页面不再为空，后续可继续优化",
                "outline": ["回答高频问题", "补充案例", "加入引用来源"],
                "keywords": ["FAQ", "对比页"],
                "weekly_tasks": [
                    {"week": "Week 1", "task": "整理高频问题"},
                    {"week": "Week 2", "task": "发布 FAQ 页面"},
                ],
                "expected_outcome": "至少有一条可执行建议",
                "measurement_plan": "下次审计时检查是否已有内容可引用",
                "acceptance_criteria": [
                    "生成页面至少一条建议",
                    "建议包含明确动作",
                    "后续可继续按 LLM 建议升级",
                ],
            }
        )

    return stubs[:3]


def _platform_label(platform: str) -> str:
    return {
        "deepseek": "DeepSeek",
        "qwen": "Qwen",
        "doubao": "Doubao",
        "kimi": "Kimi",
        "hunyuan": "Hunyuan",
        "wenxin": "Wenxin",
    }.get(platform, platform[:1].upper() + platform[1:])


# ---------------------------------------------------------------------------
# Pass 2 expansion
# ---------------------------------------------------------------------------

async def _expand_detail(
    stub: dict,
    project_info: str,
    platform_scores: str,
    competitor_info: str,
    analysis_context: str,
    evidence_context: str,
) -> dict | None:
    """Call LLM to expand a single suggestion into a detailed action plan."""
    tp = stub.get("target_platforms") or []
    if not isinstance(tp, list):
        tp = [str(tp)]
    prompt = _PASS2_USER.format(
        title=stub.get("title", ""),
        description=stub.get("description", ""),
        target_platforms=", ".join(tp),
        evidence_sources=", ".join(_as_string_list(stub.get("evidence_sources"))),
        evidence_channels=", ".join(_as_string_list(stub.get("evidence_channels")) or _as_string_list(stub.get("action_channels"))),
        action_sources=", ".join(_as_string_list(stub.get("action_sources"))),
        action_channels=", ".join(_as_string_list(stub.get("action_channels"))),
        action_type=stub.get("action_type", ""),
        project_info=project_info,
        platform_scores=platform_scores,
        competitor_info=competitor_info,
        analysis_context=analysis_context,
        evidence_context=evidence_context,
        audit_findings=json.dumps(stub.get("audit_findings") or [], ensure_ascii=False),
        evidence_summary=stub.get("evidence_summary", ""),
        success_metric=stub.get("success_metric", ""),
    )
    logger.info(f"Pass 2 calling LLM for '{stub.get('title', '?')[:30]}'...")
    try:
        result = await _call_llm(prompt, system=_PASS2_SYSTEM, expect_array=False)
        if isinstance(result, dict):
            logger.info(f"Pass 2 SUCCESS for '{stub.get('title', '?')[:30]}'")
            return result
        logger.warning(f"Pass 2 returned non-dict for '{stub.get('title')}': {type(result)}")
        return None
    except Exception as e:
        logger.warning(f"Pass 2 expansion failed for suggestion '{stub.get('title')}': {e}")
        return None


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------

async def _get_analysis_context(db: AsyncSession, audit_id: int) -> str:
    """Build analysis context string from ResponseAnalysis data."""
    result = await db.execute(
        select(ResponseAnalysis)
        .join(PlatformResponseRecord, ResponseAnalysis.response_record_id == PlatformResponseRecord.id)
        .where(PlatformResponseRecord.audit_id == audit_id)
        .where(ResponseAnalysis.status == "completed")
    )
    analyses = result.scalars().all()
    if not analyses:
        return ""

    from collections import Counter
    sentiment_counts = Counter(a.brand_sentiment for a in analyses if a.brand_sentiment)
    all_topics = [t for a in analyses for t in (a.topics_covered or [])]
    all_competitors = set(c for a in analyses for c in (a.competitor_refs or []))
    all_attributes = [attr for a in analyses for attr in (a.brand_attributes or [])]
    attr_counts = Counter(all_attributes).most_common(5)

    parts = []
    if sentiment_counts:
        parts.append(f"品牌情感分布: {dict(sentiment_counts)}")
    if all_topics:
        topic_counts = Counter(all_topics).most_common(10)
        parts.append(f"常见话题: {', '.join(t for t, _ in topic_counts)}")
    if all_competitors:
        parts.append(f"竞品被提及: {', '.join(all_competitors)}")
    if attr_counts:
        parts.append(f"品牌属性标签: {', '.join(f'{a}({c})' for a, c in attr_counts)}")

    return "\n".join(parts)


async def _get_evidence_context(
    db: AsyncSession,
    audit_id: int,
    brands: list[BrandData],
) -> str:
    """Build concrete audit evidence so suggestions can cite actual gaps."""
    primary_ids = {b.id for b in brands if not b.is_competitor}
    brand_names = {b.id: b.name for b in brands}
    competitor_ids = {b.id for b in brands if b.is_competitor}
    if not primary_ids:
        return ""

    result = await db.execute(
        select(
            QueryResult,
            Prompt.text,
            Prompt.category,
            PlatformResponseRecord.citations,
            ResponseAnalysis.cited_sources,
        )
        .join(Prompt, QueryResult.prompt_id == Prompt.id)
        .outerjoin(
            PlatformResponseRecord,
            QueryResult.response_record_id == PlatformResponseRecord.id,
        )
        .outerjoin(
            ResponseAnalysis,
            ResponseAnalysis.response_record_id == PlatformResponseRecord.id,
        )
        .where(QueryResult.audit_id == audit_id)
    )
    rows = result.all()
    if not rows:
        return ""

    source_result = await db.execute(
        select(SourceCitation).where(SourceCitation.audit_id == audit_id)
    )
    source_refs_by_platform: dict[str, list[dict]] = defaultdict(list)
    for source in source_result.scalars().all():
        source_refs_by_platform[source.platform].append(
            {
                "domain": source.domain,
                "urls": source.urls or [],
                "citation_count": source.citation_count,
            }
        )

    by_key: dict[tuple[str, int], list[tuple[QueryResult, str, object, list[dict]]]] = defaultdict(list)
    primary_results: list[tuple[QueryResult, str, object, list[dict]]] = []
    for query_result, prompt_text, category, citations, cited_sources in rows:
        source_refs = _source_refs_for_sample(
            citations=citations or [],
            cited_sources=cited_sources or [],
            platform_sources=source_refs_by_platform.get(query_result.platform, []),
        )
        by_key[(query_result.platform, query_result.prompt_id)].append(
            (query_result, prompt_text, category, source_refs)
        )
        if query_result.brand_id in primary_ids:
            primary_results.append((query_result, prompt_text, category, source_refs))

    platform_stats: dict[str, dict[str, float]] = {}
    for platform in sorted({r.platform for r, _, _, _ in primary_results}):
        platform_rows = [(r, p, c) for r, p, c, _ in primary_results if r.platform == platform]
        total = len(platform_rows)
        mentions = sum(1 for r, _, _ in platform_rows if r.mention_found)
        recommended = sum(1 for r, _, _ in platform_rows if r.is_recommended)
        ranks = [r.recommendation_rank for r, _, _ in platform_rows if r.recommendation_rank]
        platform_stats[platform] = {
            "total": total,
            "mention_rate": mentions / total if total else 0,
            "recommend_rate": recommended / total if total else 0,
            "avg_rank": sum(ranks) / len(ranks) if ranks else 0,
        }

    gap_samples: list[str] = []
    competitor_samples: list[str] = []
    positive_samples: list[str] = []
    category_counter: Counter[str] = Counter()

    for r, prompt_text, category, source_refs in primary_results:
        category_value = getattr(category, "value", str(category))
        category_counter[category_value] += 1
        context = _clip(r.mention_context or r.response_text or "", 90)
        base = f"[{r.platform}] prompt=\"{_clip(prompt_text, 70)}\""
        source_suffix = _format_source_refs(source_refs)

        peer_rows = by_key.get((r.platform, r.prompt_id), [])
        competitor_hits = [
            brand_names.get(peer.brand_id, peer.brand_id)
            for peer, _, _, _ in peer_rows
            if peer.brand_id in competitor_ids and (peer.mention_found or peer.is_recommended)
        ]

        if not r.mention_found and len(gap_samples) < MAX_EVIDENCE_ITEMS:
            suffix = f"；竞品出现：{', '.join(competitor_hits)}" if competitor_hits else ""
            gap_samples.append(f"{base}{source_suffix} -> 本品牌未提及{suffix}")
        elif r.mention_found and not r.is_recommended and len(gap_samples) < MAX_EVIDENCE_ITEMS:
            gap_samples.append(f"{base}{source_suffix} -> 本品牌有提及但未进入推荐；片段：{context}")
        elif r.is_recommended and len(positive_samples) < 5:
            rank = f"推荐第{r.recommendation_rank}位" if r.recommendation_rank else "被推荐"
            positive_samples.append(f"{base}{source_suffix} -> {rank}；片段：{context}")

        if competitor_hits and (not r.mention_found or not r.is_recommended):
            if len(competitor_samples) < MAX_EVIDENCE_ITEMS:
                own_state = "未提及" if not r.mention_found else "未推荐"
                competitor_samples.append(
                    f"{base}{source_suffix} -> 本品牌{own_state}，竞品出现：{', '.join(competitor_hits)}"
                )

    lines = ["平台表现："]
    for platform, stats in sorted(platform_stats.items(), key=lambda item: item[1]["mention_rate"]):
        avg_rank = f"，平均推荐名次 {stats['avg_rank']:.1f}" if stats["avg_rank"] else ""
        lines.append(
            f"- {platform}: 提及率 {stats['mention_rate']:.0%}，"
            f"推荐率 {stats['recommend_rate']:.0%}{avg_rank}，样本数 {int(stats['total'])}"
        )

    if category_counter:
        lines.append(
            "Prompt类型分布：" + ", ".join(
                f"{category}({count})" for category, count in category_counter.most_common()
            )
        )
    if gap_samples:
        lines.append("缺口样本：")
        lines.extend(f"- {sample}" for sample in gap_samples)
    if competitor_samples:
        lines.append("竞品压制样本：")
        lines.extend(f"- {sample}" for sample in competitor_samples)
    if positive_samples:
        lines.append("可复用正向样本：")
        lines.extend(f"- {sample}" for sample in positive_samples)

    return "\n".join(lines)


def _source_refs_for_sample(
    *,
    citations: list[dict],
    cited_sources: list[dict],
    platform_sources: list[dict],
) -> list[dict]:
    """Return source refs for a prompt sample, preferring response-level citations."""
    refs: list[dict] = []
    refs.extend(citations or [])
    refs.extend(cited_sources or [])
    if not refs:
        refs.extend(platform_sources or [])

    seen: set[str] = set()
    normalized: list[dict] = []
    for source in clean_cited_sources(refs):
        domain = source.get("domain")
        urls = source.get("urls") or []
        if source.get("url"):
            urls = [source["url"], *urls]
        url = str(urls[0]) if urls else ""
        key = f"{domain}|{url}"
        if key in seen:
            continue
        seen.add(key)
        normalized.append(
            {
                "domain": domain,
                "url": url,
                "title": source.get("title") or "",
                "citation_count": source.get("citation_count"),
            }
        )
        if len(normalized) >= 4:
            break
    return normalized


def _format_source_refs(source_refs: list[dict]) -> str:
    """Format concrete cited domains/URLs for the suggestion prompt."""
    if not source_refs:
        return ""

    items = []
    for source in source_refs:
        domain = source.get("domain")
        if not domain:
            continue
        url = source.get("url")
        title = source.get("title")
        label = str(domain)
        if url:
            label = f"{label}<{_clip(str(url), 80)}>"
        if title:
            label = f"{label}《{_clip(str(title), 30)}》"
        items.append(label)

    return f"；引用来源：{', '.join(items)}" if items else ""


def _merge_detail_with_stub(detail: dict | None, stub: dict) -> dict:
    """Keep pass-1 evidence fields even if pass 2 omits them."""
    merged = dict(detail or {})
    for key in (
        "target_platforms",
        "evidence_sources",
        "evidence_channels",
        "action_sources",
        "action_channels",
        "action_type",
        "audit_findings",
        "evidence_summary",
        "success_metric",
        "outline",
        "keywords",
        "weekly_tasks",
        "competitor_ref",
        "expected_outcome",
        "measurement_plan",
        "acceptance_criteria",
    ):
        if key not in merged and key in stub:
            merged[key] = stub[key]
    return merged


def _as_string_list(value: object) -> list[str]:
    """Normalize a value into a list of non-empty strings."""
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _clip(text: str, max_chars: int) -> str:
    """Trim long prompt/answer snippets for compact LLM context."""
    text = " ".join(str(text).split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def _get_weak_platforms(platform_scores: dict) -> str:
    """Identify weak platforms from scores."""
    if not platform_scores:
        return "无平台数据"
    sorted_p = sorted(platform_scores.items(), key=lambda x: x[1])
    weak = [f"{p}({s}分)" for p, s in sorted_p[:3]]
    return ", ".join(weak)


# ---------------------------------------------------------------------------
# LLM call helper
# ---------------------------------------------------------------------------

async def _call_llm(prompt: str, *, system: str, expect_array: bool = True) -> list[dict] | dict | None:
    """Call the configured LLM and parse JSON response."""
    api_key, base_url, model = settings.get_llm_config()
    if not api_key:
        logger.warning("No LLM API key configured, returning empty suggestions")
        return [] if expect_array else None

    try:
        async with httpx.AsyncClient(timeout=60, proxy=None) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.7,
                },
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]

            # Extract JSON from response (may have markdown code fences)
            text = text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()

            parsed = json.loads(text)
            return parsed
    except Exception as e:
        logger.error(f"LLM call failed for suggestions: {e}")
        return [] if expect_array else None
