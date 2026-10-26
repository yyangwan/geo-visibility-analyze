"""Publication receipt replay stays unique after two MySQL repeatable-read snapshots."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.intelligence.models import (
    ActionPlan,
    CaptureAttempt,
    MeasurementCohort,
    PublishedAsset,
)
from app.intelligence.publication import asset_view, record_asset
from app.intelligence.retest_retry import (
    RetryRequest,
    repair_retest,
    retest_state,
    scoped_retest,
)
from tests.test_intelligence_action_revisions_mysql import clean_project
from tests.test_intelligence_mysql import MYSQL_URL, mysql_sessions  # noqa: F401
from tests.test_intelligence_publication import delivery, request
from tests.test_intelligence_retest_retry import fixture_retest

pytestmark = pytest.mark.skipif(
    not MYSQL_URL, reason="Explicit isolated MySQL required"
)


@pytest.mark.asyncio
async def test_same_publication_operation_serializes_old_snapshots(mysql_sessions):  # noqa: F811
    sessions, _ = mysql_sessions
    project = "ci-publication-" + uuid4().hex
    try:
        async with sessions() as db:
            _, action, _, event = await delivery(db, project=project, capture_v2=True)
            data, action_id = request(action, event), action.id
        loaded, both = 0, asyncio.Event()

        async def save():
            nonlocal loaded
            async with sessions() as db:
                await db.get(ActionPlan, action_id)
                loaded += 1
                if loaded == 2:
                    both.set()
                await both.wait()
                row = await record_asset(db, project, data, "owner", "owner")
                await db.commit()
                return asset_view(row)

        one, two = await asyncio.wait_for(asyncio.gather(save(), save()), timeout=25)
        assert one == two and not one["published_bytes_verified"]
        async with sessions() as db:
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(PublishedAsset)
                    .where(PublishedAsset.project_id == project)
                )
                == 1
            )
    finally:
        await clean_project(sessions, project)


@pytest.mark.asyncio
async def test_missing_answer_repair_replays_after_two_old_mysql_snapshots(
    mysql_sessions,  # noqa: F811 - imported isolated fixture
):
    sessions, _ = mysql_sessions
    project = "ci-repair-" + uuid4().hex
    try:
        async with sessions() as db:
            cohort, retest, samples = await fixture_retest(
                db, project=project, partial=True
            )
            state = await retest_state(db, cohort, retest)
            cohort_id, retest_id = cohort.id, retest.id
            complete_id = samples["qwen"].selected_response_id
            complete_sample = samples["qwen"].id
            request_data = RetryRequest(
                expectedRetryStamp=state["retry_stamp"],
                idempotencyKey="mysql-repair-same-key",
            )
        loaded, both = 0, asyncio.Event()

        async def repair():
            nonlocal loaded
            async with sessions() as db:
                cohort = await db.get(MeasurementCohort, cohort_id)
                retest = await scoped_retest(db, cohort, retest_id)
                await retest_state(db, cohort, retest)
                loaded += 1
                if loaded == 2:
                    both.set()
                await both.wait()
                value = await repair_retest(
                    db, cohort, retest_id, request_data, "owner"
                )
                await db.commit()
                return value

        one, two = await asyncio.wait_for(
            asyncio.gather(repair(), repair()), timeout=25
        )
        assert one["repaired_samples"] == two["repaired_samples"]
        assert sorted([one["replayed"], two["replayed"]]) == [False, True]
        assert one["new_independent_rounds"] == two["new_independent_rounds"] == 0
        async with sessions() as db:
            from app.intelligence.models import AuditSample

            assert (
                await db.get(AuditSample, complete_sample)
            ).selected_response_id == complete_id
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(CaptureAttempt)
                    .where(CaptureAttempt.project_id == project)
                )
                == 3
            )
    finally:
        await clean_project(sessions, project)
