from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import website_quick_check as quick_check_api
from app.intelligence.sources import CapturedSource, SourceUnavailable
from app.services import website_quick_check


@pytest.mark.asyncio
async def test_quick_check_returns_page_evidence(monkeypatch):
    html = (
        '<html><head><title>测试官网</title><script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"Organization","name":"测试官网"}'
        '</script></head><body><p>公开产品正文</p></body></html>'
    )
    text = "这是一段官网正文，内容介绍面向连锁餐饮企业的排班工具以及员工工时管理。"
    capture = AsyncMock(return_value=CapturedSource(
        "https://example.com/", 200, html.encode(), text, "unknown",
    ))
    monkeypatch.setattr(website_quick_check, "capture_source", capture)

    output = await website_quick_check.check_website("https://example.com/")

    assert output["ruleVersion"] == "website-quick-check-v1"
    assert output["finalHost"] == "example.com"
    by_key = {item["key"]: item for item in output["checks"]}
    assert by_key["title"]["evidence"] == "测试官网"
    assert by_key["content"]["evidence"] == text
    assert by_key["structured_data"]["evidence"] == "Organization"
    capture.assert_awaited_once_with(
        "https://example.com/", byte_limit=2 * 1024 * 1024,
        timeout_seconds=10, max_redirects=3, html_only=True,
        user_agent="GeniLinkWebsiteCheck/1.0",
    )


@pytest.mark.asyncio
async def test_blocked_access_does_not_infer_missing_content(monkeypatch):
    monkeypatch.setattr(website_quick_check, "capture_source", AsyncMock(
        side_effect=SourceUnavailable("blocked", 403),
    ))
    output = await website_quick_check.check_website("https://example.com/")
    assert {item["status"] for item in output["checks"]} == {"unknown"}
    assert output["priorityFinding"]["key"] == "access"


@pytest.mark.asyncio
async def test_script_shell_is_unknown(monkeypatch):
    monkeypatch.setattr(website_quick_check, "capture_source", AsyncMock(
        return_value=CapturedSource("https://example.com/", 200, b"<div id='root'></div>", "", "unknown"),
    ))
    output = await website_quick_check.check_website("https://example.com/")
    by_key = {item["key"]: item for item in output["checks"]}
    assert by_key["access"]["status"] == "pass"
    assert by_key["content"]["status"] == "unknown"
    assert by_key["structured_data"]["status"] == "unknown"


def test_internal_route_requires_configured_service_credential(monkeypatch):
    app = FastAPI()
    app.include_router(quick_check_api.router)
    check = AsyncMock(return_value={"ruleVersion": "website-quick-check-v1"})
    monkeypatch.setattr(quick_check_api, "check_website", check)
    request = {"requestId": "test", "url": "https://example.com/"}
    with TestClient(app) as client:
        monkeypatch.setattr(quick_check_api.settings, "website_quick_check_service_secret", "")
        assert client.post("/v1", json=request).status_code == 503
        monkeypatch.setattr(quick_check_api.settings, "website_quick_check_service_secret", "test-only")
        assert client.post("/v1", json=request).status_code == 401
        check.assert_not_awaited()
        response = client.post("/v1", json=request, headers={"Authorization": "Bearer test-only"})
        assert response.status_code == 200
        check.assert_awaited_once_with("https://example.com/")
