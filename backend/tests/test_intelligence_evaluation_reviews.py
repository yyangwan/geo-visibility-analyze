from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.intelligence.brand_evaluation import (
    aggregate_scores,
    freeze_cohort,
    summarize_answer_scores,
)
from app.intelligence.brand_ranking import verified_ranking
from app.intelligence.evaluation_reviews import (
    CohortReviewRequest,
    review_cohort,
    reviewed_cohort,
)


def ranking_fixture():
    text = "推荐顺序：\n1. 云衡：适合企业\n2. 星河：适合个人"
    signals = []
    for rank, name in enumerate(["云衡", "星河"], 1):
        start = text.index(name)
        signals.append(
            {
                "id": name,
                "entity_id": name,
                "observed_name": name,
                "response_id": "r1",
                "explicit_rank": rank,
                "recommendation": 4,
                "reason_level": "need_matched",
                "start": start,
                "end": start + 2,
            }
        )
    return text, signals


def test_only_complete_explicit_lists_produce_positions():
    text, signals = ranking_fixture()
    assert verified_ranking(text, signals)["星河"]["rank"] == 2
    assert verified_ranking(text, signals)["云衡"]["list_size"] == 2
    assert verified_ranking(text.replace("推荐顺序", "相关产品"), signals) == {}
    assert verified_ranking(text + "\n以上不分先后", signals) == {}
    assert verified_ranking(text, signals[:1]) == {}
    assert verified_ranking(text + "\n1. 其他列表", signals) == {}
    assert verified_ranking(text.replace("2. 星河", "3. 星河"), signals) == {}


def test_review_changes_cohort_identity_without_mutating_base():
    base = freeze_cohort(
        [
            {
                "sample_id": "s",
                "sample_index": 1,
                "platform": "qwen",
                "prompt": "有哪些方案",
                "prompt_hash": "p",
                "category": "recommend",
            }
        ],
        [],
    )
    reviewed = reviewed_cohort(base, {"p": "eligible"})
    assert reviewed["hash"] != base["hash"]
    assert base["cells"][0]["eligibility"] == "needs_review"
    assert aggregate_scores(reviewed, {"s": 80})["score"] == 80
    with pytest.raises(HTTPException):
        reviewed_cohort(base, {"outside": "eligible"})
    directed = freeze_cohort([{**base["cells"][0], "prompt": "云衡品牌如何"}], [{"name": "云衡"}])
    with pytest.raises(HTTPException):
        reviewed_cohort(directed, {"p": "eligible"})


def test_verified_answer_scores_reach_brand_total_only_after_eligibility():
    text, signals = ranking_fixture()
    base = freeze_cohort(
        [
            {
                "sample_id": "s",
                "sample_index": 1,
                "platform": "qwen",
                "prompt": "有哪些方案",
                "prompt_hash": "p",
                "category": "recommend",
            }
        ],
        [],
    )
    manifest = {
        "responses": [
            {
                "id": "r1",
                "sample_id": "s",
                "platform": "qwen",
                "text": text,
                "error": False,
            }
        ],
        "evaluation_cohort": reviewed_cohort(base, {"p": "eligible"}),
    }
    applications = [
        (
            SimpleNamespace(status="completed"),
            SimpleNamespace(
                owner_kind="answer", owner_id="r1", start_offset=0, end_offset=len(text)
            ),
            SimpleNamespace(result={"brand_signals": signals}),
        )
    ]
    entities = [
        {"id": name, "kind": "brand", "identity_status": "confirmed"}
        for name in ["云衡", "星河"]
    ]
    mentions = [
        {
            "entity_id": name,
            "owner_id": "r1",
            "owner_kind": "answer",
            "identity_ambiguity": False,
        }
        for name in ["云衡", "星河"]
    ]
    result = summarize_answer_scores(
        manifest, applications, entities, mentions, {"observations": signals}
    )
    assert [row["score"] for row in result["brand_scores"]] == [100, 80]
    assert result["status"] == "complete"
    manifest["evaluation_cohort"] = base
    assert all(
        row["score"] is None
        for row in summarize_answer_scores(
            manifest, applications, entities, mentions, {"observations": signals}
        )["brand_scores"]
    )


@pytest.mark.asyncio
async def test_review_persists_replays_and_freezes_in_next_revision(db_session):
    from app.intelligence.ingestion import prepare_run
    from tests.test_intelligence_sampling import new_audit

    audit, _, _ = await new_audit(db_session, rounds=1)
    run = await prepare_run(db_session, "p1", audit.id)
    await db_session.commit()
    base = run.manifest["evaluation_base_cohort"]
    data = CohortReviewRequest(
        baseHash=base["hash"],
        decisions={cell["prompt_hash"]: "eligible" for cell in base["cells"]},
        reason="已确认不指定品牌的通用选型问题",
        expectedVersion=0,
        idempotencyKey="cohort-review-123",
    )
    event = await review_cohort(db_session, run, data, "reviewer-1")
    await db_session.commit()
    assert (await review_cohort(db_session, run, data, "reviewer-1")).id == event.id
    with pytest.raises(HTTPException) as stale:
        await review_cohort(
            db_session,
            run,
            data.model_copy(update={"idempotency_key": "cohort-review-456"}),
            "reviewer-1",
        )
    assert stale.value.status_code == 409
    await db_session.commit()
    later = await prepare_run(db_session, "p1", audit.id)
    assert later.id != run.id
    assert later.manifest["evaluation_cohort"]["status"] == "reviewed"
    assert run.manifest["evaluation_cohort"]["status"] == "requires_prompt_review"
    assert later.manifest["evaluation_review"]["id"] == event.id


@pytest.mark.asyncio
async def test_cohort_api_enforces_project_writer_and_actor(client, db_session, monkeypatch):
    from app.api.auth import get_current_user
    from app.config import settings
    from app.intelligence.ingestion import prepare_run
    from app.main import app
    from tests.test_intelligence_sampling import new_audit

    monkeypatch.setattr(settings, "intelligence_enabled", True)
    audit, _, _ = await new_audit(db_session, rounds=1)
    run = await prepare_run(db_session, "p1", audit.id)
    run.status = "ready"
    await db_session.commit()
    base = run.manifest["evaluation_base_cohort"]
    payload = {"baseHash": base["hash"], "decisions": {cell["prompt_hash"]: "eligible" for cell in base["cells"]},
               "reason": "审核通用竞争问题资格", "expectedVersion": 0, "idempotencyKey": "api-cohort-key-123"}
    route = f"/api/intelligence/runs/{run.id}/evaluation-cohort"
    user = {"scope": "project", "pid": "p2", "role": "owner", "sub": "reviewer"}
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        assert (await client.get(route)).status_code == 404
        assert (await client.post(route + "/review", json=payload)).status_code == 404
        user.update(pid="p1", role="viewer")
        assert (await client.post(route + "/review", json=payload)).status_code == 403
        user.update(role="owner")
        user.pop("sub")
        assert (await client.post(route + "/review", json=payload)).status_code == 403
        user["sub"] = "reviewer"
        assert (await client.post(route + "/review", json=payload)).status_code == 202
        assert (await client.get(route)).json()["latest_review_version"] == 1
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_review_migration_creates_append_only_table():
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect

    path = Path(__file__).parents[1] / "alembic/schema/20261022_evaluation_reviews.py"
    spec = importlib.util.spec_from_file_location("review_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        schema = inspect(connection)
        assert "intelligence_evaluation_reviews" in schema.get_table_names()
        assert len(schema.get_unique_constraints("intelligence_evaluation_reviews")) == 2
    engine.dispose()
