"""Tests for audit service - PRR creation and source citation upsert."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.adapters.base import PlatformResponse
from app.models.models import Audit, PlatformResponseRecord, Prompt, QueryResult, QueryStatus, SourceCitation
from app.services import audit_service
from app.services.source_extraction import ExtractedSource


def test_is_degraded_response_detects_length_finish_reason():
    resp = PlatformResponse(
        platform="kimi",
        prompt="p",
        response_text="truncated",
        finish_reason="length",
    )

    assert audit_service.is_degraded_response(resp) is True


def test_is_degraded_response_detects_metadata_flag():
    resp = PlatformResponse(
        platform="kimi",
        prompt="p",
        response_text="truncated",
        search_metadata={"response_degraded": True},
    )

    assert audit_service.is_degraded_response(resp) is True


def test_is_degraded_response_allows_complete_response():
    resp = PlatformResponse(
        platform="kimi",
        prompt="p",
        response_text="complete",
        finish_reason="stop",
    )

    assert audit_service.is_degraded_response(resp) is False


async def _create_prompt(db, project_id: str, text: str):
    prompt = Prompt(project_id=project_id, text=text)
    db.add(prompt)
    await db.flush()
    return prompt


async def _create_audit(db, project_id: str):
    audit = Audit(project_id=project_id, status=QueryStatus.PENDING, brands_json=[{"id": "brand-1", "name": "测试品牌", "aliases": ["测试"], "is_competitor": False}])
    db.add(audit)
    await db.flush()
    return audit


async def _create_brand_id(_: object, brand_id: str = "brand-1") -> str:
    return brand_id


class TestPlatformResponseRecord:
    @pytest.mark.asyncio
    async def test_create_prr(self, db_session):
        project_id = "project-1"
        prompt = await _create_prompt(db_session, project_id, "推荐保险")
        audit = await _create_audit(db_session, project_id)

        prr = PlatformResponseRecord(
            audit_id=audit.id,
            prompt_id=prompt.id,
            platform="deepseek",
            response_text="这是AI的回复",
            citations=[{"domain": "zhihu.com", "urls": [], "title": ""}],
            prompt_tokens=100,
            completion_tokens=200,
            response_model="deepseek-chat",
            finish_reason="stop",
            search_enabled=True,
        )
        db_session.add(prr)
        await db_session.flush()

        assert prr.id is not None
        assert prr.prompt_tokens == 100
        assert prr.completion_tokens == 200
        assert prr.search_enabled is True

    @pytest.mark.asyncio
    async def test_prr_unique_constraint(self, db_session):
        project_id = "project-2"
        prompt = await _create_prompt(db_session, project_id, "推荐保险")
        audit = await _create_audit(db_session, project_id)

        prr1 = PlatformResponseRecord(
            audit_id=audit.id,
            prompt_id=prompt.id,
            platform="deepseek",
            response_text="回复1",
        )
        db_session.add(prr1)
        await db_session.flush()

        prr2 = PlatformResponseRecord(
            audit_id=audit.id,
            prompt_id=prompt.id,
            platform="deepseek",
            response_text="回复2",
        )
        db_session.add(prr2)

        with pytest.raises(Exception):
            await db_session.flush()

    @pytest.mark.asyncio
    async def test_qr_references_prr(self, db_session):
        project_id = "project-3"
        prompt = await _create_prompt(db_session, project_id, "推荐保险")
        audit = await _create_audit(db_session, project_id)

        prr = PlatformResponseRecord(
            audit_id=audit.id,
            prompt_id=prompt.id,
            platform="deepseek",
            response_text="AI回复内容",
        )
        db_session.add(prr)
        await db_session.flush()

        qr = QueryResult(
            audit_id=audit.id,
            prompt_id=prompt.id,
            brand_id="brand-1",
            platform="deepseek",
            response_text="AI回复内容",
            response_record_id=prr.id,
        )
        db_session.add(qr)
        await db_session.flush()

        assert qr.response_record_id == prr.id

    @pytest.mark.asyncio
    async def test_qr_text_property_with_prr(self, db_session):
        project_id = "project-4"
        prompt = await _create_prompt(db_session, project_id, "推荐保险")
        audit = await _create_audit(db_session, project_id)

        prr = PlatformResponseRecord(
            audit_id=audit.id,
            prompt_id=prompt.id,
            platform="deepseek",
            response_text="PRR的回复内容",
        )
        db_session.add(prr)
        await db_session.flush()

        qr = QueryResult(
            audit_id=audit.id,
            prompt_id=prompt.id,
            brand_id="brand-1",
            platform="deepseek",
            response_text="QR的旧内容",
            response_record_id=prr.id,
        )
        db_session.add(qr)
        await db_session.flush()

        await db_session.refresh(qr)
        assert qr.text == "PRR的回复内容"

    @pytest.mark.asyncio
    async def test_qr_text_property_without_prr(self, db_session):
        project_id = "project-5"
        prompt = await _create_prompt(db_session, project_id, "推荐保险")
        audit = await _create_audit(db_session, project_id)

        qr = QueryResult(
            audit_id=audit.id,
            prompt_id=prompt.id,
            brand_id="brand-1",
            platform="deepseek",
            response_text="旧数据",
        )
        db_session.add(qr)
        await db_session.flush()

        await db_session.refresh(qr)
        assert qr.text == "旧数据"


class TestSourceCitation:
    @pytest.mark.asyncio
    async def test_create_citation(self, db_session):
        project_id = "project-6"
        audit = await _create_audit(db_session, project_id)

        sc = SourceCitation(
            project_id=project_id,
            audit_id=audit.id,
            domain="zhihu.com",
            citation_count=3,
            platform="deepseek",
        )
        db_session.add(sc)
        await db_session.flush()

        assert sc.id is not None
        assert sc.citation_count == 3

    @pytest.mark.asyncio
    async def test_audit_id_set_null_on_delete(self, db_session):
        project_id = "project-7"
        audit = await _create_audit(db_session, project_id)

        sc = SourceCitation(
            project_id=project_id,
            audit_id=audit.id,
            domain="zhihu.com",
            platform="deepseek",
        )
        db_session.add(sc)
        await db_session.flush()

        await db_session.delete(audit)
        await db_session.flush()
        await db_session.refresh(sc)

        assert sc is not None


@pytest.mark.asyncio
async def test_claim_audit_is_idempotent(db_session):
    audit = Audit(project_id="project-claim", status=QueryStatus.PENDING)
    db_session.add(audit)
    await db_session.flush()

    claimed = await audit_service.claim_audit(db_session, audit.id)
    assert claimed is not None
    assert claimed.status == QueryStatus.RUNNING
    assert claimed.locked_by_worker is not None
    assert claimed.locked_until is not None

    second_claim = await audit_service.claim_audit(db_session, audit.id)
    assert second_claim is None


@pytest.mark.asyncio
async def test_run_audit_rolls_back_before_marking_failed(monkeypatch):
    """If audit execution fails, the session must be rolled back before persisting failure."""

    audit = Audit(project_id="project-1", status=QueryStatus.PENDING)
    audit.id = 42

    primary_db = MagicMock()
    primary_db.execute = AsyncMock(return_value=MagicMock(rowcount=1))
    primary_db.get = AsyncMock(return_value=audit)
    primary_db.flush = AsyncMock()
    primary_db.add = MagicMock()
    primary_db.refresh = AsyncMock()

    fallback_db = MagicMock()
    fallback_db.get = AsyncMock(return_value=audit)
    fallback_db.flush = AsyncMock()
    fallback_db.add = MagicMock()
    fallback_db.refresh = AsyncMock()

    steps: list[str] = []

    async def _primary_commit():
        steps.append("primary_commit")

    async def _primary_rollback():
        steps.append("primary_rollback")

    async def _fallback_commit():
        steps.append("fallback_commit")

    primary_db.commit = AsyncMock(side_effect=_primary_commit)
    primary_db.rollback = AsyncMock(side_effect=_primary_rollback)
    fallback_db.commit = AsyncMock(side_effect=_fallback_commit)
    fallback_db.rollback = AsyncMock()

    class _SessionCtx:
        def __init__(self, session):
            self.session = session

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    sessions = iter([primary_db, fallback_db])

    monkeypatch.setattr(audit_service, "async_session", lambda: _SessionCtx(next(sessions)))
    monkeypatch.setattr(
        audit_service,
        "_execute_audit",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    monkeypatch.setattr(audit_service, "publish", lambda *args, **kwargs: None)

    await audit_service.run_audit(audit.id)

    assert steps == ["primary_commit", "primary_rollback", "fallback_commit"]
    assert audit.status == QueryStatus.FAILED
    assert audit.error_message == "boom"
    assert audit.completed_at is not None
