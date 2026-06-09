"""AI-powered optimization suggestion service.

Two-pass LLM generation:
  Pass 1 — Generate 5-8 strategic suggestions from audit data.
  Pass 2 — Expand each suggestion into a detailed action plan
           (channel, outline, timeline, keywords, competitor reference).
"""

import asyncio
import json
import logging

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.models import (
    Audit,
    PlatformResponseRecord,
    QueryResult,
    Report,
    ResponseAnalysis,
    Suggestion,
)
from app.services.audit_service import BrandData

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pass 1 — strategic suggestions
# ---------------------------------------------------------------------------

_PASS1_SYSTEM = """你是一个AI搜索优化专家。根据品牌在各AI平台的可见性审计报告，生成具体可操作的优化建议。

每条建议必须：
1. 明确指出在哪个渠道/平台执行（如小红书、知乎、官网博客、微信公众号、B站、抖音）
2. 指出针对哪个AI平台的优化目标（如Kimi、DeepSeek、通义千问）
3. 给出具体动作（如"发布产品对比评测文章"、"优化官网FAQ页面添加结构化数据"）

请以JSON数组格式返回建议，每条建议包含：
- category: 分类（content_optimization/seo_strategy/platform_focus/competitive_strategy）
- title: 建议标题（简短，20字内）
- description: 详细说明（含具体行动步骤，200字内，必须包含执行渠道和目标AI平台）
- priority: 优先级（high/medium/low）
- target_platforms: 目标AI平台列表，如["kimi","deepseek"]
- action_channel: 执行渠道，如"小红书"、"官网博客"、"知乎"

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

请生成5-8条优化建议。每条建议必须明确：在哪个渠道做什么、优化哪个AI平台、使用什么关键词。"""

# ---------------------------------------------------------------------------
# Pass 2 — detailed action plan
# ---------------------------------------------------------------------------

_PASS2_SYSTEM = """你是一个AI搜索优化执行顾问。用户会给一条优化建议和相关的审计数据，请将其扩展为详细可执行的行动方案。

请以JSON对象格式返回（不要数组），包含：
- action_channel: 执行渠道（如小红书、知乎、官网博客）
- action_type: 动作类型（如发布评测文章、优化FAQ页面、增加Schema标记、创建对比页面）
- outline: 内容大纲，3-5个要点组成的数组
- keywords: 建议使用的关键词列表，5-8个
- timeline: 执行时间线，数组，每项包含 week（第几周）和 task（该周任务）
- competitor_ref: 参考竞品的做法说明
- expected_outcome: 预期效果

只返回JSON对象，不要其他文字。"""

_PASS2_USER = """优化建议：{title}
建议详情：{description}
目标AI平台：{target_platforms}
执行渠道：{action_channel}

审计背景：
项目信息：{project_info}
各平台评分：{platform_scores}
竞品对比：{competitor_info}
{analysis_context}

请扩展为详细的行动方案。"""

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
    )

    stubs = await _call_llm(pass1_prompt, system=_PASS1_SYSTEM)
    if not stubs:
        return []

    # --- Pass 2: expand each suggestion into detail ---
    logger.info(f"Pass 1 generated {len(stubs)} stubs, expanding each...")
    suggestions: list[Suggestion] = []
    for stub in stubs:
        detail = await _expand_detail(
            stub=stub,
            project_info=project_info,
            platform_scores=platform_scores,
            competitor_info=competitor_info,
            analysis_context=analysis_context,
        )
        logger.info(f"Pass 2 for '{stub.get('title', '?')[:20]}': detail={'found' if detail else 'None'}")

        s = Suggestion(
            project_id=project_id,
            report_id=report.id,
            category=stub.get("category", "content_optimization"),
            title=stub.get("title", ""),
            description=stub.get("description", ""),
            priority=stub.get("priority", "medium"),
            detail=detail,
        )
        db.add(s)
        suggestions.append(s)

    if suggestions:
        await db.commit()
        for s in suggestions:
            await db.refresh(s)

    return suggestions


# ---------------------------------------------------------------------------
# Pass 2 expansion
# ---------------------------------------------------------------------------

async def _expand_detail(
    stub: dict,
    project_info: str,
    platform_scores: str,
    competitor_info: str,
    analysis_context: str,
) -> dict | None:
    """Call LLM to expand a single suggestion into a detailed action plan."""
    tp = stub.get("target_platforms") or []
    if not isinstance(tp, list):
        tp = [str(tp)]
    prompt = _PASS2_USER.format(
        title=stub.get("title", ""),
        description=stub.get("description", ""),
        target_platforms=", ".join(tp),
        action_channel=stub.get("action_channel", ""),
        project_info=project_info,
        platform_scores=platform_scores,
        competitor_info=competitor_info,
        analysis_context=analysis_context,
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
        return None


# ---------------------------------------------------------------------------
# Context helpers (unchanged)
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
