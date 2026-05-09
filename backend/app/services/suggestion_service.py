"""AI-powered optimization suggestion service.

Analyzes report data and uses LLM to generate actionable suggestions
for improving brand visibility across AI platforms.
"""

import json
import logging

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.models import Brand, QueryResult, Report, Suggestion

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """你是一个AI搜索优化专家。根据品牌在各AI平台的可见性审计报告，生成具体可操作的优化建议。

请以JSON数组格式返回建议，每条建议包含：
- category: 分类（content_optimization/seo_strategy/platform_focus/competitive_strategy）
- title: 建议标题（简短，20字内）
- description: 详细说明（含具体行动步骤，100字内）
- priority: 优先级（high/medium/low）

只返回JSON数组，不要其他文字。"""

_USER_PROMPT_TEMPLATE = """请分析以下品牌AI可见性报告并给出优化建议：

项目信息：{project_info}
综合评分：{overall_score}/100
品牌提及率：{mention_rate:.1%}
竞品排名：{competitor_rank}
各平台评分：{platform_scores}
已有洞察：{insights}

竞品对比：{competitor_info}
低分平台详情：{weak_platforms}

请生成5-8条优化建议，覆盖内容优化、SEO策略、平台重点、竞品策略等方面。"""


async def generate_suggestions(db: AsyncSession, report: Report) -> list[Suggestion]:
    """Generate AI-powered suggestions based on a report."""
    project_id = report.project_id

    # Gather context
    brands_result = await db.execute(
        select(Brand).where(Brand.project_id == project_id)
    )
    brands = brands_result.scalars().all()
    brand_names = [b.name for b in brands if not b.is_competitor]
    competitors = [b.name for b in brands if b.is_competitor]

    project_info = f"品牌: {', '.join(brand_names)}, 行业: 通用"
    platform_scores = json.dumps(report.platform_scores, ensure_ascii=False)
    competitor_info = f"竞品: {', '.join(competitors)}" if competitors else "无竞品数据"

    # Get weak platform details
    weak_platforms = _get_weak_platforms(report.platform_scores)

    user_prompt = _USER_PROMPT_TEMPLATE.format(
        project_info=project_info,
        overall_score=report.overall_score,
        mention_rate=report.mention_rate,
        competitor_rank=f"第{report.competitor_rank}名" if report.competitor_rank else "未知",
        platform_scores=platform_scores,
        insights="; ".join(report.insights or []),
        competitor_info=competitor_info,
        weak_platforms=weak_platforms,
    )

    # Call LLM
    suggestions_json = await _call_llm(user_prompt)

    # Parse and store
    suggestions = []
    for item in suggestions_json:
        s = Suggestion(
            project_id=project_id,
            report_id=report.id,
            category=item.get("category", "content_optimization"),
            title=item.get("title", ""),
            description=item.get("description", ""),
            priority=item.get("priority", "medium"),
        )
        db.add(s)
        suggestions.append(s)

    if suggestions:
        await db.commit()
        for s in suggestions:
            await db.refresh(s)

    return suggestions


def _get_weak_platforms(platform_scores: dict) -> str:
    """Identify weak platforms from scores."""
    if not platform_scores:
        return "无平台数据"
    sorted_p = sorted(platform_scores.items(), key=lambda x: x[1])
    weak = [f"{p}({s}分)" for p, s in sorted_p[:3]]
    return ", ".join(weak)


async def _call_llm(prompt: str) -> list[dict]:
    """Call the configured LLM and parse JSON response."""
    api_key, base_url, model = settings.get_llm_config()
    if not api_key:
        logger.warning("No LLM API key configured, returning empty suggestions")
        return []

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
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

            return json.loads(text)
    except Exception as e:
        logger.error(f"LLM call failed for suggestions: {e}")
        return []
