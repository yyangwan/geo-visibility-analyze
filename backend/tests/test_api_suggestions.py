"""API tests for suggestion report/audit scoping."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.main import app
from app.models.models import Audit, QueryStatus, Report, Suggestion


@pytest.mark.asyncio
async def test_list_suggestions_defaults_to_latest_report(
    client: AsyncClient,
    db_session: AsyncSession,
):
    project_id = "project-suggestions"
    old_audit, old_report, latest_audit, latest_report = await _seed_reports(db_session, project_id)
    db_session.add_all(
        [
            Suggestion(
                project_id=project_id,
                report_id=old_report.id,
                category="content_optimization",
                title="Old suggestion",
                description="Old audit suggestion",
                priority="low",
            ),
            Suggestion(
                project_id=project_id,
                report_id=latest_report.id,
                category="platform_focus",
                title="Latest suggestion",
                description="Latest audit suggestion",
                priority="high",
            ),
        ]
    )
    await db_session.commit()

    async def override_current_user():
        return {"scope": "project", "pid": project_id}

    app.dependency_overrides[get_current_user] = override_current_user
    try:
        resp = await client.get(f"/api/suggestions/{project_id}")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 200
    data = resp.json()
    assert [item["title"] for item in data] == ["Latest suggestion"]
    assert data[0]["report_id"] == latest_report.id
    assert data[0]["audit_id"] == latest_audit.id
    assert old_audit.id != latest_audit.id


@pytest.mark.asyncio
async def test_list_suggestions_can_filter_by_audit_or_report(
    client: AsyncClient,
    db_session: AsyncSession,
):
    project_id = "project-suggestions-filter"
    old_audit, old_report, _latest_audit, latest_report = await _seed_reports(db_session, project_id)
    db_session.add_all(
        [
            Suggestion(
                project_id=project_id,
                report_id=old_report.id,
                category="content_optimization",
                title="Old suggestion",
                description="Old audit suggestion",
                priority="low",
            ),
            Suggestion(
                project_id=project_id,
                report_id=latest_report.id,
                category="platform_focus",
                title="Latest suggestion",
                description="Latest audit suggestion",
                priority="high",
            ),
        ]
    )
    await db_session.commit()

    async def override_current_user():
        return {"scope": "project", "pid": project_id}

    app.dependency_overrides[get_current_user] = override_current_user
    try:
        by_audit = await client.get(f"/api/suggestions/{project_id}?audit_id={old_audit.id}")
        by_report = await client.get(f"/api/suggestions/{project_id}?report_id={latest_report.id}")
        all_items = await client.get(f"/api/suggestions/{project_id}?latest=false")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert by_audit.status_code == 200
    assert [item["title"] for item in by_audit.json()] == ["Old suggestion"]
    assert by_audit.json()[0]["audit_id"] == old_audit.id

    assert by_report.status_code == 200
    assert [item["title"] for item in by_report.json()] == ["Latest suggestion"]

    assert all_items.status_code == 200
    assert {item["title"] for item in all_items.json()} == {"Old suggestion", "Latest suggestion"}


@pytest.mark.asyncio
async def test_generate_suggestions_returns_audit_id(
    client: AsyncClient,
    db_session: AsyncSession,
):
    project_id = "project-suggestions-generate"
    _old_audit, _old_report, latest_audit, latest_report = await _seed_reports(db_session, project_id)

    async def override_current_user():
        return {"scope": "project", "pid": project_id}

    async def fake_generate(_db, _report):
        assert _report.id == latest_report.id
        suggestion = Suggestion(
            project_id=project_id,
            report_id=latest_report.id,
            category="platform_focus",
            title="Generated suggestion",
            description="Generated from latest audit",
            priority="high",
        )
        db_session.add(suggestion)
        await db_session.commit()
        await db_session.refresh(suggestion)
        return [suggestion]

    app.dependency_overrides[get_current_user] = override_current_user
    try:
        with patch("app.api.suggestions.generate_suggestions", new=AsyncMock(side_effect=fake_generate)):
            resp = await client.post(f"/api/suggestions/{project_id}/generate")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["title"] == "Generated suggestion"
    assert data[0]["report_id"] == latest_report.id
    assert data[0]["audit_id"] == latest_audit.id


async def _seed_reports(
    db: AsyncSession,
    project_id: str,
) -> tuple[Audit, Report, Audit, Report]:
    now = datetime(2026, 6, 20, 10, 0, 0)
    old_audit = Audit(
        project_id=project_id,
        status=QueryStatus.COMPLETED,
        platforms_json=["deepseek"],
        brands_json=[],
        created_at=now,
    )
    latest_audit = Audit(
        project_id=project_id,
        status=QueryStatus.COMPLETED,
        platforms_json=["deepseek"],
        brands_json=[],
        created_at=now + timedelta(hours=1),
    )
    db.add_all([old_audit, latest_audit])
    await db.flush()

    old_report = Report(
        project_id=project_id,
        audit_id=old_audit.id,
        overall_score=20,
        mention_rate=0.2,
        platform_scores={"deepseek": 20},
        insights=[],
        created_at=now,
    )
    latest_report = Report(
        project_id=project_id,
        audit_id=latest_audit.id,
        overall_score=80,
        mention_rate=0.8,
        platform_scores={"deepseek": 80},
        insights=[],
        created_at=now + timedelta(hours=1),
    )
    db.add_all([old_report, latest_report])
    await db.flush()
    return old_audit, old_report, latest_audit, latest_report
