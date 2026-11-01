from types import SimpleNamespace

import pytest

from app.api.auth import get_current_user
from app.config import settings
from app.intelligence.ingestion import prepare_run
from app.intelligence.models import AuditIntelligenceState, IntelligenceRun
from app.intelligence.reporting import audit_report, project_report
from app.main import app
from tests.test_intelligence_foundation import fixture_audit


def test_report_uses_independent_source_population_and_all_entities():
    responses = [
        {"id": "r1", "platform": "qwen", "answer_status": "complete"},
        {"id": "r2", "platform": "qwen", "answer_status": "partial"},
        {"id": "r3", "platform": "deepseek", "answer_status": "complete"},
    ]
    run = SimpleNamespace(
        id="a" * 32,
        revision=1,
        status="partial",
        finished_at=None,
        coverage={},
        manifest={"sources": []},
        summary={
            "entities": [{"id": "other", "name": "未登记品牌", "role": "unregistered"}]
        },
    )
    snapshot = {
        "responses": responses,
        "completed": {"r1", "r2", "r3"},
        "hits": {"other": {"r1", "r2"}},
        "source_metrics": {
            "cited_archive_presence": {
                "completed": {"r2", "r3"},
                "hits": {"other": {"r2"}},
                "unusable_reasons": {"r1": "cited_archive_incomplete"},
            }
        },
    }
    report = project_report(run, snapshot, True, {})
    qwen = next(row for row in report["platforms"] if row["platform"] == "qwen")
    answer, archive = qwen["metrics"]
    assert answer["entities"] == [
        {"entity_id": "other", "numerator": 1, "denominator": 1}
    ]
    assert archive["entities"] == [
        {"entity_id": "other", "numerator": 1, "denominator": 1}
    ]
    assert answer["included"] == ["r1"] and archive["included"] == ["r2"]
    assert archive["excluded"] == [
        {"response_id": "r1", "reason": "cited_archive_incomplete"}
    ]
    assert report["entities"][0]["role"] == "unregistered"
    assert project_report(run, snapshot, False, {})["status"] == "stale"


@pytest.mark.asyncio
async def test_newest_pending_revision_never_falls_back_or_creates_analysis(db_session):
    audit = await fixture_audit(db_session)
    missing = await audit_report(db_session, audit)
    assert missing["status"] == "needs_analysis" and "run_id" not in missing
    run = await prepare_run(db_session, "p1", audit.id)
    run.status = "ready"
    newer = IntelligenceRun(
        project_id="p1",
        audit_id=audit.id,
        revision=run.revision + 1,
        manifest={},
        input_hash="f" * 64,
        signature="e" * 64,
        status="queued",
    )
    db_session.add(newer)
    await db_session.flush()
    result = await audit_report(db_session, audit)
    assert result["run_id"] == newer.id and result["status"] == "queued"
    assert "entities" not in result and result["snapshot_current"] is False


@pytest.mark.asyncio
async def test_report_route_scopes_and_reads_without_generation(
    client, db_session, monkeypatch
):
    monkeypatch.setattr(settings, "intelligence_enabled", True)
    audit = await fixture_audit(db_session)
    other = await fixture_audit(db_session, project="p2")
    app.dependency_overrides[get_current_user] = lambda: {
        "scope": "project",
        "pid": "p1",
        "role": "viewer",
    }
    try:
        assert (
            await client.get(f"/api/intelligence/audits/{other.id}/report")
        ).status_code == 403
        result = await client.get(f"/api/intelligence/audits/{audit.id}/report")
        assert (
            result.status_code == 200 and result.json()["schema"] == "citation-report-1"
        )
        assert result.json()["status"] == "needs_analysis"
        monkeypatch.setattr(settings, "intelligence_enabled", False)
        assert (
            await client.get(f"/api/intelligence/audits/{audit.id}/report")
        ).status_code == 503
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_saved_run_reports_verified_sources_even_when_context_is_stale(
    db_session,
):
    from app.intelligence.pipeline import build_summary
    from app.models.models import Audit
    from tests.test_intelligence_source_metrics import corpus

    run, target, _ = await corpus(db_session)
    run.coverage, run.summary = await build_summary(db_session, run)
    run.status = "partial"
    await db_session.commit()
    report = await audit_report(db_session, await db_session.get(Audit, run.audit_id))
    assert report["status"] == "stale" and not report["snapshot_current"]
    assert report["run_id"] == run.id
    metric = next(
        metric
        for platform in report["platforms"]
        for metric in platform["metrics"]
        if metric["metric"] == "cited_archive_presence"
    )
    entity = next(row for row in metric["entities"] if row["entity_id"] == target.id)
    assert entity["numerator"] == entity["denominator"] == 1
    assert report["sources"][0]["kind"] == "answer_reference"
    assert "text_key" not in report["sources"][0]


@pytest.mark.asyncio
async def test_latest_report_keeps_large_json_out_of_sort_and_reads_reviews_once(db_session):
    from sqlalchemy import event

    audit = await fixture_audit(db_session)
    run = await prepare_run(db_session, "p1", audit.id)
    run.status = "partial"
    db_session.add(AuditIntelligenceState(project_id="p1", audit_id=audit.id))
    await db_session.flush()
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        report = await audit_report(db_session, audit, include_measurements=False)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert report["run_id"] == run.id
    sorted_runs = [sql for sql in statements if "from intelligence_runs" in sql and "order by" in sql]
    assert sorted_runs
    assert all("intelligence_runs.manifest" not in sql and "intelligence_runs.summary" not in sql for sql in sorted_runs)
    assert sum("from intelligence_claim_reviews" in sql for sql in statements) == 1
    assert not any("from intelligence_brand_facts" in sql for sql in statements)


@pytest.mark.asyncio
async def test_dashboard_matches_report_without_loading_unused_reviews(db_session, monkeypatch):
    from sqlalchemy import event

    from app.intelligence.dashboard import project_dashboard

    monkeypatch.setattr(settings, "intelligence_enabled", True)
    audit = await fixture_audit(db_session)
    run = await prepare_run(db_session, "p1", audit.id)
    run.status = "partial"
    db_session.add(AuditIntelligenceState(project_id="p1", audit_id=audit.id))
    await db_session.flush()
    full = await audit_report(db_session, audit, include_measurements=False)
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        overview = (await project_dashboard(db_session, "p1"))["report"]
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert overview == {key: value for key, value in full.items() if key not in ("sources", "evidence", "responses", "source_states")}
    assert not any("from intelligence_claim_reviews" in sql or "from intelligence_brand_facts" in sql for sql in statements)


@pytest.mark.asyncio
async def test_capture_poll_does_not_load_analysis_bodies(db_session):
    from sqlalchemy import event

    from app.api.intelligence import capture_status
    from app.intelligence.models import SamplingAudit

    audit = await fixture_audit(db_session)
    run = await prepare_run(db_session, "p1", audit.id)
    run.status, run.coverage = "partial", {"semantic_error_count": 2}
    db_session.add(SamplingAudit(project_id="p1", audit_id=audit.id, idempotency_key="capture-poll", request_hash="f" * 64, expected_samples=1))
    await db_session.flush()
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        status = await capture_status(audit.id, {"scope": "project", "pid": "p1", "role": "viewer"}, db_session)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert status["analysis_status"] == "partial" and status["semantic_error_count"] == 2
    assert not any("intelligence_runs.manifest" in sql or "intelligence_runs.summary" in sql for sql in statements)
