"""Repeated-read sessions must replay one cohort/capture/result, not duplicate them."""

import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.intelligence.measurement import (
    CohortRequest,
    CompareRequest,
    RetestRequest,
    compare_cohort,
    create_cohort,
    start_retest,
)
from app.intelligence.models import (
    AuditSample,
    CohortRetest,
    IntelligenceRun,
    MeasurementCohort,
    MeasurementResult,
    PromptVersion,
)
from app.intelligence.semantics import stable_hash
from tests.test_intelligence_action_revisions_mysql import clean_project
from tests.test_intelligence_descriptive_measurement import OWN, descriptive_fixture
from tests.test_intelligence_foundation import fixture_audit
from tests.test_intelligence_mysql import MYSQL_URL, mysql_sessions  # noqa: F401

pytestmark = pytest.mark.skipif(
    not MYSQL_URL, reason="Explicit isolated MySQL CI database not configured"
)


@pytest.mark.asyncio
async def test_mysql_cohort_retest_and_result_replay_after_older_repeatable_read_snapshots(
    mysql_sessions,  # noqa: F811 - explicit isolated database fixture
):
    sessions, _ = mysql_sessions
    project = "ci-descriptive-" + uuid4().hex
    try:
        async with sessions() as db:
            _spec, before, after = descriptive_fixture()
            audit = await fixture_audit(db, project=project)
            version = PromptVersion(
                project_id=project,
                prompt_id=1,
                text="哪些方案适合多门店企业",
                text_hash=stable_hash("哪些方案适合多门店企业"),
                category="custom",
            )
            # Prompt versions use the hash of actual UTF-8 bytes, not a JSON string.
            from app.intelligence.blob import digest

            version.text_hash = digest(version.text.encode())
            db.add(version)
            await db.flush()
            sample = AuditSample(
                project_id=project,
                audit_id=audit.id,
                prompt_version_id=version.id,
                platform="qwen",
                surface="app",
                sample_index=1,
                conditions=before["responses"][0]["conditions"],
                condition_hash=stable_hash({}),
                status="sealed",
            )
            db.add(sample)
            await db.flush()
            for snapshot, sid in ((before, sample.id), (after, "followup-sample")):
                snapshot["responses"][0].update(
                    sample_id=sid,
                    prompt_version_id=version.id,
                    prompt_hash=version.text_hash,
                )
                for observation in snapshot["brand_presence"]["observations"]:
                    observation["sample_id"] = sid
                snapshot["evaluation_cohort"]["cells"] = [
                    {
                        "platform": "qwen",
                        "prompt_hash": version.text_hash,
                        "primary_scenario": "implementation",
                        "eligibility": "eligible",
                    }
                ]
            baseline = IntelligenceRun(
                project_id=project,
                audit_id=audit.id,
                revision=1,
                status="partial",
                input_hash="i" * 64,
                signature="s" * 64,
                manifest={
                    "responses": before["responses"],
                    "evaluation_cohort": before["evaluation_cohort"],
                },
                summary={"brand_presence": before["brand_presence"]},
            )
            db.add(baseline)
            await db.commit()
            baseline_id = baseline.id
        request = CohortRequest(
            baselineRunId=baseline_id,
            comparisonMode="descriptive_v1",
            brandTargetIds=[OWN],
            title="单轮并发复测方案",
            idempotencyKey="mysql-cohort-key",
        )

        async def race(model, row_id, operation):
            loaded, both = 0, asyncio.Event()

            async def execute():
                nonlocal loaded
                async with sessions() as db:
                    row = await db.get(
                        model, row_id
                    )  # Establish an older consistent-read snapshot.
                    loaded += 1
                    if loaded == 2:
                        both.set()
                    await both.wait()
                    value = await operation(db, row)
                    await db.commit()
                    return value.id

            values = await asyncio.wait_for(
                asyncio.gather(execute(), execute()), timeout=25
            )
            assert values[0] == values[1]
            return values[0]

        cohort_id = await race(
            IntelligenceRun,
            baseline_id,
            lambda db, _run: create_cohort(db, project, request, "owner"),
        )
        retest_id = await race(
            MeasurementCohort,
            cohort_id,
            lambda db, cohort: start_retest(
                db, cohort, RetestRequest(idempotencyKey="mysql-retest-key"), "owner"
            ),
        )
        async with sessions() as db:
            retest = await db.get(CohortRetest, retest_id)
            sample = await db.scalar(
                select(AuditSample).where(AuditSample.audit_id == retest.audit_id)
            )
            after["responses"][0]["sample_id"] = sample.id
            for observation in after["brand_presence"]["observations"]:
                observation["sample_id"] = sample.id
            followup = IntelligenceRun(
                project_id=project,
                audit_id=retest.audit_id,
                revision=1,
                status="partial",
                input_hash="f" * 64,
                signature="s" * 64,
                manifest={
                    "responses": deepcopy(after["responses"]),
                    "evaluation_cohort": after["evaluation_cohort"],
                },
                summary={"brand_presence": after["brand_presence"]},
            )
            db.add(followup)
            await db.commit()
            followup_id = followup.id
        await race(
            MeasurementCohort,
            cohort_id,
            lambda db, cohort: compare_cohort(
                db, cohort, CompareRequest(followupRunId=followup_id), "owner"
            ),
        )
        async with sessions() as db:
            for model in (MeasurementCohort, CohortRetest, MeasurementResult):
                assert (
                    await db.scalar(
                        select(func.count())
                        .select_from(model)
                        .where(model.project_id == project)
                    )
                    == 1
                )
    finally:
        await clean_project(sessions, project)
