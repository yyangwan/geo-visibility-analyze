from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


TaskType = Literal["gateway.healthcheck", "appium.prompt"]
TaskSurface = Literal["web", "app"]


class DeviceTaskCreate(BaseModel):
    project_id: str = Field(min_length=1, max_length=50)
    task_type: TaskType
    target_gateway_id: str | None = Field(default=None, max_length=100)
    platform: str | None = Field(default=None, max_length=50)
    surface: TaskSurface | None = None
    payload: dict = Field(default_factory=dict)
    priority: int = Field(default=0, ge=-100, le=100)
    max_attempts: int = Field(default=3, ge=1, le=10)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_target(self):
        if self.task_type == "appium.prompt":
            if not self.platform or not self.surface:
                raise ValueError("appium.prompt requires platform and surface")
            prompt = self.payload.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValueError("appium.prompt requires a non-empty payload.prompt")
        return self


class DeviceTaskOut(BaseModel):
    id: str
    project_id: str
    target_gateway_id: str | None = None
    gateway_id: str | None = None
    task_type: str
    platform: str | None = None
    surface: str | None = None
    payload: dict
    status: str
    priority: int
    idempotency_key: str | None = None
    attempt_count: int
    max_attempts: int
    result: dict | None = None
    error_code: str | None = None
    error_message: str | None = None
    available_at: datetime
    lease_expires_at: datetime | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime

    model_config = {"from_attributes": True}


class GatewayHeartbeatRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    status: Literal["online", "degraded"] = "online"
    capabilities: dict = Field(default_factory=dict)
    device_snapshot: dict = Field(default_factory=dict)
    agent_version: str | None = Field(default=None, max_length=50)


class GatewayHeartbeatResponse(BaseModel):
    gateway_id: str
    server_time: datetime
    lease_seconds: int


class GatewayClaimRequest(BaseModel):
    capabilities: list[str] = Field(default_factory=list, max_length=100)


class GatewayClaimedTask(BaseModel):
    task: DeviceTaskOut | None = None
    lease_token: str | None = None


class GatewayLeaseRequest(BaseModel):
    lease_token: str = Field(min_length=32, max_length=200)


class GatewayCompleteRequest(GatewayLeaseRequest):
    result: dict = Field(default_factory=dict)


class GatewayFailRequest(GatewayLeaseRequest):
    error_code: str = Field(min_length=1, max_length=100)
    error_message: str = Field(default="", max_length=10000)
    retryable: bool = False
    retry_after_seconds: int = Field(default=30, ge=0, le=3600)
