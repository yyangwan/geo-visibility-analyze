"""Project ownership is Portal-issued scope; retired local CRUD must stay absent."""

import pytest

from app.api.auth import get_current_user
from app.main import app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/projects"),
        ("POST", "/api/projects"),
        ("GET", "/api/projects/1"),
        ("POST", "/api/projects/1/brands"),
    ],
)
async def test_local_project_crud_is_retired(client, method, path):
    response = await client.request(method, path, json={"name": "obsolete"})
    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user",
    [
        {"scope": "workspace", "pid": "p1"},
        {"scope": "project", "pid": "other"},
        {"sub": "local-user", "project_ids": ["p1"]},
    ],
)
async def test_summary_rejects_wrong_scope(client, user):
    async def identity():
        return user

    app.dependency_overrides[get_current_user] = identity
    try:
        response = await client.get("/api/integration/summary?project_id=p1")
        assert response.status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_summary_accepts_exact_project_token(client):
    async def identity():
        return {"scope": "project", "pid": "p1", "role": "viewer"}

    app.dependency_overrides[get_current_user] = identity
    try:
        response = await client.get("/api/integration/summary?project_id=p1")
        assert response.status_code == 200
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_summary_rejects_missing_auth(client):
    assert (
        await client.get("/api/integration/summary?project_id=p1")
    ).status_code == 401
