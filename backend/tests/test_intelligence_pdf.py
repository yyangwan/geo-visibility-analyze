from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from app.api.auth import get_current_user
from app.config import settings
from app.intelligence.report_pdf import export_scope, render_pdf
from app.main import app
from tests.test_intelligence_foundation import fixture_audit


def sample():
    return {
        "schema": "citation-report-1",
        "audit_id": "12",
        "project_id": "p1",
        "run_id": "a" * 32,
        "revision": 1,
        "status": "partial",
        "snapshot_current": True,
        "coverage": {},
        "platforms": [{"platform": "qwen", "samples": 2, "metrics": []}],
        "entities": [{"id": "entity", "name": "机密品牌"}],
        "sources": [],
        "evidence": [],
    }


def test_pdf_redaction_and_literal_source_text():
    data = sample()
    basic = export_scope(data, "basic")
    assert "entities" not in basic and "evidence" not in basic
    assert basic["platforms"][0]["metrics"] == []
    data["sources"] = [
        {
            "id": "source",
            "response_id": "1",
            "platform": "qwen",
            "title": '<img src="http://invalid/private"/> & 原文',
            "snippet": "原始文本" * 4000,
            "kind": "search_candidate",
            "snapshot_id": None,
            "fetch_status": "blocked",
        }
    ]
    rendered = render_pdf(data)
    assert rendered.startswith(b"%PDF-") and b"%%EOF" in rendered
    assert b"/URI" not in rendered and b"/JavaScript" not in rendered
    assert data["sources"][0]["title"].startswith("<img")
    data["sources"][0]["snippet"] = "原" * 1_000_001
    with pytest.raises(ValueError, match="report_too_large"):
        render_pdf(data)


def test_pdf_contains_associated_comparison_provenance_and_omits_stale_or_basic_estimates(
    monkeypatch,
):
    from app.intelligence import report_pdf

    data = sample()
    metric = {
        "entity_id": "e1",
        "metric": "answer_presence",
        "baseline": {"numerator": 1, "denominator": 10},
        "followup": {"numerator": 3, "denominator": 10},
        "difference": 0.2,
        "interval95": [0.1, 0.3],
    }
    data["measurements"] = {
        "scope_run_id": data["run_id"],
        "truncated": False,
        "results": [
            {
                "id": "m1",
                "cohort_id": "c1",
                "created_at": "2026-10-16",
                "snapshot_current": True,
                "report_role": "followup",
                "baseline_audit_id": "11",
                "followup_audit_id": "12",
                "baseline_run_id": "base",
                "followup_run_id": data["run_id"],
                "entities": [{"id": "e1", "name": "复测主体"}],
                "platforms": [
                    {
                        "platform": "qwen",
                        "status": "valid_comparison",
                        "included": [],
                        "excluded": [],
                        "expected_samples": 60,
                        "prompt_clusters": 20,
                        "out_of_cohort": [],
                        "metrics": [metric],
                    }
                ],
            }
        ],
    }
    paragraphs = []
    original = report_pdf.Paragraph

    def paragraph(text, style):
        paragraphs.append(text)
        return original(text, style)

    monkeypatch.setattr(report_pdf, "Paragraph", paragraph)
    assert render_pdf(data).startswith(b"%PDF-")
    assert any("观测变化 20.0 个百分点" in text for text in paragraphs)
    assert any("基线审计 11 / Run base" in text for text in paragraphs)
    paragraphs.clear()
    data["measurements"]["results"][0]["snapshot_current"] = False
    render_pdf(data)
    assert not any("观测变化 20.0" in text for text in paragraphs)
    assert any("不输出当前变化结论" in text for text in paragraphs)
    paragraphs.clear()
    render_pdf(data, "basic")
    assert not any("固定样本复测" in text or "复测主体" in text for text in paragraphs)


@pytest.mark.asyncio
async def test_pdf_route_exact_revision_currentness_scope_and_no_html_fallback(
    client, db_session, monkeypatch
):
    from app.intelligence import report_pdf, reporting

    monkeypatch.setattr(settings, "intelligence_enabled", True)
    rollback = AsyncMock(wraps=db_session.rollback)
    monkeypatch.setattr(db_session, "rollback", rollback)
    audit = await fixture_audit(db_session)
    other = await fixture_audit(db_session, project="p2")
    other_id = other.id
    data = sample()

    async def report(db, row):
        return {**deepcopy(data), "audit_id": str(row.id)}

    monkeypatch.setattr(reporting, "audit_report", report)
    app.dependency_overrides[get_current_user] = lambda: {
        "scope": "project",
        "pid": "p1",
        "role": "viewer",
    }
    path = f"/api/intelligence/audits/{audit.id}/report/pdf?run_id={'a' * 32}"
    try:
        response = await client.get(path)
        assert response.status_code == 200 and response.content.startswith(b"%PDF-")
        assert response.headers["x-report-run-id"] == "a" * 32
        rollback.assert_awaited_once()
        assert (
            await client.get(
                f"/api/intelligence/audits/{other_id}/report/pdf?run_id={'a' * 32}"
            )
        ).status_code == 403
        assert (await client.get(path.replace("a" * 32, "b" * 32))).status_code == 409
        data["snapshot_current"] = False
        assert (await client.get(path)).status_code == 409
        data["snapshot_current"] = True

        calls = 0

        async def changed_report(db, row):
            nonlocal calls
            calls += 1
            return {**deepcopy(data), "snapshot_current": calls == 1}

        monkeypatch.setattr(reporting, "audit_report", changed_report)
        assert (await client.get(path)).status_code == 409
        calls = 0

        async def changed_attachment(db, row):
            nonlocal calls
            calls += 1
            return {
                **deepcopy(data),
                "measurements": {
                    "scope_run_id": data["run_id"],
                    "results": [],
                    "truncated": calls > 1,
                },
            }

        monkeypatch.setattr(reporting, "audit_report", changed_attachment)
        assert (await client.get(path + "&detail=full")).status_code == 409
        calls = 0
        assert (
            await client.get(path)
        ).status_code == 200  # basic excludes full-detail attachments
        monkeypatch.setattr(reporting, "audit_report", report)

        async def unavailable(*args):
            raise ValueError("report_too_large")

        monkeypatch.setattr(report_pdf, "render_report", unavailable)
        assert (await client.get(path)).status_code == 422
    finally:
        app.dependency_overrides.pop(get_current_user, None)
