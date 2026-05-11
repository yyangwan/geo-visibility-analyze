"""Tests for Strategic Intelligence Service — cross-audit trend analysis."""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import (
    Audit,
    Brand,
    PlatformResponseRecord,
    Project,
    Prompt,
    QueryResult,
    QueryStatus,
    Report,
    ResponseAnalysis,
    SourceCitation,
    User,
)
from app.services.strategic_intelligence_service import (
    get_answer_structure_evolution,
    get_competitor_positioning_map,
    get_multi_audit_comparison,
    get_source_authority_trends,
)


# ---- Helpers ----

async def _seed_project(db: AsyncSession):
    """Create user + project, return project."""
    user = User(username="stratuser", hashed_password="x")
    db.add(user)
    await db.flush()

    project = Project(name="StratTest", industry="insurance", user_id=user.id)
    db.add(project)
    await db.flush()

    brand = Brand(project_id=project.id, name="主品牌", aliases=[], is_competitor=False)
    db.add(brand)
    await db.flush()

    comp = Brand(project_id=project.id, name="竞品A", aliases=[], is_competitor=True)
    db.add(comp)
    await db.flush()

    prompt = Prompt(project_id=project.id, text="推荐一款保险", is_auto_generated=True)
    db.add(prompt)
    await db.flush()

    return project, brand, comp, prompt


async def _create_audit_with_data(
    db: AsyncSession,
    project: Project,
    brand: Brand,
    comp: Brand,
    prompt: Prompt,
    status: str = "completed",
    response_text: str = "推荐主品牌保险",
    sentiment: str = "positive",
    structure: str = "list",
    competitor_refs: list[str] | None = None,
    cited_sources: list[dict] | None = None,
    mention_found: bool = True,
):
    """Create audit + PRR + ResponseAnalysis + QueryResult."""
    audit = Audit(project_id=project.id, status=QueryStatus.COMPLETED)
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
    await db.flush()

    qr = QueryResult(
        audit_id=audit.id,
        prompt_id=prompt.id,
        brand_id=brand.id,
        platform="deepseek",
        response_text=response_text,
        mention_found=mention_found,
        mention_position=1 if mention_found else None,
        is_recommended=mention_found,
        recommendation_rank=1 if mention_found else None,
        response_record_id=prr.id,
    )
    db.add(qr)

    # Also add a query result for the competitor
    qr_comp = QueryResult(
        audit_id=audit.id,
        prompt_id=prompt.id,
        brand_id=comp.id,
        platform="deepseek",
        response_text=response_text,
        mention_found=True,
        response_record_id=prr.id,
    )
    db.add(qr_comp)

    await db.flush()
    return audit


# ---- Tests ----

@pytest.mark.asyncio
async def test_source_authority_trends_empty(db_session: AsyncSession):
    """Should return empty when no audits exist."""
    project, _, _, _ = await _seed_project(db_session)
    result = await get_source_authority_trends(db_session, project.id)
    assert result["audits"] == []
    assert result["domain_trends"] == []


@pytest.mark.asyncio
async def test_source_authority_trends_with_data(db_session: AsyncSession):
    """Should aggregate source citations across audits."""
    project, brand, comp, prompt = await _seed_project(db_session)

    # Create 2 audits with different sources
    await _create_audit_with_data(
        db_session, project, brand, comp, prompt,
        cited_sources=[{"domain": "source-a.com", "authority_score": 4}, {"domain": "source-b.com", "authority_score": 2}],
    )
    await _create_audit_with_data(
        db_session, project, brand, comp, prompt,
        cited_sources=[{"domain": "source-a.com", "authority_score": 5}, {"domain": "source-c.com", "authority_score": 3}],
    )

    result = await get_source_authority_trends(db_session, project.id, limit=10)
    assert len(result["audits"]) == 2
    assert len(result["domain_trends"]) > 0
    # source-a.com should appear in both audits
    source_a = next((d for d in result["domain_trends"] if d["domain"] == "source-a.com"), None)
    assert source_a is not None
    assert len(source_a["data"]) == 2


