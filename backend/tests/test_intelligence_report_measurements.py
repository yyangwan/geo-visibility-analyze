from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from app.intelligence.measurement import CompareRequest, compare_cohort
from app.intelligence.measurement_history import report_measurements
from app.intelligence.models import IntelligenceRun
from app.intelligence.reporting import audit_report
from app.models.models import Audit
from tests.test_intelligence_foundation import fixture_audit
from tests.test_intelligence_measurement_history import saved_result


@pytest.mark.asyncio
async def test_report_only_associates_exact_run_and_preserves_baseline_followup_roles(
    db_session, monkeypatch
):
    monkeypatch.setattr(
        "app.intelligence.measurement_history.stamp",
        AsyncMock(return_value=("stamp", True)),
    )
    cohort, baseline, followup, row = await saved_result(db_session)
    data = await report_measurements(db_session, baseline)
    assert data["scope_run_id"] == baseline.id and not data["truncated"]
    assert data["results"][0]["report_role"] == "baseline"
    assert (await report_measurements(db_session, followup))["results"][0][
        "report_role"
    ] == "followup"
    other_audit = await fixture_audit(db_session, project="p2")
    other = IntelligenceRun(
        project_id="p2",
        audit_id=other_audit.id,
        revision=1,
        input_hash="o" * 64,
        signature=baseline.signature,
        manifest={},
        status="partial",
    )
    db_session.add(other)
    await db_session.flush()
    assert (await report_measurements(db_session, other))["results"] == []
    newer = IntelligenceRun(
        project_id="p1",
        audit_id=followup.audit_id,
        revision=2,
        input_hash="n" * 64,
        signature=followup.signature,
        manifest={},
        status="partial",
    )
    db_session.add(newer)
    await db_session.flush()
    assert (await report_measurements(db_session, newer))["results"] == []
    assert not (await report_measurements(db_session, baseline))["results"][0][
        "snapshot_current"
    ]
    report = await audit_report(
        db_session, await db_session.get(Audit, baseline.audit_id)
    )
    assert report["measurements"]["results"][0]["id"] == row.id
    overview = await audit_report(
        db_session,
        await db_session.get(Audit, baseline.audit_id),
        include_measurements=False,
    )
    assert "measurements" not in overview
    assert cohort.id == report["measurements"]["results"][0]["cohort_id"]


@pytest.mark.asyncio
async def test_stale_projection_removes_delta_without_mutating_saved_measurement(
    db_session,
):
    _, baseline, _, row = await saved_result(db_session)
    payload = deepcopy(row.result)
    metric = payload["platforms"][0]["metrics"][0]
    metric.update(difference=0.2, interval95=[0.1, 0.3])
    row.result = payload
    await db_session.flush()
    data = await report_measurements(db_session, baseline)
    assert not data["results"][0]["snapshot_current"]
    projected = data["results"][0]["platforms"][0]["metrics"][0]
    assert projected["difference"] is None and projected["interval95"] is None
    assert row.result["platforms"][0]["metrics"][0]["difference"] == 0.2


@pytest.mark.asyncio
async def test_report_marks_truncation_without_altering_any_result_population(
    db_session,
):
    cohort, baseline, _, _ = await saved_result(db_session)
    for index in range(10):
        audit = await fixture_audit(db_session)
        run = IntelligenceRun(
            project_id="p1",
            audit_id=audit.id,
            revision=1,
            input_hash=str(index).zfill(64),
            signature=baseline.signature,
            manifest={},
            status="partial",
        )
        db_session.add(run)
        await db_session.flush()
        await compare_cohort(
            db_session, cohort, CompareRequest(followupRunId=run.id), "tester"
        )
    data = await report_measurements(db_session, baseline)
    assert data["truncated"] and len(data["results"]) == 10
    assert all(item["baseline_run_id"] == baseline.id for item in data["results"])
    assert all(
        item["platforms"][0]["expected_samples"] == len(cohort.specification["slots"])
        for item in data["results"]
    )
