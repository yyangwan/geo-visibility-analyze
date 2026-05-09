"""Tests for JWT authentication service."""

import time
from datetime import timedelta

from app.services.auth_service import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


class TestPasswordHashing:
    def test_hash_and_verify(self):
        hashed = hash_password("testpassword123")
        assert hashed != "testpassword123"
        assert verify_password("testpassword123", hashed) is True

    def test_wrong_password(self):
        hashed = hash_password("correct")
        assert verify_password("wrong", hashed) is False

    def test_different_hashes(self):
        h1 = hash_password("same")
        h2 = hash_password("same")
        # bcrypt generates different salts
        assert h1 != h2
        assert verify_password("same", h1) is True
        assert verify_password("same", h2) is True


class TestJWTTokens:
    def test_create_and_decode(self):
        token = create_access_token(data={"sub": "42"})
        payload = decode_access_token(token)
        assert payload is not None
        assert payload["sub"] == "42"

    def test_expired_token(self):
        token = create_access_token(
            data={"sub": "42"},
            expires_delta=timedelta(seconds=-1),
        )
        payload = decode_access_token(token)
        assert payload is None

    def test_tampered_token(self):
        token = create_access_token(data={"sub": "42"})
        tampered = token[:-5] + "XXXXX"
        payload = decode_access_token(tampered)
        assert payload is None

    def test_invalid_token(self):
        payload = decode_access_token("not.a.valid.token")
        assert payload is None
