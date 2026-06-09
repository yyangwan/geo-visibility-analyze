"""Tests for GeniLink access helpers."""

import pytest
from fastapi import HTTPException

from app.api.access import require_project_scope, require_workspace_scope
from app.services import genilink_auth


def test_require_project_scope_accepts_exact_pid():
    require_project_scope({"scope": "project", "pid": "project-1"}, "project-1")


def test_require_project_scope_rejects_workspace_token():
    with pytest.raises(HTTPException) as exc:
        require_project_scope({"scope": "workspace", "pid": "project-1"}, "project-1")
    assert exc.value.status_code == 403


def test_require_workspace_scope_rejects_project_token():
    with pytest.raises(HTTPException) as exc:
        require_workspace_scope({"scope": "project", "pid": "project-1"})
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_verify_genilink_token_refreshes_jwks_on_unknown_kid(monkeypatch):
    calls = {"fetch": 0, "decode": 0}

    async def fake_fetch_jwks():
        calls["fetch"] += 1
        if calls["fetch"] == 1:
            return {"keys": [{"kid": "old-kid"}]}
        return {"keys": [{"kid": "rotated-kid"}]}

    def fake_get_unverified_header(token: str):
        return {"kid": "rotated-kid"}

    def fake_decode(token, key, algorithms, issuer, audience):
        calls["decode"] += 1
        assert key == {"kid": "rotated-kid"}
        assert algorithms == ["RS256"]
        assert issuer == genilink_auth.GENILINK_ISSUER
        assert audience == genilink_auth.SERVICE_AUDIENCE
        return {"sub": "user-1", "scope": "project", "pid": "project-1"}

    monkeypatch.setattr(genilink_auth, "_jwks_cache", None)
    monkeypatch.setattr(genilink_auth, "_jwks_cache_expires", 0)
    monkeypatch.setattr(genilink_auth, "_fetch_jwks", fake_fetch_jwks)
    monkeypatch.setattr(genilink_auth.jwt, "get_unverified_header", fake_get_unverified_header)
    monkeypatch.setattr(genilink_auth.jwt, "decode", fake_decode)

    payload = await genilink_auth.verify_genilink_token("token")

    assert payload == {"sub": "user-1", "scope": "project", "pid": "project-1"}
    assert calls["fetch"] == 2
    assert calls["decode"] == 1
