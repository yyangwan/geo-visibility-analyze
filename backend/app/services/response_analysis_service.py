"""Response analysis service — LLM-powered deep analysis of AI platform responses.

Runs after audit completes. Analyzes each PlatformResponseRecord to extract:
brand positioning, sentiment, topics, answer structure, competitor references,
and cited source authority.
"""

import asyncio
import json
from collections import Counter

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.logging_config import get_logger
from app.models.models import (
    Audit,
    Brand,
    PlatformResponseRecord,
    Prompt,
    ResponseAnalysis,
)

logger = get_logger("analysis")

_SYSTEM_PROMPT = """你是一个AI搜索结果分析专家。分析AI平台给出的搜索回答，提取以下结构化信息。

请以JSON格式返回，包含：
- brand_sentiment: 品牌情感倾向（positive/neutral/negative）
- brand_attributes: 品牌属性标签列表（如"性价比高"、"服务好"、"理赔快"等，最多5个）
- topics_covered: 涵盖的主题列表（如"产品特点"、"价格对比"、"用户体验"等）
- answer_structure: 回答结构类型（list/comparison/narrative/qa）
- competitor_refs: 提到的竞品品牌名称列表
- cited_sources: 引用来源列表 [{"domain": "xxx.com", "authority_score": 3}]

authority_score 为1-5整数，5表示高度权威（政府/主流媒体），1表示低权威（个人博客/论坛）。
只返回JSON，不要其他文字。"""

_USER_PROMPT_TEMPLATE = """请分析以下AI搜索平台的回答：

目标品牌：{brands}
竞品品牌：{competitors}

AI平台回答内容：
{response_text}"""

MAX_RESPONSE_CHARS = 4000
MAX_CONCURRENT_ANALYSIS = 3


async def run_analysis_for_audit(audit_id: int) -> None:
    """Background task: analyze all PRRs in an audit."""
    async with async_session() as db:
        audit = await db.get(Audit, audit_id)
        if not audit:
            return

        if audit.status.value not in ("completed", "partial"):
            logger.info("analysis_skipped", audit_id=audit_id, status=audit.status.value)
            return

        # Load PRRs
        result = await db.execute(
            select(PlatformResponseRecord)
            .where(PlatformResponseRecord.audit_id == audit_id)
            .where(PlatformResponseRecord.error.is_(None))
        )
        prrs = result.scalars().all()
        if not prrs:
            logger.info("analysis_no_prrs", audit_id=audit_id)
            return

        # Load brands
        brand_result = await db.execute(
            select(Brand).where(Brand.project_id == audit.project_id)
        )
        brands = brand_result.scalars().all()
        brand_names = [b.name for b in brands if not b.is_competitor]
        competitor_names = [b.name for b in brands if b.is_competitor]

        # Create pending ResponseAnalysis rows for PRRs without one
        for prr in prrs:
            existing = await db.execute(
                select(ResponseAnalysis).where(
                    ResponseAnalysis.response_record_id == prr.id
                )
            )
            if not existing.scalar_one_or_none():
                db.add(ResponseAnalysis(
                    response_record_id=prr.id,
                    status="pending",
                ))
        await db.commit()

        # Load pending/failed analyses
        result = await db.execute(
            select(ResponseAnalysis)
            .join(PlatformResponseRecord, ResponseAnalysis.response_record_id == PlatformResponseRecord.id)
            .where(PlatformResponseRecord.audit_id == audit_id)
            .where(ResponseAnalysis.status.in_(["pending", "failed"]))
        )
        pending_analyses = result.scalars().all()

        logger.info(
            "analysis_started",
            audit_id=audit_id,
            total_prrs=len(prrs),
            pending=len(pending_analyses),
        )

        # Run analyses with concurrency limit (each gets its own DB session)
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_ANALYSIS)

        async def _analyze_with_semaphore(ra: ResponseAnalysis):
            async with semaphore:
                async with async_session() as task_db:
                    await _analyze_single(task_db, ra, brand_names, competitor_names)

        tasks = [_analyze_with_semaphore(ra) for ra in pending_analyses]
        await asyncio.gather(*tasks, return_exceptions=True)

        logger.info("analysis_completed", audit_id=audit_id)


