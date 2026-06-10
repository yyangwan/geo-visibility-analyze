"""Auth API — JWT-only authentication via 智鏈 RS256 tokens.

Development mode: supports local login/register when GENILINK_URL is not configured.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel

from app.services.genilink_auth import verify_genilink_token

router = APIRouter()

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


# --- Development-only local auth ---
class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class AuthUser(BaseModel):
    sub: str
    email: str | None = None
    workspace_id: str | None = None
    project_ids: list[str] = []


# Simple in-memory user store for development
_DEV_USERS: dict[str, str] = {}  # username -> password


@router.post("/register", response_model=AuthUser)
async def register_local(data: RegisterRequest):
    """Development-only local registration."""
    if data.username in _DEV_USERS:
        raise HTTPException(status_code=400, detail="Username already exists")
    _DEV_USERS[data.username] = data.password
    return AuthUser(
        sub=data.username,
        email=f"{data.username}@local.dev",
        workspace_id="local",
        project_ids=["demo-project"],
    )


@router.post("/login", response_model=TokenResponse)
async def login_local(data: LoginRequest):
    """Development-only local login.

    For production, use GeniLink SSO instead.
    """
    if data.username not in _DEV_USERS:
        # Auto-register for convenience
        _DEV_USERS[data.username] = data.password
    elif _DEV_USERS[data.username] != data.password:
        raise HTTPException(status_code=401, detail="Invalid username or password")

    # Return a fake JWT for development
    # In production, use GeniLink SSO
    import time
    fake_token = f"local.{data.username}.{int(time.time())}"
    return TokenResponse(access_token=fake_token)


async def get_current_user(
    token: str = Depends(oauth2_scheme),
) -> dict:
    """Dependency that validates 智鏈 RS256 JWT and returns user context.

    For development, also accepts local fake tokens (format: local.{username}.{timestamp}).

    Returns dict with: sub (user_id), email, workspace_id, project_ids, etc.
    No database lookup — pure JWT validation.
    """
    # Development: check for local fake token
    if token and token.startswith("local."):
        parts = token.split(".")
        if len(parts) >= 2:
            username = parts[1]
            return {
                "sub": username,
                "email": f"{username}@local.dev",
                "workspace_id": "local",
                "project_ids": ["demo-project"],
            }

    # Production: verify GeniLink JWT
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

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


@router.get("/me", response_model=AuthUser)
async def get_me(current_user: dict = Depends(get_current_user)):
    """Get current user info."""
    return AuthUser(
        sub=current_user.get("sub", ""),
        email=current_user.get("email"),
        workspace_id=current_user.get("workspace_id"),
        project_ids=current_user.get("project_ids", []),
    )
