from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.config import settings
from app.intelligence.ingestion import prepare_run
from app.intelligence.workspace import analysis_workspace, observation_contexts
from app.models.models import QueryStatus
from tests.test_intelligence_foundation import fixture_audit


def test_shared_snapshot_preserves_every_platform_and_citation_kind():
    run = SimpleNamespace(
        manifest={
            "responses": [{"id": "r1", "platform": "qwen"}],
            "sources": [
                {
                    "id": "c1",
                    "platform": "qwen",
                    "response_id": "r1",
                    "snapshot_id": "s1",
                    "kind": "answer_reference",
                },
                {
                    "id": "c2",
                    "platform": "deepseek",
                    "response_id": "r2",
                    "snapshot_id": "s1",
                    "kind": "search_candidate",
                },
            ],
        }
    )
    contexts = observation_contexts(run, {"owner_kind": "snapshot", "owner_id": "s1"})
    assert [(row["platform"], row["kind"]) for row in contexts] == [
        ("qwen", "answer_reference"),
        ("deepseek", "search_candidate"),
    ]
    assert (
        len(observation_contexts(run, {"owner_kind": "citation", "owner_id": "c2"}))
        == 1
    )
    assert (
        observation_contexts(run, {"owner_kind": "answer", "owner_id": "missing"}) == []
    )


@pytest.mark.asyncio
async def test_latest_pending_never_falls_back_and_explicit_older_selection_is_honored(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "intelligence_enabled", True)
    old = await fixture_audit(db_session)
    await prepare_run(db_session, "p1", old.id)
    latest = await fixture_audit(db_session)
    latest.status = QueryStatus.RUNNING
    foreign = await fixture_audit(db_session, project="p2")
    await db_session.commit()
    result = await analysis_workspace(db_session, "p1")
    assert result["selected_audit_id"] == str(latest.id)
    assert result["report"]["status"] == "needs_analysis"
    assert result["observations"] is None and result["processing"]
    result = await analysis_workspace(db_session, "p1", old.id)
    assert result["selected_audit_id"] == str(old.id)
    assert result["latest_audit_id"] == str(latest.id)
    with pytest.raises(HTTPException) as error:
        await analysis_workspace(db_session, "p1", foreign.id)
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_empty_and_disabled_workspace_are_explicit(db_session, monkeypatch):
    monkeypatch.setattr(settings, "intelligence_enabled", True)
    assert (await analysis_workspace(db_session, "empty"))["report"] is None
    await fixture_audit(db_session)
    monkeypatch.setattr(settings, "intelligence_enabled", False)
    result = await analysis_workspace(db_session, "p1")
    assert not result["enabled"] and result["audits"] == []
