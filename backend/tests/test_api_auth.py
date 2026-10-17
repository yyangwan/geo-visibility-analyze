"""Authentication contracts and the explicit development-only local boundary."""

from unittest.mock import AsyncMock

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


@pytest.mark.asyncio
@pytest.mark.parametrize("debug", [None, "0", "true", ""])
async def test_production_disables_local_identity_without_verifier_fallback(client, monkeypatch, debug):
    from app.api import auth

    if debug is None:
        monkeypatch.delenv("AISCOPE_DEBUG", raising=False)
    else:
        monkeypatch.setenv("AISCOPE_DEBUG", debug)
    verifier = AsyncMock(return_value={"sub": "should-not-be-used"})
    monkeypatch.setattr(auth, "verify_genilink_token", verifier)
    for path in ["register", "login"]:
        response = await client.post(f"/api/auth/{path}", json={"username": "alice", "password": "secret"})
        assert response.status_code == 404
    assert _DEV_USERS == {}
    response = await client.get("/api/auth/me", headers={"Authorization": "Bearer local.alice.123"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    verifier.assert_not_awaited()


@pytest.mark.asyncio
async def test_debug_explicitly_enables_local_registration_login_and_identity(client, monkeypatch):
    monkeypatch.setenv("AISCOPE_DEBUG", "1")
    response = await client.post("/api/auth/register", json={"username": "alice", "password": "secret"})
    assert response.status_code == 200
    login = await client.post("/api/auth/login", json={"username": "alice", "password": "secret"})
    assert login.status_code == 200
    identity = await client.get("/api/auth/me", headers={"Authorization": "Bearer " + login.json()["access_token"]})
    assert identity.status_code == 200 and identity.json()["sub"] == "alice"


@pytest.mark.asyncio
@pytest.mark.parametrize("debug", ["0", "1"])
async def test_signed_tokens_always_go_through_genilink_verification(client, monkeypatch, debug):
    from app.api import auth

    monkeypatch.setenv("AISCOPE_DEBUG", debug)
    verifier = AsyncMock(return_value={"sub": "portal-user", "workspace_id": "workspace", "project_ids": ["project"]})
    monkeypatch.setattr(auth, "verify_genilink_token", verifier)
    response = await client.get("/api/auth/me", headers={"Authorization": "Bearer signed.portal.token"})
    assert response.status_code == 200
    assert response.json()["sub"] == "portal-user"
    verifier.assert_awaited_once_with("signed.portal.token")
