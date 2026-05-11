"""Tests for ResponseAnalysisService — unit + integration."""

import json
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import (
    Audit,
    Brand,
    PlatformResponseRecord,
    Prompt,
    ResponseAnalysis,
    User,
    QueryStatus,
)
from app.services.response_analysis_service import (
    MAX_RESPONSE_CHARS,
    _call_llm_for_analysis,
    _analyze_single,
    retry_failed_analyses,
    run_analysis_for_audit,
)


# ---- Helpers ----

async def _seed_audit(db: AsyncSession, num_prrs: int = 3):
    """Create a user, project, audit, brands, prompts, and PRRs."""
    user = User(username="testuser", hashed_password="x")
    db.add(user)
    await db.flush()

    from app.models.models import Project
    project = Project(name="Test", industry="insurance", user_id=user.id)
    db.add(project)
    await db.flush()

    brand = Brand(project_id=project.id, name="测试品牌", aliases=["测试"], is_competitor=False)
    competitor = Brand(project_id=project.id, name="竞品A", is_competitor=True)
    db.add_all([brand, competitor])
    await db.flush()

    prompt = Prompt(project_id=project.id, text="推荐一款保险产品")
    db.add(prompt)
    await db.flush()

    audit = Audit(project_id=project.id, status=QueryStatus.COMPLETED, platforms_json=["deepseek"])
    db.add(audit)
    await db.flush()

    platforms = ["deepseek", "qwen", "doubao", "kimi", "hunyuan"]
    prrs = []
    for i in range(num_prrs):
        prr = PlatformResponseRecord(
            audit_id=audit.id,
            prompt_id=prompt.id,
            platform=platforms[i % len(platforms)],
            response_text=f"这是第{i+1}个AI平台的回答内容，包含关于测试品牌的信息。",
            prompt_tokens=100,
            completion_tokens=200,
        )
        db.add(prr)
        prrs.append(prr)
    await db.flush()

    return audit, prrs, [brand, competitor]


MOCK_LLM_RESPONSE = {
    "brand_sentiment": "positive",
    "brand_attributes": ["性价比高", "服务好"],
    "topics_covered": ["产品特点", "价格对比"],
    "answer_structure": "list",
    "competitor_refs": ["竞品A"],
    "cited_sources": [{"domain": "example.com", "authority_score": 4}],
}


# ---- Unit Tests ----

@pytest.mark.asyncio
async def test_call_llm_for_analysis_success(db_session: AsyncSession):
    """LLM returns valid JSON — parsed correctly."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": json.dumps(MOCK_LLM_RESPONSE)}}]
    }

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.response_analysis_service.httpx.AsyncClient", return_value=mock_client):
        result = await _call_llm_for_analysis("sys", "user", 60)

    assert result is not None
    assert result["brand_sentiment"] == "positive"
    assert len(result["topics_covered"]) == 2


@pytest.mark.asyncio
async def test_call_llm_for_analysis_with_code_fences(db_session: AsyncSession):
    """LLM returns JSON wrapped in markdown code fences — extracted correctly."""
    content = "```json\n" + json.dumps(MOCK_LLM_RESPONSE) + "\n```"
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": content}}]
    }

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.response_analysis_service.httpx.AsyncClient", return_value=mock_client):
        result = await _call_llm_for_analysis("sys", "user", 60)

    assert result is not None
    assert result["brand_sentiment"] == "positive"


@pytest.mark.asyncio
async def test_call_llm_for_analysis_failure(db_session: AsyncSession):
    """LLM call throws exception — returns None."""
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=Exception("API error"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.response_analysis_service.httpx.AsyncClient", return_value=mock_client):
        result = await _call_llm_for_analysis("sys", "user", 60)

    assert result is None


@pytest.mark.asyncio
async def test_call_llm_for_analysis_invalid_json(db_session: AsyncSession):
    """LLM returns non-JSON text — returns None."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": "This is not JSON"}}]
    }

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.response_analysis_service.httpx.AsyncClient", return_value=mock_client):
        result = await _call_llm_for_analysis("sys", "user", 60)

    assert result is None


@pytest.mark.asyncio
async def test_response_text_truncation(db_session: AsyncSession):
    """Response text over 4000 chars is truncated in the user prompt."""
    long_text = "x" * 5000
    captured_prompt = {}

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": json.dumps(MOCK_LLM_RESPONSE)}}]
    }

    async def capture_post(url, **kwargs):
        captured_prompt["text"] = kwargs["json"]["messages"][1]["content"]
        return mock_resp

    mock_client = AsyncMock()
    mock_client.post = capture_post
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.response_analysis_service.httpx.AsyncClient", return_value=mock_client):
        with patch("app.services.response_analysis_service.settings") as mock_settings:
            mock_settings.get_llm_config.return_value = ("key", "url", "model")
            mock_settings.analysis_timeout_seconds = 60

            # Create a PRR with long text
            audit, prrs, brands = await _seed_audit(db_session, 1)
            prr = prrs[0]
            prr.response_text = long_text

            # Create a ResponseAnalysis
            ra = ResponseAnalysis(response_record_id=prr.id, status="pending")
            db_session.add(ra)
            await db_session.commit()

            from app.services.response_analysis_service import _analyze_single
            await _analyze_single(
                db_session, ra,
                ["测试品牌"],
                ["竞品A"],
            )

    assert long_text[:MAX_RESPONSE_CHARS] in captured_prompt["text"]
    assert len(captured_prompt["text"].split("AI平台回答内容：\n")[1]) <= MAX_RESPONSE_CHARS + 50


# ---- Integration Tests ----

@pytest.mark.asyncio
async def test_analyze_single_success(db_session: AsyncSession):
    """_analyze_single creates a completed analysis with all fields."""
    audit, prrs, brands = await _seed_audit(db_session, 1)
    ra = ResponseAnalysis(response_record_id=prrs[0].id, status="pending")
    db_session.add(ra)
    await db_session.commit()

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": json.dumps(MOCK_LLM_RESPONSE)}}]
    }

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.response_analysis_service.httpx.AsyncClient", return_value=mock_client):
        await _analyze_single(db_session, ra, ["测试品牌"], ["竞品A"])

    assert ra.status == "completed"
    assert ra.brand_sentiment == "positive"
    assert ra.brand_attributes == ["性价比高", "服务好"]
    assert len(ra.topics_covered) == 2
    assert ra.answer_structure == "list"


@pytest.mark.asyncio
async def test_analyze_single_llm_failure(db_session: AsyncSession):
    """_analyze_single handles LLM failure — status becomes 'failed'."""
    audit, prrs, brands = await _seed_audit(db_session, 1)
    ra = ResponseAnalysis(response_record_id=prrs[0].id, status="pending")
    db_session.add(ra)
    await db_session.commit()

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=Exception("API error"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.response_analysis_service.httpx.AsyncClient", return_value=mock_client):
        await _analyze_single(db_session, ra, ["测试品牌"], ["竞品A"])

    assert ra.status == "failed"


@pytest.mark.asyncio
async def test_analyze_single_no_response_text(db_session: AsyncSession):
    """_analyze_single handles PRR with no response_text — status becomes 'failed'."""
    audit, prrs, brands = await _seed_audit(db_session, 1)
    prrs[0].response_text = None
    await db_session.commit()

    ra = ResponseAnalysis(response_record_id=prrs[0].id, status="pending")
    db_session.add(ra)
    await db_session.commit()

    await _analyze_single(db_session, ra, ["测试品牌"], ["竞品A"])

    assert ra.status == "failed"
