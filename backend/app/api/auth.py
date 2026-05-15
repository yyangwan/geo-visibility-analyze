"""Auth API — GeniLink SSO + local auth (migration period)."""

import os
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import UserOut, UserRegister
from app.database import get_db
from app.models.models import User
from app.services.auth_service import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from app.services.genilink_auth import (
    get_or_create_user_from_genilink,
    verify_genilink_token,
)

router = APIRouter()

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Dependency that extracts and validates the current user from JWT.

    Supports both GeniLink RS256 JWTs and legacy HS256 local tokens.
    """
    # Try GeniLink RS256 JWT first
    claims = await verify_genilink_token(token)
    if claims:
        return await get_or_create_user_from_genilink(claims, db)

    # Fallback: legacy local HS256 token
    payload = decode_access_token(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )
    user = await db.get(User, int(user_id))
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )
    return user


# Optional dependency — returns None if no token provided
async def get_optional_user(
    token: str | None = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User | None:
    if not token:
        return None
    try:
        return await get_current_user(token, db)
    except HTTPException:
        return None


@router.post("/register", response_model=UserOut)
async def register(
    data: UserRegister,
    db: AsyncSession = Depends(get_db),
):
    """Register a new user."""
    existing = await db.execute(
        select(User).where(User.username == data.username)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Username already exists")

    user = User(
        username=data.username,
        hashed_password=hash_password(data.password),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/login")
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """Login and get JWT token."""
    result = await db.execute(
        select(User).where(User.username == form_data.username)
    )
    user = result.scalar_one_or_none()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
        )
    if not user.is_active:
        raise HTTPException(status_code=400, detail="User is inactive")

    token = create_access_token(data={"sub": str(user.id)})
    return {"access_token": token, "token_type": "bearer"}


@router.get("/me", response_model=UserOut)
async def get_me(current_user: User = Depends(get_current_user)):
    """Get current user info."""
    return current_user


@router.get("/sso/callback")
async def sso_callback(
    code: str,
    db: AsyncSession = Depends(get_db),
):
    """Exchange GeniLink authorization code for a local JWT.

    Called by the frontend SSO callback view.
    """
    genilink_url = os.getenv("GENILINK_URL", "https://genilink.cn")
    client_secret = os.getenv("GENILINK_CLIENT_SECRET")

    if not client_secret:
        raise HTTPException(status_code=500, detail="SSO not configured")

    frontend_origin = os.getenv("FRONTEND_URL", "http://localhost:5173")
    redirect_uri = f"{frontend_origin}/sso/callback"

    # Exchange code for GeniLink RS256 JWT
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{genilink_url}/api/auth/sso/token",
            json={
                "code": code,
                "service": "visibility",
                "redirect_uri": redirect_uri,
                "client_secret": client_secret,
            },
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=401, detail="Code exchange failed")
        data = resp.json()

    genilink_token = data["access_token"]

    # Verify the GeniLink JWT
    claims = await verify_genilink_token(genilink_token)
    if not claims:
        raise HTTPException(status_code=401, detail="Invalid GeniLink token")

    # Auto-provision user
    user = await get_or_create_user_from_genilink(claims, db)

    # Issue a local HS256 JWT for the frontend
    local_token = create_access_token(data={"sub": str(user.id)})

    return {"access_token": local_token, "token_type": "bearer"}
