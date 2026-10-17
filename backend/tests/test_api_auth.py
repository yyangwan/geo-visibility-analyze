"""Authentication API contracts; the implementation remains unchanged."""

import pytest

from app.api.auth import _DEV_USERS


@pytest.fixture(autouse=True)
def isolated_dev_users():
    saved = dict(_DEV_USERS)
    _DEV_USERS.clear()
    yield
    _DEV_USERS.clear()
    _DEV_USERS.update(saved)


@pytest.mark.asyncio
async def test_registration_identity_contract(client):
    response = await client.post(
        "/api/auth/register", json={"username": "alice", "password": "secret"}
    )
    assert response.status_code == 200
    assert response.json()["sub"] == "alice"
    assert response.json()["workspace_id"] == "local"
    duplicate = await client.post(
        "/api/auth/register", json={"username": "alice", "password": "other"}
    )
    assert duplicate.status_code == 400


@pytest.mark.asyncio
async def test_json_login_and_current_user(client):
    await client.post(
        "/api/auth/register", json={"username": "alice", "password": "secret"}
    )
    response = await client.post(
        "/api/auth/login", json={"username": "alice", "password": "secret"}
    )
    assert response.status_code == 200
    assert response.json()["token_type"] == "bearer"
    me = await client.get(
        "/api/auth/me",
        headers={"Authorization": "Bearer " + response.json()["access_token"]},
    )
    assert me.status_code == 200
    assert me.json()["sub"] == "alice"


@pytest.mark.asyncio
async def test_wrong_password_rejected(client):
    await client.post(
        "/api/auth/register", json={"username": "alice", "password": "secret"}
    )
    response = await client.post(
        "/api/auth/login", json={"username": "alice", "password": "wrong"}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_form_login_is_not_the_json_contract(client):
    response = await client.post(
        "/api/auth/login", data={"username": "alice", "password": "secret"}
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_current_user_requires_authentication(client):
    assert (await client.get("/api/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_invalid_signed_token_rejected(client, monkeypatch):
    from app.api import auth

    async def reject(_):
        return None

    monkeypatch.setattr(auth, "verify_genilink_token", reject)
    response = await client.get(
        "/api/auth/me", headers={"Authorization": "Bearer invalid.token"}
    )
    assert response.status_code == 401
