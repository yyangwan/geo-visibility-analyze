"""API integration tests — auth endpoints."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
class TestRegister:
    async def test_register_success(self, client: AsyncClient):
        resp = await client.post("/api/auth/register", json={
            "username": "alice",
            "password": "secret123",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "alice"
        assert "id" in data

    async def test_register_duplicate(self, client: AsyncClient):
        await client.post("/api/auth/register", json={
            "username": "bob", "password": "pass1",
        })
        resp = await client.post("/api/auth/register", json={
            "username": "bob", "password": "pass2",
        })
        assert resp.status_code == 400
        assert "already exists" in resp.json()["detail"]


@pytest.mark.asyncio
class TestLogin:
    async def test_login_success(self, client: AsyncClient):
        await client.post("/api/auth/register", json={
            "username": "carol", "password": "pw123",
        })
        resp = await client.post("/api/auth/login", data={
            "username": "carol", "password": "pw123",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    async def test_login_wrong_password(self, client: AsyncClient):
        await client.post("/api/auth/register", json={
            "username": "dave", "password": "right",
        })
        resp = await client.post("/api/auth/login", data={
            "username": "dave", "password": "wrong",
        })
        assert resp.status_code == 401

    async def test_login_nonexistent_user(self, client: AsyncClient):
        resp = await client.post("/api/auth/login", data={
            "username": "ghost", "password": "nop",
        })
        assert resp.status_code == 401


@pytest.mark.asyncio
class TestMe:
    async def test_me_with_valid_token(self, client: AsyncClient):
        await client.post("/api/auth/register", json={
            "username": "eve", "password": "pw",
        })
        login_resp = await client.post("/api/auth/login", data={
            "username": "eve", "password": "pw",
        })
        token = login_resp.json()["access_token"]
        resp = await client.get("/api/auth/me", headers={
            "Authorization": f"Bearer {token}",
        })
        assert resp.status_code == 200
        assert resp.json()["username"] == "eve"

    async def test_me_without_token(self, client: AsyncClient):
        resp = await client.get("/api/auth/me")
        assert resp.status_code == 401

    async def test_me_with_invalid_token(self, client: AsyncClient):
        resp = await client.get("/api/auth/me", headers={
            "Authorization": "Bearer invalid.token.here",
        })
        assert resp.status_code == 401


# --- Helper ---

async def _register_and_login(client: AsyncClient, username: str = "tester", password: str = "pw") -> str:
    """Register a user and return their JWT token."""
    await client.post("/api/auth/register", json={
        "username": username, "password": password,
    })
    resp = await client.post("/api/auth/login", data={
        "username": username, "password": password,
    })
    return resp.json()["access_token"]
