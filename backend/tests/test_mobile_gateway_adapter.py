from types import SimpleNamespace

import pytest

from app.adapters.base import ErrorCode
from app.adapters.mobile_gateway import MobileGatewayAdapter, mobile_capture_enabled
from app.config import settings
from app.services.audit_service import _build_persisted_citations
from app.services.source_extraction import ExtractedSource


def test_mobile_capture_switch_respects_platforms_and_config(monkeypatch):
    monkeypatch.setattr(settings, "mobile_app_capture_enabled", True)
    monkeypatch.setattr(
        settings,
        "mobile_app_capture_platforms",
        "deepseek,hunyuan,qwen",
    )

    assert mobile_capture_enabled("deepseek")
    assert mobile_capture_enabled("hunyuan")
    assert not mobile_capture_enabled("kimi")
    assert not mobile_capture_enabled(
        "qwen",
        {"mobile_gateway": {"enabled": False}},
    )


@pytest.mark.asyncio
async def test_mobile_result_normalizes_to_platform_response(monkeypatch):
    adapter = MobileGatewayAdapter("hunyuan")
    adapter.set_runtime_context(
        {
            "analysis_run_id": "run-1",
            "audit_id": 7,
            "project_id": "project-1",
        }
    )
    task = SimpleNamespace(id="task-1")
    completed = SimpleNamespace(
        id="task-1",
        status="completed",
        result={
            "platform": "yuanbao",
            "surface": "app",
            "package_name": "com.tencent.hunyuan.app.chat",
            "app_version": "2.78.0",
            "device_serial": "device-1",
            "answer": "DeepSeek 由深度求索开发。",
            "answer_urls": ["https://www.deepseek.com/"],
            "reference_count": 2,
            "source_count": 2,
            "source_success_count": 1,
            "source_failure_count": 1,
            "source_completeness": 0.5,
            "capture_status": "partial",
            "duration_ms": 12345,
            "source_collection_duration_ms": 900,
            "sources": [
                {
                    "index": 1,
                    "site_name": "深度求索",
                    "title": "DeepSeek | 深度求索",
                    "domain": "www.deepseek.com",
                    "url": "https://www.deepseek.com/",
                    "url_resolution": "exact",
                    "status": "collected",
                },
                {
                    "index": 2,
                    "site_name": "某新闻",
                    "title": "DeepSeek 公司介绍",
                    "domain": "",
                    "url": "",
                    "url_resolution": "unavailable",
                    "status": "collected",
                },
            ],
        },
        error_message=None,
        error_code=None,
        attempt_count=1,
    )

    async def enqueue(prompt):
        return task

    async def wait(task_id):
        return completed

    monkeypatch.setattr(adapter, "_enqueue_task", enqueue)
    monkeypatch.setattr(adapter, "_wait_for_task", wait)

    response = (await adapter.query(["问题"]))[0]

    assert response.success
    assert response.platform == "hunyuan"
    assert response.response_text == "DeepSeek 由深度求索开发。"
    assert response.latency_ms == 12345
    assert response.response_model == "app:2.78.0"
    assert response.search_enabled is True
    assert response.citations[0]["url"] == "https://www.deepseek.com/"
    assert response.citations[1]["site_name"] == "某新闻"
    assert response.citations[1]["url_resolution"] == "unavailable"
    assert response.raw_response["platform"] == "yuanbao"
    assert response.search_metadata["reference_count"] == 2
    assert response.search_metadata["source_success_count"] == 1
    assert response.search_metadata["source_failure_count"] == 1
    assert response.search_metadata["source_completeness"] == 0.5
    assert response.search_metadata["capture_status"] == "partial"
    assert response.request_params["platform"] == "yuanbao"


@pytest.mark.asyncio
async def test_mobile_failure_maps_peak_demand_to_rate_limit(monkeypatch):
    adapter = MobileGatewayAdapter("kimi")
    task = SimpleNamespace(id="task-2")
    failed = SimpleNamespace(
        id="task-2",
        status="failed",
        result=None,
        error_message="Kimi is temporarily unavailable due to peak demand",
        error_code="gateway_execution_failed",
        attempt_count=3,
    )

    async def enqueue(prompt):
        return task

    async def wait(task_id):
        return failed

    monkeypatch.setattr(adapter, "_enqueue_task", enqueue)
    monkeypatch.setattr(adapter, "_wait_for_task", wait)

    response = (await adapter.query(["问题"]))[0]

    assert not response.success
    assert response.error_code == ErrorCode.RATE_LIMITED
    assert response.raw_response["attempt_count"] == 3


def test_unresolved_mobile_citations_are_preserved_without_fake_domain():
    persisted = _build_persisted_citations(
        [
            ExtractedSource(
                domain="deepseek.com",
                urls=["https://www.deepseek.com/"],
                title="DeepSeek",
            )
        ],
        [
            {
                "provider": "mobile_gateway",
                "url": "",
                "domain": "",
                "title": "DeepSeek 公司介绍",
                "site_name": "某新闻",
                "index": 2,
                "url_resolution": "unavailable",
                "status": "collected",
            }
        ],
    )

    assert persisted == [
        {
            "domain": "deepseek.com",
            "urls": ["https://www.deepseek.com/"],
            "title": "DeepSeek",
        },
        {
            "domain": "",
            "urls": [],
            "title": "DeepSeek 公司介绍",
            "site_name": "某新闻",
            "index": 2,
            "url_resolution": "unavailable",
            "status": "collected",
        },
    ]


def test_capture_quality_counts_missing_reference_records_as_failures():
    quality = MobileGatewayAdapter._capture_quality(
        {
            "reference_count": 3,
            "sources": [
                {
                    "status": "collected",
                    "url": "https://example.com/source",
                }
            ],
        }
    )

    assert quality == {
        "source_success_count": 1,
        "source_failure_count": 2,
        "source_completeness": pytest.approx(1 / 3),
        "capture_status": "partial",
    }
