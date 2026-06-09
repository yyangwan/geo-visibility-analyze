"""Tests for ResponseAnalysisService - unit + integration."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import (
    Audit,
    PlatformResponseRecord,
    Prompt,
    QueryStatus,
    ResponseAnalysis,
)
from app.services.response_analysis_service import (
    MAX_RESPONSE_CHARS,
    _analyze_single,
    _call_llm_for_analysis,
)


def _brand_names(brands: list[dict], *, competitor: bool) -> list[str]:
    return [b["name"] for b in brands if bool(b.get("is_competitor")) == competitor]


async def _seed_audit(db: AsyncSession, num_prrs: int = 3):
    """Create a user, project, audit snapshot, prompt, and PRRs."""
    project_id = "project-1"

    brands = [
        {"id": "brand-1", "name": "测试品牌", "aliases": ["测试"], "is_competitor": False},
        {"id": "brand-2", "name": "竞品A", "aliases": [], "is_competitor": True},
    ]

    prompt = Prompt(project_id=project_id, text="推荐一款保险产品")
    db.add(prompt)
    await db.flush()

    audit = Audit(
        project_id=project_id,
        status=QueryStatus.COMPLETED,
        platforms_json=["deepseek"],
        brands_json=brands,
    )
    db.add(audit)
    await db.flush()

    platforms = ["deepseek", "qwen", "doubao", "kimi", "hunyuan"]
    prrs = []
    for i in range(num_prrs):
        prr = PlatformResponseRecord(
            audit_id=audit.id,
            prompt_id=prompt.id,
            platform=platforms[i % len(platforms)],
            response_text=f"这是第{i + 1}个AI平台的回答内容，包含关于测试品牌的信息。",
            prompt_tokens=100,
            completion_tokens=200,
        )
        db.add(prr)
        prrs.append(prr)
    await db.flush()

    return audit, prrs, brands


MOCK_LLM_RESPONSE = {
    "brand_sentiment": "positive",
    "brand_attributes": ["性价比高", "服务好"],
    "topics_covered": ["产品特点", "价格对比"],
    "answer_structure": "list",
    "competitor_refs": ["竞品A"],
    "cited_sources": [{"domain": "example.com", "authority_score": 4}],
}


@pytest.mark.asyncio
async def test_call_llm_for_analysis_success(db_session: AsyncSession):
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
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=Exception("API error"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.response_analysis_service.httpx.AsyncClient", return_value=mock_client):
        result = await _call_llm_for_analysis("sys", "user", 60)

    assert result is None


@pytest.mark.asyncio
async def test_call_llm_for_analysis_invalid_json(db_session: AsyncSession):
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
    long_text = "x" * 5000
    captured_prompt: dict[str, str] = {}

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

            _, prrs, brands = await _seed_audit(db_session, 1)
            prr = prrs[0]
            prr.response_text = long_text

            ra = ResponseAnalysis(response_record_id=prr.id, status="pending")
            db_session.add(ra)
            await db_session.commit()

            await _analyze_single(
                db_session,
                ra,
                _brand_names(brands, competitor=False),
                _brand_names(brands, competitor=True),
            )

    assert captured_prompt["text"].count("x") == MAX_RESPONSE_CHARS


@pytest.mark.asyncio
async def test_analyze_single_success(db_session: AsyncSession):
    _, prrs, brands = await _seed_audit(db_session, 1)
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
        await _analyze_single(
            db_session,
            ra,
            _brand_names(brands, competitor=False),
            _brand_names(brands, competitor=True),
        )

    assert ra.status == "completed"
    assert ra.brand_sentiment == "positive"
    assert ra.brand_attributes == ["性价比高", "服务好"]
    assert len(ra.topics_covered) == 2
    assert ra.answer_structure == "list"


@pytest.mark.asyncio
async def test_analyze_single_llm_failure(db_session: AsyncSession):
    _, prrs, brands = await _seed_audit(db_session, 1)
    ra = ResponseAnalysis(response_record_id=prrs[0].id, status="pending")
    db_session.add(ra)
    await db_session.commit()

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=Exception("API error"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.response_analysis_service.httpx.AsyncClient", return_value=mock_client):
        await _analyze_single(
            db_session,
            ra,
            _brand_names(brands, competitor=False),
            _brand_names(brands, competitor=True),
        )

    assert ra.status == "failed"


@pytest.mark.asyncio
async def test_analyze_single_no_response_text(db_session: AsyncSession):
    _, prrs, brands = await _seed_audit(db_session, 1)
    prrs[0].response_text = None
    await db_session.commit()

    ra = ResponseAnalysis(response_record_id=prrs[0].id, status="pending")
    db_session.add(ra)
    await db_session.commit()

    await _analyze_single(
        db_session,
        ra,
        _brand_names(brands, competitor=False),
        _brand_names(brands, competitor=True),
    )

    assert ra.status == "failed"