async def retry_failed_analyses(audit_id: int) -> int:
    """Retry all failed analyses for an audit. Returns count of retried records."""
    async with async_session() as db:
        result = await db.execute(
            select(ResponseAnalysis)
            .join(PlatformResponseRecord, ResponseAnalysis.response_record_id == PlatformResponseRecord.id)
            .where(PlatformResponseRecord.audit_id == audit_id)
            .where(ResponseAnalysis.status == "failed")
        )
        failed = result.scalars().all()

        if not failed:
            return 0

        # Load brands
        audit = await db.get(Audit, audit_id)
        brand_result = await db.execute(
            select(Brand).where(Brand.project_id == audit.project_id)
        )
        brands = brand_result.scalars().all()
        brand_names = [b.name for b in brands if not b.is_competitor]
        competitor_names = [b.name for b in brands if b.is_competitor]

        for ra in failed:
            ra.status = "pending"
        await db.commit()

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_ANALYSIS)

        async def _analyze_with_semaphore(ra: ResponseAnalysis):
            async with semaphore:
                async with async_session() as task_db:
                    await _analyze_single(task_db, ra, brand_names, competitor_names)

        tasks = [_analyze_with_semaphore(ra) for ra in failed]
        await asyncio.gather(*tasks, return_exceptions=True)

        return len(failed)


async def _analyze_single(
    db: AsyncSession,
    ra: ResponseAnalysis,
    brand_names: list[str],
    competitor_names: list[str],
) -> None:
    """Analyze a single ResponseAnalysis record."""
    # Re-fetch within this session to avoid cross-session object issues
    ra = await db.get(ResponseAnalysis, ra.id)
    if not ra:
        return

    ra.status = "running"
    await db.commit()

    try:
        # Load PRR with response text
        prr = await db.get(PlatformResponseRecord, ra.response_record_id)
        if not prr or not prr.response_text:
            ra.status = "failed"
            await db.commit()
            return

        response_text = prr.response_text[:MAX_RESPONSE_CHARS]
        user_prompt = _USER_PROMPT_TEMPLATE.format(
            brands="、".join(brand_names),
            competitors="、".join(competitor_names) if competitor_names else "无",
            response_text=response_text,
        )

        result = await _call_llm_for_analysis(
            _SYSTEM_PROMPT,
            user_prompt,
            settings.analysis_timeout_seconds,
        )

        if result is None:
            ra.status = "failed"
            await db.commit()
            return

        api_key, _, model = settings.get_llm_config()
        ra.brand_sentiment = result.get("brand_sentiment")
        ra.brand_attributes = result.get("brand_attributes", [])
        ra.topics_covered = result.get("topics_covered", [])
        ra.answer_structure = result.get("answer_structure")
        ra.competitor_refs = result.get("competitor_refs", [])
        ra.cited_sources = result.get("cited_sources", [])
        ra.analysis_model = model
        ra.status = "completed"
        await db.commit()

        logger.info(
            "analysis_record_done",
            ra_id=ra.id,
            sentiment=ra.brand_sentiment,
            topics=len(ra.topics_covered),
        )

    except Exception as e:
        ra.status = "failed"
        await db.commit()
        logger.error("analysis_record_failed", ra_id=ra.id, error=str(e))


async def _call_llm_for_analysis(
    system_prompt: str,
    user_prompt: str,
    timeout: int,
) -> dict | None:
    """Call LLM and parse structured JSON response for analysis."""
    api_key, base_url, model = settings.get_llm_config()
    if not api_key:
        logger.warning("analysis_no_api_key")
        return None

    try:
        async with httpx.AsyncClient(timeout=timeout, proxy=None) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.3,
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
        logger.error("analysis_llm_failed", error=str(e))
        return None
