"""GeniLink JWT authentication — JWKS-based RS256 validation.

No local user provisioning — pure JWT validation.
User context comes entirely from 智鏈 JWT claims.
"""

import os

import httpx
from jose import JWTError, jwt

GENILINK_JWKS_URL = os.getenv(
    "GENILINK_JWKS_URL",
    "https://app.genilink.cn/.well-known/jwks.json",
)
GENILINK_ISSUER = os.getenv("GENILINK_ISSUER", "https://app.genilink.cn")
SERVICE_AUDIENCE = os.getenv("GENILINK_AUDIENCE", "visibility.genilink.cn")

# Cache JWKS for 1 hour
_jwks_cache: dict | None = None
_jwks_cache_expires: float = 0


async def _fetch_jwks() -> dict:
    """Fetch JWKS from GeniLink portal with caching."""
    global _jwks_cache, _jwks_cache_expires
    import time

    if _jwks_cache and time.time() < _jwks_cache_expires:
        return _jwks_cache

    async with httpx.AsyncClient() as client:
        resp = await client.get(GENILINK_JWKS_URL)
        resp.raise_for_status()
        _jwks_cache = resp.json()
        _jwks_cache_expires = time.time() + 3600
        return _jwks_cache


async def verify_genilink_token(token: str) -> dict | None:
    """Verify a GeniLink RS256 JWT against the JWKS endpoint.

    Returns the decoded claims or None if invalid.
    """
    try:
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")
        if not kid:
            return None

        jwks = await _fetch_jwks()
        rsa_key = next((key for key in jwks.get("keys", []) if key.get("kid") == kid), None)

        # If the key was rotated, refresh once before giving up.
        if rsa_key is None:
            global _jwks_cache, _jwks_cache_expires
            _jwks_cache = None
            _jwks_cache_expires = 0
            jwks = await _fetch_jwks()
            rsa_key = next((key for key in jwks.get("keys", []) if key.get("kid") == kid), None)

        if not rsa_key:
            return None

        payload = jwt.decode(
            token,
            rsa_key,
            algorithms=["RS256"],
            issuer=GENILINK_ISSUER,
            audience=SERVICE_AUDIENCE,
        )
        return payload
    except JWTError:
        return None
