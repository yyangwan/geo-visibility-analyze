"""Loopback development DB proof: transactional fixtures, API startup and worker.

Business fixtures are always rolled back. Only the worker heartbeat is retained.
No model credentials, external source requests or production databases are used.
"""

import argparse
import asyncio
import json
from uuid import uuid4

from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.config import settings
from app.intelligence.models import (
    ActionPlan,
    ActionPlanRevision,
    BrandFact,
    ContextAnalysis,
    IntelligenceRun,
    OwnedContentAsset,
    PlatformDiagnosis,
    WorkerHeartbeat,
)
from app.intelligence.schema_version import SCHEMA_REVISION
from app.utils.timezone import utcnow


def check_contract(expected_database):
    url = make_url(settings.database_url)
    if (
        url.host not in ("localhost", "127.0.0.1", "::1")
        or url.database != expected_database
    ):
        raise ValueError("Fixture proof is limited to an explicitly named loopback DB")
    if settings.intelligence_environment not in ("development", "test"):
        raise ValueError("Fixture proof requires development/test mode")
    engine = create_engine(url.set(drivername="mysql+pymysql"))
    checked = []
    tables = (OwnedContentAsset, ContextAnalysis, ActionPlanRevision, BrandFact)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            session = Session(bind=connection, expire_on_commit=False)
            initial = {
                model.__tablename__: session.scalar(
                    select(func.count()).select_from(model)
                )
                for model in tables
            }
            try:
                run = IntelligenceRun(
                    project_id="schema-fixture-a",
                    audit_id=-1,
                    revision=1,
                    input_hash=uuid4().hex * 2,
                    signature="a" * 64,
                    manifest={},
                    status="ready",
                )
                session.add(run)
                session.flush()
                asset = OwnedContentAsset(
                    project_id=run.project_id,
                    asset_key=uuid4().hex,
                    version=1,
                    registry_brand_id="registered-own",
                    target_id="registry:registered-own",
                    title="Synthetic schema fixture",
                    source_kind="text",
                    status="verified",
                    actor_id="schema-fixture",
                    idempotency_key=uuid4().hex,
                    request_hash="b" * 64,
                )
                session.add(asset)
                session.flush()
                fact_values = {
                    "project_id": run.project_id,
                    "fact_key": "f" * 64,
                    "version": 1,
                    "source_kind": "owned_asset_revision",
                    "target_id": asset.target_id,
                    "registry_brand_id": asset.registry_brand_id,
                    "owned_asset_revision_id": asset.id,
                    "owned_asset_version": 1,
                    "statement": "Structural fixture only",
                    "scope": "Not a business fact approval",
                    "valid_from": utcnow(),
                    "valid_until": utcnow(),
                    "state": "fixture",
                    "reason": "No authorization implied",
                    "actor_id": "schema-fixture",
                    "idempotency_key": uuid4().hex,
                    "request_hash": "c" * 64,
                }
                session.add(BrandFact(**fact_values))
                session.add(
                    ContextAnalysis(
                        project_id=run.project_id,
                        run_id=run.id,
                        owner_kind="answer",
                        owner_id="answer-fixture",
                        input_hash="d" * 64,
                        context_hash="e" * 64,
                        signature="f" * 64,
                    )
                )
                diagnosis = PlatformDiagnosis(
                    project_id=run.project_id,
                    run_id=run.id,
                    diagnosis_hash="1" * 64,
                    payload={},
                )
                session.add(diagnosis)
                session.flush()
                action = ActionPlan(
                    project_id=run.project_id,
                    run_id=run.id,
                    diagnosis_id=diagnosis.id,
                    title="Structural fixture",
                    target={},
                    actor_id="schema-fixture",
                    idempotency_key=uuid4().hex,
                )
                session.add(action)
                session.flush()
                revision_values = {
                    "project_id": run.project_id,
                    "action_id": action.id,
                    "run_id": run.id,
                    "revision": 1,
                    "task_key": "2" * 64,
                    "signature": "3" * 64,
                    "payload": {},
                    "actor_id": "schema-fixture",
                    "reason": "Structural fixture",
                    "idempotency_key": uuid4().hex,
                    "request_hash": "4" * 64,
                }
                session.add(ActionPlanRevision(**revision_values))
                session.flush()
                checked.append("three_new_tables_read_write")
                checked.append("owned_fact_without_entity_or_citation")

                def rejected(label, row, expected_code=1452):
                    try:
                        with session.begin_nested():
                            session.add(row)
                            session.flush()
                    except DBAPIError as error:
                        if not error.orig.args or error.orig.args[0] != expected_code:
                            raise AssertionError(
                                "Unexpected database failure during constraint check"
                            ) from None
                        checked.append(label)
                        return
                    raise AssertionError("Required constraint did not reject fixture")

                rejected(
                    "owned_fact_cross_project_rejected",
                    BrandFact(
                        **{
                            **fact_values,
                            "project_id": "schema-fixture-b",
                            "idempotency_key": uuid4().hex,
                        }
                    ),
                )
                rejected(
                    "owned_fact_wrong_revision_rejected",
                    BrandFact(
                        **{
                            **fact_values,
                            "fact_key": "5" * 64,
                            "owned_asset_version": 2,
                            "idempotency_key": uuid4().hex,
                        }
                    ),
                )
                rejected(
                    "invalid_fact_source_branch_rejected",
                    BrandFact(
                        **{
                            **fact_values,
                            "fact_key": "6" * 64,
                            "source_kind": "citation_review",
                            "idempotency_key": uuid4().hex,
                        }
                    ),
                    expected_code=3819,
                )
                rejected(
                    "context_cross_project_rejected",
                    ContextAnalysis(
                        project_id="schema-fixture-b",
                        run_id=run.id,
                        owner_kind="answer",
                        owner_id="answer-fixture",
                        input_hash="a" * 64,
                        context_hash="b" * 64,
                        signature="c" * 64,
                    ),
                )
                rejected(
                    "revision_cross_project_rejected",
                    ActionPlanRevision(
                        **{
                            **revision_values,
                            "project_id": "schema-fixture-b",
                            "idempotency_key": uuid4().hex,
                        }
                    ),
                )
                rejected(
                    "duplicate_action_revision_rejected",
                    ActionPlanRevision(
                        **{**revision_values, "idempotency_key": uuid4().hex}
                    ),
                    expected_code=1062,
                )
            finally:
                session.close()
                transaction.rollback()
            with Session(bind=connection) as verify:
                after = {
                    model.__tablename__: verify.scalar(
                        select(func.count()).select_from(model)
                    )
                    for model in tables
                }
            if after != initial:
                raise AssertionError("Fixture transaction did not roll back")
            checked.append("business_fixtures_rolled_back")
    finally:
        engine.dispose()
    return checked


