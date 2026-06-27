"""Tests for content intelligence aggregation."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.main import app
from app.models.models import (
    Audit,
    PlatformResponseRecord,
    Prompt,
    QueryStatus,
    ResponseAnalysis,
    SourceCitation,
)


PROJECT_ID = "project-content-intelligence"


async def _override_user():
    return {"scope": "project", "pid": PROJECT_ID}


async def _seed_audit(
    db: AsyncSession,
    *,
    project_id: str = PROJECT_ID,
    cited_sources: list[dict] | None = None,
) -> Audit:
    prompt = Prompt(project_id=project_id, text="推荐一个咖啡器具", is_auto_generated=True)
    db.add(prompt)
    await db.flush()

    audit = Audit(project_id=project_id, status=QueryStatus.COMPLETED)
    db.add(audit)
    await db.flush()

    prr = PlatformResponseRecord(
        audit_id=audit.id,
        prompt_id=prompt.id,
        platform="deepseek",
        response_text="推荐手冲壶，并引用了权威来源。",
        prompt_tokens=12,
        completion_tokens=34,
    )
    db.add(prr)
    await db.flush()

    db.add(
        ResponseAnalysis(
            response_record_id=prr.id,
            cited_sources=cited_sources or [],
            brand_sentiment="positive",
            topics_covered=["购买建议"],
            answer_structure="list",
            analysis_model="test-model",
            status="completed",
        )
    )
    await db.flush()
    return audit


@pytest.mark.asyncio
async def test_content_intelligence_uses_source_citations_when_analysis_sources_empty(
    client: AsyncClient,
    db_session: AsyncSession,
):
    app.dependency_overrides[get_current_user] = _override_user
    try:
        audit = await _seed_audit(db_session)
        db_session.add_all(
            [
                SourceCitation(
                    project_id=PROJECT_ID,
                    audit_id=audit.id,
                    domain="zhihu.com",
                    urls=["https://www.zhihu.com/question/1"],
                    citation_count=2,
                    platform="deepseek",
                ),
                SourceCitation(
                    project_id=PROJECT_ID,
                    audit_id=audit.id,
                    domain="chinapp.com",
                    urls=["https://www.chinapp.com/1"],
                    citation_count=1,
                    platform="deepseek",
                ),
            ]
        )
        await db_session.commit()

        resp = await client.get(f"/api/analysis/projects/{PROJECT_ID}/content-intelligence")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 200
    sources = resp.json()["top_cited_sources"]
    assert sources[0]["domain"] == "zhihu.com"
    assert sources[0]["total_count"] == 2
    assert sources[0]["authority_avg"] == 3.0
    assert {source["domain"] for source in sources} == {"zhihu.com", "chinapp.com"}


@pytest.mark.asyncio
async def test_content_intelligence_filters_by_requested_audit_id(
    client: AsyncClient,
    db_session: AsyncSession,
):
    app.dependency_overrides[get_current_user] = _override_user
    try:
        requested_audit = await _seed_audit(db_session)
        latest_audit = await _seed_audit(db_session)
        db_session.add_all(
            [
                SourceCitation(
                    project_id=PROJECT_ID,
                    audit_id=requested_audit.id,
                    domain="requested.example",
                    urls=["https://requested.example/a"],
                    citation_count=1,
                    platform="deepseek",
                ),
                SourceCitation(
                    project_id=PROJECT_ID,
                    audit_id=latest_audit.id,
                    domain="latest.example",
                    urls=["https://latest.example/a"],
                    citation_count=1,
                    platform="deepseek",
                ),
            ]
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/analysis/projects/{PROJECT_ID}/content-intelligence",
            params={"audit_id": requested_audit.id},
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 200
    sources = resp.json()["top_cited_sources"]
    assert [source["domain"] for source in sources] == ["requested.example"]


@pytest.mark.asyncio
async def test_content_intelligence_filters_synthetic_analysis_sources(
    client: AsyncClient,
    db_session: AsyncSession,
):
    app.dependency_overrides[get_current_user] = _override_user
    try:
        await _seed_audit(
            db_session,
            cited_sources=[
                {"domain": "source_S1", "authority_score": 3},
                {"domain": "source-c.com", "authority_score": 4},
            ],
        )
        await db_session.commit()

        resp = await client.get(f"/api/analysis/projects/{PROJECT_ID}/content-intelligence")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 200
    sources = resp.json()["top_cited_sources"]
    assert [source["domain"] for source in sources] == ["source-c.com"]
