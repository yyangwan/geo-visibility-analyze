"""Tests for Strategic Intelligence Service - cross-audit trend analysis."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Audit, PlatformResponseRecord, Prompt, QueryResult, QueryStatus, Report, ResponseAnalysis, SourceCitation
from app.services.strategic_intelligence_service import (
    get_answer_structure_evolution,
    get_competitor_positioning_map,
    get_multi_audit_comparison,
    get_source_authority_trends,
)


PROJECT_ID = "project-strategic"
BRANDS_JSON = [
    {"id": "brand-1", "name": "主品牌", "aliases": [], "is_competitor": False},
    {"id": "brand-2", "name": "竞品A", "aliases": [], "is_competitor": True},
]


async def _seed_prompt(db: AsyncSession):
    prompt = Prompt(project_id=PROJECT_ID, text="推荐一款保险产品", is_auto_generated=True)
    db.add(prompt)
    await db.flush()
    return prompt


async def _create_audit_with_data(
    db: AsyncSession,
    prompt: Prompt,
    *,
    response_text: str = "推荐主品牌保险",
    sentiment: str = "positive",
    structure: str = "list",
    competitor_refs: list[str] | None = None,
    cited_sources: list[dict] | None = None,
    mention_found: bool = True,
) -> Audit:
    audit = Audit(project_id=PROJECT_ID, status=QueryStatus.COMPLETED, brands_json=BRANDS_JSON)
    db.add(audit)
    await db.flush()

    prr = PlatformResponseRecord(
        audit_id=audit.id,
        prompt_id=prompt.id,
        platform="deepseek",
        response_text=response_text,
    )
    db.add(prr)
    await db.flush()

    ra = ResponseAnalysis(
        response_record_id=prr.id,
        brand_sentiment=sentiment,
        brand_attributes=["性价比高"],
        topics_covered=["产品特点"],
        answer_structure=structure,
        competitor_refs=competitor_refs or ["竞品A"],
        cited_sources=cited_sources or [{"domain": "example.com", "authority_score": 4}],
        analysis_model="test-model",
        status="completed",
    )
    db.add(ra)

    qr = QueryResult(
        audit_id=audit.id,
        prompt_id=prompt.id,
        brand_id="brand-1",
        platform="deepseek",
        response_text=response_text,
        mention_found=mention_found,
        mention_position=1 if mention_found else None,
        is_recommended=mention_found,
        recommendation_rank=1 if mention_found else None,
        response_record_id=prr.id,
    )
    db.add(qr)

    qr_comp = QueryResult(
        audit_id=audit.id,
        prompt_id=prompt.id,
        brand_id="brand-2",
        platform="deepseek",
        response_text=response_text,
        mention_found=True,
        response_record_id=prr.id,
    )
    db.add(qr_comp)

    await db.flush()
    return audit


@pytest.mark.asyncio
async def test_source_authority_trends_empty(db_session: AsyncSession):
    result = await get_source_authority_trends(db_session, PROJECT_ID)
    assert result["audits"] == []
    assert result["domain_trends"] == []


@pytest.mark.asyncio
async def test_source_authority_trends_with_data(db_session: AsyncSession):
    prompt = await _seed_prompt(db_session)

    audit1 = await _create_audit_with_data(
        db_session,
        prompt,
        cited_sources=[
            {"domain": "source-a.com", "authority_score": 4},
            {"domain": "source-b.com", "authority_score": 2},
        ],
    )
    await _create_audit_with_data(
        db_session,
        prompt,
        cited_sources=[
            {"domain": "source-a.com", "authority_score": 5},
            {"domain": "source-c.com", "authority_score": 3},
        ],
    )

    result = await get_source_authority_trends(db_session, PROJECT_ID, limit=10)
    assert len(result["audits"]) == 2
    source_a = next((d for d in result["domain_trends"] if d["domain"] == "source-a.com"), None)
    assert source_a is not None
    assert len(source_a["data"]) == 2


@pytest.mark.asyncio
async def test_competitor_positioning_empty(db_session: AsyncSession):
    prompt = await _seed_prompt(db_session)
    # Seed one audit so the service can read brand snapshots.
    await _create_audit_with_data(db_session, prompt, mention_found=False)

    result = await get_competitor_positioning_map(db_session, PROJECT_ID)
    assert len(result["brands"]) >= 2
    assert result["quadrant_labels"]["q1"]


@pytest.mark.asyncio
async def test_competitor_positioning_with_data(db_session: AsyncSession):
    prompt = await _seed_prompt(db_session)
    await _create_audit_with_data(db_session, prompt, sentiment="positive")

    result = await get_competitor_positioning_map(db_session, PROJECT_ID)
    assert len(result["brands"]) >= 2
    primary = next((b for b in result["brands"] if not b["is_competitor"]), None)
    assert primary is not None
    assert primary["name"] == "主品牌"
    assert primary["mention_frequency"] > 0


@pytest.mark.asyncio
async def test_structure_evolution_empty(db_session: AsyncSession):
    result = await get_answer_structure_evolution(db_session, PROJECT_ID)
    assert result["audits"] == []
    assert result["structure_distribution"] == {}


@pytest.mark.asyncio
async def test_structure_evolution_with_data(db_session: AsyncSession):
    prompt = await _seed_prompt(db_session)
    await _create_audit_with_data(db_session, prompt, structure="list")
    await _create_audit_with_data(db_session, prompt, structure="comparison")

    result = await get_answer_structure_evolution(db_session, PROJECT_ID, limit=10)
    assert len(result["audits"]) == 2
    assert "list" in result["structure_distribution"]
    assert "comparison" in result["structure_distribution"]


@pytest.mark.asyncio
async def test_multi_audit_comparison_too_few(db_session: AsyncSession):
    result = await get_multi_audit_comparison(db_session, PROJECT_ID, [1])
    assert result["audits"] == []


@pytest.mark.asyncio
async def test_multi_audit_comparison_with_data(db_session: AsyncSession):
    prompt = await _seed_prompt(db_session)

    audit1 = await _create_audit_with_data(
        db_session,
        prompt,
        cited_sources=[{"domain": "old-source.com", "authority_score": 3}],
    )
    report1 = Report(project_id=PROJECT_ID, audit_id=audit1.id, overall_score=60, mention_rate=0.5)
    db_session.add(report1)
    await db_session.flush()

    audit2 = await _create_audit_with_data(
        db_session,
        prompt,
        cited_sources=[{"domain": "new-source.com", "authority_score": 4}],
    )
    report2 = Report(project_id=PROJECT_ID, audit_id=audit2.id, overall_score=75, mention_rate=0.8)
    db_session.add(report2)
    await db_session.flush()

    result = await get_multi_audit_comparison(db_session, PROJECT_ID, [audit1.id, audit2.id])
    assert len(result["audits"]) == 2
    assert result["diffs"]["score_delta"] == 15.0
    assert result["diffs"]["mention_rate_delta"] == pytest.approx(0.3, abs=0.01)
    assert "old-source.com" in result["diffs"]["source_changes"]["removed"]
    assert "new-source.com" in result["diffs"]["source_changes"]["added"]


@pytest.mark.asyncio
async def test_multi_audit_comparison_invalid_ids(db_session: AsyncSession):
    result = await get_multi_audit_comparison(db_session, PROJECT_ID, [9999, 8888])
    assert result["audits"] == []

