"""Tests for persisted audit workflow state and SSE snapshots."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.main import app
from app.models.models import (
    Audit,
    AuditEventLog,
    AuditPlatformRun,
    AuditStageRun,
    PlatformResponseRecord,
    Prompt,
    QueryResult,
    QueryStatus,
    ResponseAnalysis,
)
from app.services import audit_service
from app.services import response_analysis_service
from app.adapters.base import PlatformResponse


class _FakeAdapter:
    def __init__(self, platform_name: str):
        self.platform_name = platform_name

    def set_platform_config(self, config):
        self.config = config


class _FakeAnalysisSettings:
    analysis_timeout_seconds = 60

    def get_llm_config(self):
        return ("api-key", "https://example.com", "analysis-model")


MOCK_LLM_RESPONSE = {
    "brand_sentiment": "positive",
    "brand_attributes": ["性价比高"],
    "topics_covered": ["产品特点"],
    "answer_structure": "list",
    "competitor_refs": [],
    "cited_sources": [{"domain": "example.com", "authority_score": 4}],
}


@pytest.mark.asyncio
async def test_execute_audit_persists_workflow_state(db_session, monkeypatch):
    audit = Audit(
        project_id="project-1",
        status=QueryStatus.PENDING,
        platforms_json=["deepseek"],
        brands_json=[
            {
                "id": "brand-1",
                "name": "Alpha",
                "aliases": ["Alpha"],
                "is_competitor": False,
            }
        ],
    )
    db_session.add(audit)
    await db_session.flush()

    prompt = Prompt(project_id="project-1", text="What is Alpha?")
    db_session.add(prompt)
    await db_session.flush()

    monkeypatch.setattr(audit_service, "get_adapters", lambda platforms: [_FakeAdapter("deepseek")])
    monkeypatch.setattr(
        audit_service,
        "_query_platform",
        AsyncMock(
            return_value=[
                PlatformResponse(
                    platform="deepseek",
                    prompt=prompt.text,
                    response_text="Alpha is the best option.",
                    response_model="fake-model",
                    finish_reason="stop",
                    parse_error="ValueError: bad citation format",
                )
            ]
        ),
    )
    monkeypatch.setattr(audit_service, "publish", lambda *args, **kwargs: None)

    await audit_service._execute_audit(db_session, audit)

    assert audit.status == QueryStatus.COMPLETED

    stage_runs = (await db_session.execute(
        select(AuditStageRun).where(AuditStageRun.audit_id == audit.id)
    )).scalars().all()
    platform_runs = (await db_session.execute(
        select(AuditPlatformRun).where(AuditPlatformRun.audit_id == audit.id)
    )).scalars().all()
    events = (await db_session.execute(
        select(AuditEventLog).where(AuditEventLog.audit_id == audit.id)
    )).scalars().all()
    prrs = (await db_session.execute(
        select(PlatformResponseRecord).where(PlatformResponseRecord.audit_id == audit.id)
    )).scalars().all()
    results = (await db_session.execute(
        select(QueryResult).where(QueryResult.audit_id == audit.id)
    )).scalars().all()

    assert len(stage_runs) == 4
    assert len(platform_runs) == 1
    assert len(prrs) == 1
    assert len(results) == 1
    assert audit.analysis_run_id is not None
    assert prrs[0].parse_error == "ValueError: bad citation format"
    assert prrs[0].source_snapshot_hash is not None
    assert any(event.event_type == "audit_completed" for event in events)
    assert any(event.event_type == "platform_done" for event in events)


@pytest.mark.asyncio
async def test_execute_audit_overwrites_existing_platform_rows(db_session, monkeypatch):
    audit = Audit(
        project_id="project-1",
        status=QueryStatus.PENDING,
        platforms_json=["deepseek"],
        brands_json=[
            {
                "id": "brand-1",
                "name": "Alpha",
                "aliases": ["Alpha"],
                "is_competitor": False,
            }
        ],
    )
    db_session.add(audit)
    await db_session.flush()

    prompt = Prompt(project_id="project-1", text="What is Alpha?")
    db_session.add(prompt)
    await db_session.flush()

    existing_prr = PlatformResponseRecord(
        audit_id=audit.id,
        prompt_id=prompt.id,
        platform="deepseek",
        response_text="Old response",
        finish_reason="stop",
    )
    db_session.add(existing_prr)
    await db_session.flush()

    existing_result = QueryResult(
        audit_id=audit.id,
        prompt_id=prompt.id,
        brand_id="brand-1",
        platform="deepseek",
        response_text="Old response",
        response_record_id=existing_prr.id,
    )
    db_session.add(existing_result)
    await db_session.flush()

    monkeypatch.setattr(audit_service, "get_adapters", lambda platforms: [_FakeAdapter("deepseek")])
    monkeypatch.setattr(
        audit_service,
        "_query_platform",
        AsyncMock(
            return_value=[
                PlatformResponse(
                    platform="deepseek",
                    prompt=prompt.text,
                    response_text="Alpha is the best option.",
                    response_model="fake-model",
                    finish_reason="stop",
                )
            ]
        ),
    )
    monkeypatch.setattr(audit_service, "publish", lambda *args, **kwargs: None)

    await audit_service._execute_audit(db_session, audit)

    prrs = (await db_session.execute(
        select(PlatformResponseRecord).where(PlatformResponseRecord.audit_id == audit.id)
    )).scalars().all()
    results = (await db_session.execute(
        select(QueryResult).where(QueryResult.audit_id == audit.id)
    )).scalars().all()

    assert len(prrs) == 1
    assert prrs[0].response_text == "Alpha is the best option."
    assert len(results) == 1
    assert results[0].response_text == "Alpha is the best option."


@pytest.mark.asyncio
async def test_execute_audit_keeps_other_platforms_running_and_analysis_uses_prrs(
    db_session,
    monkeypatch,
):
    audit = Audit(
        project_id="project-3",
        status=QueryStatus.PENDING,
        platforms_json=["deepseek", "qwen"],
        brands_json=[
            {
                "id": "brand-1",
                "name": "Alpha",
                "aliases": ["Alpha"],
                "is_competitor": False,
            }
        ],
    )
    db_session.add(audit)
    await db_session.flush()

    prompt = Prompt(project_id="project-3", text="What is Alpha?")
    db_session.add(prompt)
    await db_session.flush()

    monkeypatch.setattr(
        audit_service,
        "get_adapters",
        lambda platforms: [_FakeAdapter("deepseek"), _FakeAdapter("qwen")],
    )

    async def _query_platform(platform_name, adapter, prompt_texts):
        if platform_name == "qwen":
            raise RuntimeError("qwen exploded")
        return [
            PlatformResponse(
                platform=platform_name,
                prompt=prompt_texts[0],
                response_text="Alpha is the best option.",
                response_model="fake-model",
                finish_reason="stop",
            )
        ]

    monkeypatch.setattr(audit_service, "_query_platform", _query_platform)
    monkeypatch.setattr(audit_service, "publish", lambda *args, **kwargs: None)

    await audit_service._execute_audit(db_session, audit)

    assert audit.status == QueryStatus.PARTIAL

    prrs = (await db_session.execute(
        select(PlatformResponseRecord).where(PlatformResponseRecord.audit_id == audit.id)
    )).scalars().all()
    results = (await db_session.execute(
        select(QueryResult).where(QueryResult.audit_id == audit.id)
    )).scalars().all()

    assert len(prrs) == 2
    assert sum(1 for prr in prrs if prr.error is None) == 1
    assert sum(1 for prr in prrs if prr.error is not None) == 1
    assert len(results) == 2

    monkeypatch.setattr(
        response_analysis_service,
        "_call_llm_for_analysis",
        AsyncMock(return_value=MOCK_LLM_RESPONSE),
    )
    monkeypatch.setattr(response_analysis_service, "settings", _FakeAnalysisSettings())

    @asynccontextmanager
    async def _same_session():
        yield db_session

    monkeypatch.setattr(response_analysis_service, "async_session", _same_session)

    await response_analysis_service.run_analysis_for_audit(audit.id)

    analyses = (await db_session.execute(
        select(ResponseAnalysis).join(
            PlatformResponseRecord,
            ResponseAnalysis.response_record_id == PlatformResponseRecord.id,
        ).where(PlatformResponseRecord.audit_id == audit.id)
    )).scalars().all()

    assert len(analyses) == 1
    assert analyses[0].status == "completed"
    assert analyses[0].analysis_model == "analysis-model"
    assert analyses[0].brand_sentiment == "positive"


@pytest.mark.asyncio
async def test_audit_events_streams_snapshot_before_done(client, db_session, monkeypatch):
    audit = Audit(
        project_id="project-2",
        status=QueryStatus.COMPLETED,
        platforms_json=["deepseek"],
        brands_json=[],
    )
    db_session.add(audit)
    await db_session.flush()

    monkeypatch.setattr(
        audit_service, "publish", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "app.api.audits.verify_genilink_token",
        AsyncMock(return_value={"scope": "project", "pid": "project-2"}),
    )

    try:
        async with client.stream(
            "GET",
            f"/api/audits/{audit.id}/events?token=test-token",
        ) as resp:
            body = await resp.aread()
    finally:
        pass

    assert resp.status_code == 200
    text = body.decode()
    assert "event: audit_snapshot" in text
    assert "event: audit_done" in text
