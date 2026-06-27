"""Tests for evidence-grounded suggestion generation."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import (
    Audit,
    PlatformResponseRecord,
    Prompt,
    QueryResult,
    QueryStatus,
    Report,
    ResponseAnalysis,
    SourceCitation,
)
from app.services import suggestion_service
from app.services.suggestion_service import _get_evidence_context, generate_suggestions


@pytest.mark.asyncio
async def test_get_evidence_context_includes_audit_samples(db_session: AsyncSession):
    audit, _ = await _seed_suggestion_audit(db_session)

    brands = [
        suggestion_service.BrandData(id="brand-1", name="Acme", aliases=[], is_competitor=False),
        suggestion_service.BrandData(id="brand-2", name="CompetitorX", aliases=[], is_competitor=True),
    ]

    context = await _get_evidence_context(db_session, audit.id, brands)

    assert "平台表现" in context
    assert "deepseek" in context
    assert "prompt=\"推荐AI分析工具\"" in context
    assert "本品牌未提及" in context
    assert "CompetitorX" in context
    assert "引用来源" in context
    assert "zhihu.com" in context
    assert "https://www.zhihu.com/question/123" in context
    assert "可复用正向样本" in context


@pytest.mark.asyncio
async def test_generate_suggestions_sends_evidence_to_both_passes(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    audit, report = await _seed_suggestion_audit(db_session)
    captured_prompts: list[str] = []

    async def fake_call_llm(prompt: str, *, system: str, expect_array: bool = True):
        captured_prompts.append(prompt)
        if expect_array:
            return [
                {
                    "category": "platform_focus",
                    "title": "补齐DeepSeek场景",
                    "description": "针对DeepSeek中推荐AI分析工具场景未提及的问题，在官网博客发布对比评测并补充FAQ。",
                    "priority": "high",
                    "target_platforms": ["deepseek"],
                    "evidence_sources": ["zhihu.com", "competitorx.com/cases"],
                    "evidence_channels": ["知乎", "官网博客"],
                    "action_sources": ["acme.com/blog", "zhihu.com"],
                    "action_channels": ["官网博客"],
                    "action_type": "发布对比评测文章",
                    "audit_findings": ["deepseek prompt=推荐AI分析工具 本品牌未提及，竞品出现"],
                    "evidence_summary": "来自DeepSeek低提及和竞品压制样本",
                    "success_metric": "DeepSeek推荐类prompt提及率提升到50%",
                }
            ]
        return {
            "evidence_sources": ["zhihu.com", "competitorx.com/cases"],
            "evidence_channels": ["知乎", "官网博客"],
            "action_sources": ["acme.com/blog", "zhihu.com"],
            "action_channels": ["官网博客"],
            "action_type": "发布对比评测文章",
            "outline": ["补齐选型场景", "对比竞品差异", "增加FAQ"],
            "keywords": ["AI分析工具", "Acme", "竞品对比"],
            "timeline": [{"week": 1, "task": "完成内容发布"}],
            "competitor_ref": "参考CompetitorX在推荐回答中的高频出现方式",
            "expected_outcome": "提升DeepSeek提及率",
            "acceptance_criteria": ["下次审计DeepSeek推荐类prompt出现Acme"],
        }

    monkeypatch.setattr(suggestion_service, "_call_llm", fake_call_llm)

    suggestions = await generate_suggestions(db_session, report)

    assert len(suggestions) == 1
    assert len(captured_prompts) == 2
    assert "审计证据样本" in captured_prompts[0]
    assert "推荐AI分析工具" in captured_prompts[0]
    assert "本品牌未提及" in captured_prompts[0]
    assert "zhihu.com" in captured_prompts[0]
    assert "https://www.zhihu.com/question/123" in captured_prompts[0]
    assert "证据引用来源网站：zhihu.com, competitorx.com/cases" in captured_prompts[1]
    assert "行动落点网站：acme.com/blog, zhihu.com" in captured_prompts[1]
    assert "该建议的审计发现" in captured_prompts[1]
    assert "DeepSeek推荐类prompt提及率提升到50%" in captured_prompts[1]
    assert suggestions[0].detail["evidence_sources"] == ["zhihu.com", "competitorx.com/cases"]
    assert suggestions[0].detail["action_sources"] == ["acme.com/blog", "zhihu.com"]
    assert suggestions[0].detail["audit_findings"] == ["deepseek prompt=推荐AI分析工具 本品牌未提及，竞品出现"]
    assert suggestions[0].detail["success_metric"] == "DeepSeek推荐类prompt提及率提升到50%"


async def _seed_suggestion_audit(db: AsyncSession) -> tuple[Audit, Report]:
    prompt_gap = Prompt(project_id="project-1", text="推荐AI分析工具")
    prompt_win = Prompt(project_id="project-1", text="Acme适合什么团队")
    db.add_all([prompt_gap, prompt_win])
    await db.flush()

    audit = Audit(
        project_id="project-1",
        status=QueryStatus.COMPLETED,
        platforms_json=["deepseek", "kimi"],
        brands_json=[
            {"id": "brand-1", "name": "Acme", "aliases": [], "is_competitor": False},
            {"id": "brand-2", "name": "CompetitorX", "aliases": [], "is_competitor": True},
        ],
    )
    db.add(audit)
    await db.flush()

    prr_gap = PlatformResponseRecord(
        audit_id=audit.id,
        prompt_id=prompt_gap.id,
        platform="deepseek",
        response_text="推荐CompetitorX，因为它有成熟的数据分析案例。",
        citations=[
            {
                "domain": "zhihu.com",
                "url": "https://www.zhihu.com/question/123",
                "title": "AI分析工具选型讨论",
            },
            {
                "domain": "competitorx.com",
                "url": "https://competitorx.com/cases",
                "title": "CompetitorX客户案例",
            },
        ],
    )
    prr_win = PlatformResponseRecord(
        audit_id=audit.id,
        prompt_id=prompt_win.id,
        platform="kimi",
        response_text="Acme适合需要快速做AI可见性分析的团队。",
    )
    db.add_all([prr_gap, prr_win])
    await db.flush()

    db.add_all(
        [
            QueryResult(
                audit_id=audit.id,
                prompt_id=prompt_gap.id,
                brand_id="brand-1",
                platform="deepseek",
                response_record_id=prr_gap.id,
                response_text=prr_gap.response_text,
                mention_found=False,
                is_recommended=False,
            ),
            QueryResult(
                audit_id=audit.id,
                prompt_id=prompt_gap.id,
                brand_id="brand-2",
                platform="deepseek",
                response_record_id=prr_gap.id,
                response_text=prr_gap.response_text,
                mention_found=True,
                mention_context="推荐CompetitorX",
                mention_confidence=0.9,
                is_recommended=True,
                recommendation_rank=1,
            ),
            QueryResult(
                audit_id=audit.id,
                prompt_id=prompt_win.id,
                brand_id="brand-1",
                platform="kimi",
                response_record_id=prr_win.id,
                response_text=prr_win.response_text,
                mention_found=True,
                mention_context="Acme适合需要快速做AI可见性分析的团队",
                mention_confidence=0.8,
                is_recommended=True,
                recommendation_rank=1,
            ),
        ]
    )
    db.add(
        ResponseAnalysis(
            response_record_id=prr_gap.id,
            status="completed",
            brand_sentiment="neutral",
            topics_covered=["选型推荐", "竞品对比"],
            competitor_refs=["CompetitorX"],
            cited_sources=[{"domain": "36kr.com", "authority_score": 4}],
            brand_attributes=[],
        )
    )
    db.add(
        SourceCitation(
            project_id="project-1",
            audit_id=audit.id,
            domain="zhihu.com",
            urls=["https://www.zhihu.com/question/123"],
            citation_count=2,
            platform="deepseek",
        )
    )

    report = Report(
        project_id="project-1",
        audit_id=audit.id,
        overall_score=42,
        mention_rate=0.5,
        competitor_rank=2,
        platform_scores={"deepseek": 20, "kimi": 85},
        insights=["DeepSeek表现弱于Kimi"],
    )
    db.add(report)
    await db.commit()
    await db.refresh(audit)
    await db.refresh(report)
    return audit, report
