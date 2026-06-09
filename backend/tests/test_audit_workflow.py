"""Tests for persisted audit workflow state and SSE snapshots."""

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
)
from app.services import audit_service
from app.adapters.base import PlatformResponse


class _FakeAdapter:
    def __init__(self, platform_name: str):
        self.platform_name = platform_name


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
