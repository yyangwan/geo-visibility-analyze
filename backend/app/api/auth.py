"""Auth API — JWT-only authentication via 智鏈 RS256 tokens."""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.services.genilink_auth import verify_genilink_token

router = APIRouter()

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


async def get_current_user(
    token: str = Depends(oauth2_scheme),
) -> dict:
    """Dependency that validates 智鏈 RS256 JWT and returns user context.

    Returns dict with: sub (user_id), email, workspace_id, etc.
    No database lookup — pure JWT validation.
    """
    claims = await verify_genilink_token(token)
    if not claims:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return claims


async def get_optional_user(
    token: str | None = Depends(oauth2_scheme),
) -> dict | None:
    if not token:
        return None
    try:
        return await get_current_user(token)
    except HTTPException:
        return None
