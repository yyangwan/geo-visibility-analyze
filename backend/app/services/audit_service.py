"""Audit execution service.

Orchestrates the full audit pipeline:
1. Load project, prompts and brands
2. Query each platform via adapters
3. Detect brand mentions in responses
4. Store results and update audit status
"""

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import PlatformResponse
from app.adapters.registry import get_adapters
from app.database import async_session
from app.logging_config import get_logger
from app.models.models import (
    Audit,
    Brand,
    Project,
    Prompt,
    QueryResult,
    QueryStatus,
)
from app.services.audit_events import PlatformEvent, publish
from app.services.detect import detect_mentions

logger = get_logger("audit")


async def run_audit(audit_id: int) -> None:
    """Execute an audit by ID. Designed to be run as a background task."""
    async with async_session() as db:
        audit = await db.get(Audit, audit_id)
        if not audit:
            return

        # Mark as running
        audit.status = QueryStatus.RUNNING
        await db.commit()
        logger.info("audit_started", audit_id=audit_id, project_id=audit.project_id)

        try:
            await _execute_audit(db, audit)
        except Exception as e:
            audit.status = QueryStatus.FAILED
            audit.error_message = str(e)
            audit.completed_at = datetime.now(timezone.utc)
            await db.commit()
            publish(audit_id, PlatformEvent(type="audit_failed", error=str(e)))
            logger.error("audit_failed", audit_id=audit_id, error=str(e))


async def _execute_audit(db: AsyncSession, audit: Audit) -> None:
    """Core audit execution logic."""
    # Load project (for industry context in detection)
    project = await db.get(Project, audit.project_id)
    project_industry = project.industry if project else ""

    # Load prompts for this project
    result = await db.execute(
        select(Prompt).where(Prompt.project_id == audit.project_id)
    )
    prompts = result.scalars().all()
    if not prompts:
        audit.status = QueryStatus.FAILED
        audit.error_message = "No prompts found for this project"
        audit.completed_at = datetime.now(timezone.utc)
        await db.commit()
        return

    # Load brands for this project
    result = await db.execute(
        select(Brand).where(Brand.project_id == audit.project_id)
    )
    brands = result.scalars().all()
    if not brands:
        audit.status = QueryStatus.FAILED
        audit.error_message = "No brands found for this project"
        audit.completed_at = datetime.now(timezone.utc)
        await db.commit()
        return

    prompt_texts = [p.text for p in prompts]
    platforms = audit.platforms_json or []
    adapters = get_adapters(platforms)

    # Query platforms concurrently, publishing progress events as each completes
    all_responses: list[tuple[str, PlatformResponse]] = []

    # Wrap each platform query to carry its name through asyncio.as_completed
    async def _query_and_tag(name: str, adapter, prompts: list[str]):
        publish(audit.id, PlatformEvent(type="platform_start", platform=name))
        try:
            result = await _query_platform(name, adapter, prompts)
            return name, result, None
        except Exception as e:
            return name, None, e

    tasks = [asyncio.create_task(_query_and_tag(a.platform_name, a, prompt_texts)) for a in adapters]

    for coro in asyncio.as_completed(tasks):
        platform_name, responses, exc = await coro
        if exc:
            for pt in prompt_texts:
                all_responses.append((
                    platform_name,
                    PlatformResponse(
                        platform=platform_name,
                        prompt=pt,
                        response_text="",
                        error_code=None,
                        error_message=str(exc),
                    ),
                ))
            publish(audit.id, PlatformEvent(type="platform_error", platform=platform_name, error=str(exc)))
        else:
            for resp in responses:
                all_responses.append((platform_name, resp))
            publish(audit.id, PlatformEvent(type="platform_done", platform=platform_name))

    # Process responses and detect mentions
    results = []
    for platform_name, resp in all_responses:
        prompt_obj = next((p for p in prompts if p.text == resp.prompt), None)
        if not prompt_obj:
            continue

        for brand in brands:
            qr = QueryResult(
                audit_id=audit.id,
                prompt_id=prompt_obj.id,
                brand_id=brand.id,
                platform=platform_name,
                response_text=resp.response_text if resp.success else None,
                error=resp.error_message if not resp.success else None,
            )

            if resp.success and resp.response_text:
                mentions = detect_mentions(
                    resp.response_text,
                    brand.name,
                    brand.aliases,
                    industry=project_industry,
                )
                if mentions:
                    best = max(mentions, key=lambda m: m.confidence)
                    qr.mention_found = True
                    qr.mention_position = best.position
                    qr.mention_context = best.context
                    qr.mention_confidence = best.confidence
                    qr.is_recommended = best.is_recommended

                    if best.is_recommended:
                        # Compute actual rank among all recommended brands
                        all_recommended: list[tuple[str, int]] = []
                        for b in brands:
                            b_mentions = detect_mentions(
                                resp.response_text, b.name, b.aliases,
                                industry=project_industry,
                            )
                            for m in b_mentions:
                                if m.is_recommended:
                                    all_recommended.append((b.name, m.position))
                        all_recommended.sort(key=lambda x: x[1])
                        rank = next(
                            (i + 1 for i, (name, _) in enumerate(all_recommended)
                             if name == brand.name),
                            None,
                        )
                        qr.recommendation_rank = rank

            results.append(qr)

    db.add_all(results)

    # Determine final status
    error_count = sum(1 for _, r in all_responses if not r.success)
    total = len(all_responses)
    if error_count == total:
        audit.status = QueryStatus.FAILED
        audit.error_message = "All platform queries failed"
    elif error_count > 0:
        audit.status = QueryStatus.PARTIAL
    else:
        audit.status = QueryStatus.COMPLETED

    audit.completed_at = datetime.now(timezone.utc)
    await db.commit()
    publish(audit.id, PlatformEvent(type="audit_done"))
    logger.info(
        "audit_completed",
        audit_id=audit.id,
        status=audit.status.value,
        results=len(results),
    )


async def _query_platform(
    platform_name: str, adapter, prompt_texts: list[str]
) -> list[PlatformResponse]:
    """Query a single platform with all prompts."""
    return await adapter.query(prompt_texts)