@pytest.mark.asyncio
async def test_competitor_positioning_empty(db_session: AsyncSession):
    """Should return brands with zero data when no audits."""
    project, _, _, _ = await _seed_project(db_session)
    result = await get_competitor_positioning_map(db_session, project.id)
    assert len(result["brands"]) >= 2  # primary + competitor
    assert result["quadrant_labels"]["q1"]  # should have quadrant labels


@pytest.mark.asyncio
async def test_competitor_positioning_with_data(db_session: AsyncSession):
    """Should compute mention frequency and sentiment for brands."""
    project, brand, comp, prompt = await _seed_project(db_session)
    await _create_audit_with_data(db_session, project, brand, comp, prompt, sentiment="positive")

    result = await get_competitor_positioning_map(db_session, project.id)
    assert len(result["brands"]) >= 2

    primary = next((b for b in result["brands"] if not b["is_competitor"]), None)
    assert primary is not None
    assert primary["name"] == "主品牌"
    assert primary["mention_frequency"] > 0


@pytest.mark.asyncio
async def test_structure_evolution_empty(db_session: AsyncSession):
    """Should return empty when no audits."""
    project, _, _, _ = await _seed_project(db_session)
    result = await get_answer_structure_evolution(db_session, project.id)
    assert result["audits"] == []
    assert result["structure_distribution"] == {}


@pytest.mark.asyncio
async def test_structure_evolution_with_data(db_session: AsyncSession):
    """Should track structure types across audits."""
    project, brand, comp, prompt = await _seed_project(db_session)

    await _create_audit_with_data(db_session, project, brand, comp, prompt, structure="list")
    await _create_audit_with_data(db_session, project, brand, comp, prompt, structure="comparison")

    result = await get_answer_structure_evolution(db_session, project.id, limit=10)
    assert len(result["audits"]) == 2
    assert "list" in result["structure_distribution"]
    assert "comparison" in result["structure_distribution"]


@pytest.mark.asyncio
async def test_multi_audit_comparison_too_few(db_session: AsyncSession):
    """Should return empty when less than 2 audit_ids."""
    project, _, _, _ = await _seed_project(db_session)
    result = await get_multi_audit_comparison(db_session, project.id, [1])
    assert result["audits"] == []


@pytest.mark.asyncio
async def test_multi_audit_comparison_with_data(db_session: AsyncSession):
    """Should compare two audits and compute diffs."""
    project, brand, comp, prompt = await _seed_project(db_session)

    audit1 = await _create_audit_with_data(
        db_session, project, brand, comp, prompt,
        cited_sources=[{"domain": "old-source.com", "authority_score": 3}],
    )

    # Add report for audit1
    report1 = Report(
        project_id=project.id,
        audit_id=audit1.id,
        overall_score=60,
        mention_rate=0.5,
    )
    db_session.add(report1)
    await db_session.flush()

    audit2 = await _create_audit_with_data(
        db_session, project, brand, comp, prompt,
        cited_sources=[{"domain": "new-source.com", "authority_score": 4}],
    )

    report2 = Report(
        project_id=project.id,
        audit_id=audit2.id,
        overall_score=75,
        mention_rate=0.8,
    )
    db_session.add(report2)
    await db_session.flush()

    result = await get_multi_audit_comparison(db_session, project.id, [audit1.id, audit2.id])
    assert len(result["audits"]) == 2
    assert result["diffs"]["score_delta"] == 15.0
    assert result["diffs"]["mention_rate_delta"] == pytest.approx(0.3, abs=0.01)
    assert "old-source.com" in result["diffs"]["source_changes"]["removed"]
    assert "new-source.com" in result["diffs"]["source_changes"]["added"]


@pytest.mark.asyncio
async def test_multi_audit_comparison_invalid_ids(db_session: AsyncSession):
    """Should return empty when audit IDs don't belong to project."""
    project, _, _, _ = await _seed_project(db_session)
    result = await get_multi_audit_comparison(db_session, project.id, [9999, 8888])
    assert result["audits"] == []
