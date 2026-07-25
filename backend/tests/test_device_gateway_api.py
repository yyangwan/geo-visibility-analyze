from datetime import datetime

import pytest
from httpx import AsyncClient

from app.api.auth import get_current_user
from app.config import settings
from app.main import app


GATEWAY_HEADERS = {
    "Authorization": "Bearer test-device-gateway-token",
    "X-Gateway-Id": "CHAO",
}


@pytest.fixture(autouse=True)
def configure_gateway(monkeypatch):
    monkeypatch.setattr(
        settings,
        "device_gateway_token",
        "test-device-gateway-token",
    )
    monkeypatch.setattr(settings, "device_gateway_ids", "CHAO")
    monkeypatch.setattr(settings, "device_gateway_lease_seconds", 120)


def use_project(project_id: str):
    async def current_user():
        return {"sub": "user-1", "scope": "project", "pid": project_id}

    app.dependency_overrides[get_current_user] = current_user


@pytest.mark.asyncio
async def test_gateway_requires_token_and_allowed_id(client: AsyncClient):
    response = await client.post("/api/device-gateway/heartbeat", json={})
    assert response.status_code == 401

    response = await client.post(
        "/api/device-gateway/heartbeat",
        headers={
            "Authorization": "Bearer test-device-gateway-token",
            "X-Gateway-Id": "UNKNOWN",
        },
        json={},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_task_lifecycle_and_idempotency(client: AsyncClient):
    use_project("project-1")
    task_body = {
        "project_id": "project-1",
        "task_type": "gateway.healthcheck",
        "target_gateway_id": "CHAO",
        "payload": {"echo": "ready"},
        "idempotency_key": "healthcheck-1",
    }

    created = await client.post("/api/device-tasks", json=task_body)
    assert created.status_code == 201
    task_id = created.json()["id"]

    duplicate = await client.post("/api/device-tasks", json=task_body)
    assert duplicate.status_code == 201
    assert duplicate.json()["id"] == task_id

    heartbeat = await client.post(
        "/api/device-gateway/heartbeat",
        headers=GATEWAY_HEADERS,
        json={
            "status": "online",
            "capabilities": {"taskTypes": ["gateway.healthcheck"]},
            "device_snapshot": {"devices": []},
            "agent_version": "0.1.0",
        },
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["gateway_id"] == "CHAO"

    claimed = await client.post(
        "/api/device-gateway/tasks/claim",
        headers=GATEWAY_HEADERS,
        json={"capabilities": ["gateway.healthcheck"]},
    )
    assert claimed.status_code == 200
    claimed_data = claimed.json()
    assert claimed_data["task"]["id"] == task_id
    assert claimed_data["task"]["status"] == "leased"
    assert claimed_data["lease_token"]
    datetime.fromisoformat(claimed_data["task"]["lease_expires_at"])

    rejected = await client.post(
        f"/api/device-gateway/tasks/{task_id}/complete",
        headers=GATEWAY_HEADERS,
        json={"lease_token": "x" * 32, "result": {}},
    )
    assert rejected.status_code == 409

    renewed = await client.post(
        f"/api/device-gateway/tasks/{task_id}/heartbeat",
        headers=GATEWAY_HEADERS,
        json={"lease_token": claimed_data["lease_token"]},
    )
    assert renewed.status_code == 200

    completed = await client.post(
        f"/api/device-gateway/tasks/{task_id}/complete",
        headers=GATEWAY_HEADERS,
        json={
            "lease_token": claimed_data["lease_token"],
            "result": {"echo": "ready", "gateway": "CHAO"},
        },
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"

    fetched = await client.get(f"/api/device-tasks/{task_id}")
    assert fetched.status_code == 200
    assert fetched.json()["result"]["gateway"] == "CHAO"


@pytest.mark.asyncio
async def test_capability_filter_and_retry(client: AsyncClient):
    use_project("project-2")
    response = await client.post(
        "/api/device-tasks",
        json={
            "project_id": "project-2",
            "task_type": "appium.prompt",
            "platform": "doubao",
            "surface": "app",
            "payload": {"prompt": "请推荐一款咖啡机"},
            "max_attempts": 2,
        },
    )
    task_id = response.json()["id"]

    no_task = await client.post(
        "/api/device-gateway/tasks/claim",
        headers=GATEWAY_HEADERS,
        json={"capabilities": ["gateway.healthcheck"]},
    )
    assert no_task.status_code == 200
    assert no_task.json() == {"task": None, "lease_token": None}

    claimed = await client.post(
        "/api/device-gateway/tasks/claim",
        headers=GATEWAY_HEADERS,
        json={"capabilities": ["appium.prompt"]},
    )
    lease_token = claimed.json()["lease_token"]

    failed = await client.post(
        f"/api/device-gateway/tasks/{task_id}/fail",
        headers=GATEWAY_HEADERS,
        json={
            "lease_token": lease_token,
            "error_code": "device_busy",
            "error_message": "Device is busy",
            "retryable": True,
            "retry_after_seconds": 0,
        },
    )
    assert failed.status_code == 200
    assert failed.json()["status"] == "pending"

    reclaimed = await client.post(
        "/api/device-gateway/tasks/claim",
        headers=GATEWAY_HEADERS,
        json={"capabilities": ["appium.prompt"]},
    )
    assert reclaimed.json()["task"]["attempt_count"] == 2

    final_failure = await client.post(
        f"/api/device-gateway/tasks/{task_id}/fail",
        headers=GATEWAY_HEADERS,
        json={
            "lease_token": reclaimed.json()["lease_token"],
            "error_code": "automation_failed",
            "error_message": "Selector changed",
            "retryable": True,
            "retry_after_seconds": 0,
        },
    )
    assert final_failure.json()["status"] == "failed"


@pytest.mark.asyncio
async def test_task_validation_and_project_isolation(client: AsyncClient):
    use_project("project-3")
    invalid = await client.post(
        "/api/device-tasks",
        json={
            "project_id": "project-3",
            "task_type": "appium.prompt",
            "platform": "deepseek",
            "surface": "web",
            "payload": {},
        },
    )
    assert invalid.status_code == 422

    forbidden = await client.post(
        "/api/device-tasks",
        json={
            "project_id": "another-project",
            "task_type": "gateway.healthcheck",
        },
    )
    assert forbidden.status_code == 403
