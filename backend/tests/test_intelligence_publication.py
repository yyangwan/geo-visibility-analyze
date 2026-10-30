"""Publication is a signed human report of an exact delivered revision, not a publish command."""

from copy import deepcopy
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select

from app.intelligence.action_revisions import revise_plan
from app.intelligence.measurement import CohortRequest, create_cohort
from app.intelligence.models import AuditSample, PromptVersion, PublishedAsset
from app.intelligence.publication import (
    PublicationRequest,
    add_publication_windows,
    asset_view,
    publication_options,
    record_asset,
)
from tests.test_intelligence_action_revisions import revision_request
from tests.test_intelligence_descriptive_measurement import OWN, descriptive_fixture
from tests.test_intelligence_execution import arranged, change, command


async def delivery(db, *, capture_v2=False, project="p1"):
    run, action, pack = await arranged(db, capture_v2=capture_v2, project=project)
    await change(db, action, command("start", 1))
    value = await change(
        db,
        action,
        command(
            "deliver",
            2,
            artifacts=[
                {
                    "kind": "external_url",
                    "title": "官网实施与权限说明",
                    "url": "https://example.com/delivery",
                }
            ],
        ),
    )
    return run, action, pack, value["snapshot"]["delivery"]["event_id"]


def request(action, event, **changes):
    return PublicationRequest(
        **{
            "actionId": action.id,
            "expectedRevision": 1,
            "deliveryEventId": event,
            "artifactIndex": 0,
            "versionConfirmed": True,
            "url": "https://example.com/published",
            "channel": "website",
            "publishedAt": datetime(2026, 1, 1, tzinfo=UTC),
            "reason": "已核对发布页面与已交付版本",
            "idempotencyKey": "publication-key-123",
            **changes,
        }
    )


@pytest.mark.asyncio
async def test_scoped_publication_choices_history_and_role_contract(
    client, db_session, monkeypatch
):
    from app.api.auth import get_current_user
    from app.config import settings
    from app.main import app

    _run, action, _pack, event = await delivery(db_session, capture_v2=True)
    monkeypatch.setattr(settings, "intelligence_enabled", True)
    principal = {"scope": "project", "pid": "p1", "role": "owner", "sub": "owner"}
    app.dependency_overrides[get_current_user] = lambda: principal
    try:
        choices = await client.get(
            f"/api/intelligence/actions/{action.id}/publication-options?revision=1"
        )
        assert choices.status_code == 200 and choices.json()["can_register"]
        assert choices.json()["task_pack"]["own_target_id"] == "registry:own"
        actions = await client.get("/api/intelligence/projects/p1/publication-actions")
        assert (
            actions.status_code == 200
            and actions.json()["actions"][0]["id"] == action.id
        )
        body = request(action, event).model_dump(mode="json", by_alias=True)
        saved = await client.post("/api/intelligence/projects/p1/assets", json=body)
        assert saved.status_code == 202 and not saved.json()["published_bytes_verified"]
        listed = await client.get(
            f"/api/intelligence/projects/p1/assets?actionId={action.id}&revision=1"
        )
        assert listed.json()["assets"] == [saved.json()]
        assert (
            await client.get("/api/intelligence/projects/p1/assets?revision=1")
        ).status_code == 422
        principal["role"] = "viewer"
        assert not (
            await client.get(
                f"/api/intelligence/actions/{action.id}/publication-options"
            )
        ).json()["can_register"]
        assert (
            await client.post("/api/intelligence/projects/p1/assets", json=body)
        ).status_code == 403
        principal.update(pid="other", role="owner")
        assert (
            await client.get(
                f"/api/intelligence/actions/{action.id}/publication-options"
            )
        ).status_code == 404
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_derived_version_and_actual_publication_report_are_separate_from_content_verification(
    db_session,
):
    _run, action, _pack, event = await delivery(db_session)
    choices = await publication_options(
        db_session, "p1", action.id, 1, "owner", "owner"
    )
    assert (
        choices["can_register"]
        and choices["deliveries"][0]["delivery_event_id"] == event
    )
    assert not (
        await publication_options(db_session, "p1", action.id, 1, "viewer", "viewer")
    )["can_register"]
    row = await record_asset(db_session, "p1", request(action, event), "owner", "owner")
    view = asset_view(row)
    assert (
        view["version_fingerprint"] == choices["deliveries"][0]["version_fingerprint"]
    )
    assert view["provenance"]["revision_id"] == choices["revision_id"]
    assert view["provenance"]["artifact"]["url"] == "https://example.com/delivery"
    assert not view["published_bytes_verified"] and not view["publication_authorized"]
    assert view["verification"] == "human_attested_delivery_version"
    assert "content_hash" not in view
    assert (
        await record_asset(db_session, "p1", request(action, event), "owner", "owner")
    ).id == row.id
    assert (
        await db_session.scalar(select(func.count()).select_from(PublishedAsset)) == 1
    )
    row.provenance = {**row.provenance, "revision": 2}
    with pytest.raises(HTTPException) as error:
        asset_view(row)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_old_report_replay_survives_revision_change_but_new_publication_cannot_inherit_old_delivery(
    db_session,
):
    _run, action, _pack, event = await delivery(db_session)
    data = request(action, event)
    row = await record_asset(db_session, "p1", data, "owner", "owner")
    await db_session.commit()
    await revise_plan(db_session, action, revision_request(), "owner")
    await db_session.commit()
    assert (await record_asset(db_session, "p1", data, "owner", "owner")).id == row.id
    old = await publication_options(db_session, "p1", action.id, 1, "owner", "owner")
    assert not old["is_current"] and not old["can_register"] and old["deliveries"]
    current = await publication_options(
        db_session, "p1", action.id, 2, "owner", "owner"
    )
    assert not current["can_register"] and current["deliveries"] == []
    with pytest.raises(HTTPException) as error:
        await record_asset(
            db_session,
            "p1",
            request(action, event, idempotencyKey="changed-revision-key"),
            "owner",
            "owner",
        )
    assert error.value.status_code == 409
    assert asset_view(row)["provenance"]["revision"] == 1


