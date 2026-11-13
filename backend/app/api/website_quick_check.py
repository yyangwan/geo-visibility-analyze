import hmac
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.services.website_quick_check import check_website

router = APIRouter()


class QuickCheckRequest(BaseModel):
    requestId: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=2048)


@router.post("/v1", include_in_schema=False)
async def quick_check(
    body: QuickCheckRequest,
    authorization: Annotated[str | None, Header()] = None,
):
    secret = settings.website_quick_check_service_secret
    if not secret:
        raise HTTPException(status_code=503, detail="QUICK_CHECK_DISABLED")
    if not authorization or not hmac.compare_digest(
        authorization.encode(), f"Bearer {secret}".encode()
    ):
        raise HTTPException(status_code=401, detail="UNAUTHORIZED")
    return await check_website(body.url)
