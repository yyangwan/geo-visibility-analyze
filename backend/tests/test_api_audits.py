"""API integration tests for audit creation and recovery behavior."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app.api.auth import get_current_user
from app.main import app
from app.models.models import Audit, QueryStatus


@pytest.mark.asyncio
class TestAudits:
    async def test_create_audit_starts_in_pending_state(self, client: AsyncClient, db_session):
        project_id = "proj-audit-test"

        async def override_current_user():
            return {"scope": "project", "pid": project_id}

        app.dependency_overrides[get_current_user] = override_current_user

        try:
            with patch("app.api.audits.run_audit", new=AsyncMock(return_value=None)):
                resp = await client.post(
                    "/api/audits",
                    json={"project_id": project_id, "platforms": ["deepseek"], "brands": []},
                )
        finally:
            app.dependency_overrides.pop(get_current_user, None)

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == QueryStatus.PENDING.value

        audit = await db_session.get(Audit, data["id"])
        assert audit is not None
        assert audit.status == QueryStatus.PENDING
