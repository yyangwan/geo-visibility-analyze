import pytest
from httpx import AsyncClient
from unittest.mock import AsyncMock


@pytest.mark.asyncio
async def test_health_advertises_product_website_idempotency(client: AsyncClient, monkeypatch):
    class HealthySession:
        execute = AsyncMock()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

    monkeypatch.setattr("app.database.async_session", HealthySession)
    response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert "product_website_idempotency_v1" in response.json()["capabilities"]