@pytest.mark.asyncio
async def test_forged_delivery_scope_assignee_and_extra_hash_cannot_create_publication(
    db_session,
):
    _run, action, _pack, event = await delivery(db_session)
    for changes in ({"artifactIndex": 9}, {"deliveryEventId": "f" * 32}):
        with pytest.raises(HTTPException) as error:
            await record_asset(
                db_session, "p1", request(action, event, **changes), "owner", "owner"
            )
        assert error.value.status_code == 409
    with pytest.raises(HTTPException) as error:
        await record_asset(
            db_session, "p1", request(action, event), "another-member", "member"
        )
    assert error.value.status_code == 403
    with pytest.raises(HTTPException):
        await record_asset(db_session, "p2", request(action, event), "owner", "owner")
    for changes in (
        {"versionConfirmed": False},
        {"versionConfirmed": "true"},
        {"contentHash": "a" * 64},
        {"actorId": "another"},
    ):
        with pytest.raises(ValidationError):
            request(action, event, **changes)
    assert (
        await db_session.scalar(select(func.count()).select_from(PublishedAsset)) == 0
    )


@pytest.mark.asyncio
async def test_action_cohort_freezes_exact_publication_and_only_original_platform_question_targets(
    db_session,
):
    run, action, pack, event = await delivery(db_session, capture_v2=True)
    row = await record_asset(db_session, "p1", request(action, event), "owner", "owner")
    response = run.manifest["responses"][0]
    version = await db_session.get(PromptVersion, response["prompt_version_id"])
    assert version.text_hash == response["prompt_hash"]
    sample = await db_session.scalar(
        select(AuditSample).where(AuditSample.audit_id == run.audit_id)
    )
    _spec, before, _after = descriptive_fixture()
    for observation in before["brand_presence"]["observations"]:
        observation.update(
            response_id=response["id"],
            sample_id=sample.id,
            platform=response["platform"],
        )
    cohort_data = {
        "rule_version": "brand-visibility-v1",
        "cells": [
            {
                "platform": response["platform"],
                "prompt_hash": response["prompt_hash"],
                "primary_scenario": "implementation",
                "eligibility": "eligible",
            }
        ],
    }
    run.manifest = {**run.manifest, "evaluation_cohort": cohort_data}
    run.summary = {**run.summary, "brand_presence": before["brand_presence"]}
    data = CohortRequest(
        baselineRunId=run.id,
        title="发布行动的单轮观察",
        comparisonMode="descriptive_v1",
        brandTargetIds=[OWN],
        actionId=action.id,
        expectedActionRevision=1,
        publicationAssetIds=[row.id],
        idempotencyKey="action-cohort-key",
    )
    cohort = await create_cohort(db_session, "p1", data, "owner")
    link = cohort.specification["action_link"]
    assert link["revision_id"] == asset_view(row)["provenance"]["revision_id"]
    assert link["publications"][0]["id"] == row.id
    assert link["ai_targets"] == pack.model_dump(mode="json")["ai_targets"]
    assert len(cohort.specification["slots"]) == 1
    with pytest.raises(HTTPException) as error:
        await create_cohort(
            db_session,
            "p1",
            data.model_copy(
                update={
                    "idempotency_key": "wrong-action-revision",
                    "expected_action_revision": 2,
                }
            ),
            "owner",
        )
    assert error.value.status_code == 409


@pytest.mark.parametrize(
    "published,status",
    [
        ("2026-10-15T00:00:00Z", "time_ordered"),
        ("2026-10-12T00:00:00Z", "publication_not_between_samples"),
        ("2026-10-20T00:00:00Z", "publication_not_between_samples"),
    ],
)
def test_time_windows_are_frozen_per_cell_without_changing_raw_observed_deltas(
    published, status
):
    result = {
        "platforms": [
            {
                "observations": [
                    {
                        "baseline_observed_at": "2026-10-13T00:00:00Z",
                        "followup_observed_at": "2026-10-16T00:00:00Z",
                        "observed_delta": 1,
                    }
                ]
            }
        ]
    }
    spec = {
        "action_link": {
            "publications": [
                {
                    "id": "asset",
                    "published_at": published,
                    "provenance": {"delivery_submitted_at": "2026-10-26T00:00:00Z"},
                }
            ]
        }
    }
    value = add_publication_windows(deepcopy(result), spec)
    cell = value["platforms"][0]["observations"][0]
    assert cell["publication_window"]["assets"][0]["status"] == status
    assert (
        cell["observed_delta"] == 1
        and not cell["publication_window"]["causal_claim_allowed"]
    )
    # Late entry of a delivery record does not establish that content was created late.
    assert value["action_link"] == spec["action_link"]