async def check_runtime():
    import os

    import httpx

    from app.database import async_session, engine
    from app.intelligence.worker import main as worker_main
    from app.main import app

    if settings.get_analysis_llm_config()[0] or settings.get_llm_config()[0]:
        raise ValueError("Remove model credentials before local startup verification")
    if (
        settings.intelligence_blob_backend != "local"
        or not settings.intelligence_enabled
    ):
        raise ValueError("Use enabled development worker with local storage")
    monitor_id = uuid4().hex
    os.environ["AISCOPE_INTELLIGENCE_WORKER_MONITOR_ID"] = monitor_id
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://local-fixture"
            ) as client:
                response = await client.get("/api/health")
                if (
                    response.status_code != 200
                    or response.json().get("db") != "connected"
                ):
                    raise AssertionError("API startup or database health failed")
            task = asyncio.create_task(worker_main())
            try:
                for _ in range(40):
                    if task.done():
                        await task
                        raise AssertionError("Worker stopped before heartbeat")
                    async with async_session() as db:
                        heartbeat = await db.get(WorkerHeartbeat, monitor_id)
                        if heartbeat and heartbeat.state in ("idle", "working"):
                            break
                    await asyncio.sleep(0.1)
                else:
                    raise AssertionError("Worker heartbeat not observed")
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return ["api_startup_health_200", "worker_fresh_heartbeat", "model_calls_zero"]
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-database", required=True)
    args = parser.parse_args()
    try:
        checks = check_contract(args.expected_database)
        checks.extend(asyncio.run(check_runtime()))
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "database_verification_failed",
                    "error_class": type(error).__name__,
                }
            )
        )
        raise SystemExit(1) from None
    print(
        json.dumps(
            {
                "status": "fresh_database_verified",
                "schema": SCHEMA_REVISION,
                "checks": checks,
                "release_eligible": False,
            }
        )
    )


if __name__ == "__main__":
    main()
