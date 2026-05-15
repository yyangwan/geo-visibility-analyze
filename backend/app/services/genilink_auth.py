"""GeniLink SSO authentication — JWKS-based RS256 JWT validation."""

import httpx
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

GENILINK_JWKS_URL = "https://genilink.cn/.well-known/jwks.json"
GENILINK_ISSUER = "https://genilink.cn"
SERVICE_AUDIENCE = "visibility.genilink.cn"

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
        _jwks_cache_expires = time.time() + 3600  # 1 hour cache
        return _jwks_cache


async def verify_genilink_token(token: str) -> dict | None:
    """Verify a GeniLink RS256 JWT against the JWKS endpoint.

    Returns the decoded claims or None if invalid.
    """
    try:
        # Decode header to get kid
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")
        if not kid:
            return None

        # Fetch JWKS and find matching key
        jwks = await _fetch_jwks()
        rsa_key = None
        for key in jwks.get("keys", []):
            if key.get("kid") == kid:
                rsa_key = key
                break

        if not rsa_key:
            return None

        # Verify and decode
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


async def get_or_create_user_from_genilink(
    claims: dict, db: AsyncSession
) -> "User":
    """Auto-provision a local user from GeniLink JWT claims."""
    from app.models.models import User

    genilink_user_id = claims.get("sub")
    email = claims.get("email")

    # Find by genilink_user_id or email
    stmt = select(User)
    if genilink_user_id:
        stmt = stmt.where(User.genilink_user_id == genilink_user_id)
    if email and not genilink_user_id:
        stmt = stmt.where(User.username == email)

    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user:
        # Link if not yet linked
        if genilink_user_id and not user.genilink_user_id:
            user.genilink_user_id = genilink_user_id
            await db.commit()
        return user

    # Create new user from GeniLink claims
    import secrets

    user = User(
        username=email or f"genilink_{genilink_user_id[:8]}",
        hashed_password=secrets.token_hex(32),  # Random — no local login
        genilink_user_id=genilink_user_id,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user
