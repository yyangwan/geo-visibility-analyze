"""Owned materials never fabricate citations, identities, or model evidence."""

from copy import deepcopy
from datetime import UTC, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, update

from app.api.auth import get_current_user
from app.config import settings
from app.intelligence.blob import IntegrityError, LocalBlobStore
from app.intelligence.ingestion import prepare_run
from app.intelligence.models import (
    AnalysisJob,
    BrandFact,
    CitationObservation,
    ClaimReview,
    OwnedContentAsset,
)
from app.intelligence.owned_assets import (
    RegisterAsset,
    RetryAsset,
    VerifyAsset,
    material_hash,
    read_text,
    register_asset,
    retry_asset,
    verify_asset,
)
from app.intelligence.owned_facts import (
    ApproveOwnedFact,
    approve_owned_fact,
    withdraw_owned_fact,
)
from app.intelligence.pipeline import Pipeline
from app.intelligence.project_context import ContextRequest, receive_context
from app.intelligence.queue import JobQueue, LeaseLost, db_now
from app.intelligence.reviews import WithdrawRequest, fact_snapshot, fact_view
from app.intelligence.sources import CapturedSource, SourceBlocked, SourceUnavailable
from app.intelligence.worker import execute
from app.main import app
from app.utils.timezone import utcnow
from tests.conftest import TestSessionLocal
from tests.test_intelligence_foundation import fixture_audit

TEXT = "🧭云衡支持30家门店，仅限专业版。服务范围为中国大陆。"


async def context(db):
    await receive_context(
        db,
        "p1",
        ContextRequest(
            version=1,
            brands=[
                {"id": "b1", "name": "云衡", "is_competitor": False},
                {"id": "b2", "name": "锐川", "is_competitor": True},
            ],
        ),
        "reviewer",
    )
    await db.commit()


def registration(**changes):
    return RegisterAsset(
        **{
            "registryBrandId": "b1",
            "title": "云衡产品规格",
            "text": TEXT,
            "expectedVersion": 0,
            "idempotencyKey": "register-material-1",
            **changes,
        }
    )


def verification(asset, **changes):
    return VerifyAsset(
        **{
            "expectedVersion": asset.verification.get("version", 0),
            "materialHash": material_hash(asset),
            "decision": "verified",
            "scope": "中国大陆专业版产品规格",
            "reason": "产品负责人核对材料版本及适用范围",
            "idempotencyKey": "verify-material-1",
            **changes,
        }
    )


def approval(asset, **changes):
    quote = "云衡支持30家门店，仅限专业版。"
    start = TEXT.index(quote)
    now = utcnow().replace(tzinfo=UTC)
    return ApproveOwnedFact(
        **{
            "quote": quote,
            "start": start,
            "end": start + len(quote),
            "materialHash": material_hash(asset),
            "expectedVersion": 0,
            "expectedVerificationVersion": asset.verification["version"],
            "scope": "中国大陆专业版产品，按产品规格",
            "reason": "核对原文及版本后批准此限定事实",
            "validFrom": now - timedelta(days=1),
            "validUntil": now + timedelta(days=30),
            "idempotencyKey": "approve-material-fact",
            **changes,
        }
    )


async def saved(db, tmp_path):
    await context(db)
    store = LocalBlobStore(tmp_path)
    asset = await register_asset(db, "p1", registration(), "reviewer", store)
    await db.commit()
    return asset, store


@pytest.mark.asyncio
async def test_text_registration_is_durable_replayed_and_scoped_without_creating_observations(
    db_session, tmp_path
):
    asset, store = await saved(db_session, tmp_path)
    assert await read_text(asset, store) == TEXT
    assert asset.verification["purpose"] == "audit_reference"
    assert (
        await register_asset(db_session, "p1", registration(), "reviewer", store)
    ).id == asset.id
    assert (
        await db_session.scalar(select(func.count()).select_from(OwnedContentAsset))
        == 1
    )
    assert (
        await db_session.scalar(select(func.count()).select_from(CitationObservation))
        == 0
    )
    with pytest.raises(HTTPException) as conflict:
        await register_asset(
            db_session, "p1", registration(text="其他文字"), "reviewer", store
        )
    assert conflict.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("brand", ["b2", "missing"])
async def test_material_cannot_be_registered_for_a_competitor_or_unknown_brand(
    db_session, tmp_path, brand
):
    await context(db_session)
    body = registration().model_dump(by_alias=True)
    body["registryBrandId"] = brand
    with pytest.raises(HTTPException) as raised:
        await register_asset(
            db_session,
            "p1",
            RegisterAsset(**body),
            "reviewer",
            LocalBlobStore(tmp_path),
        )
    assert raised.value.status_code == 422


@pytest.mark.asyncio
async def test_approval_requires_verification_and_exact_unicode_then_needs_no_entity_or_audit(
    db_session, tmp_path
):
    asset, store = await saved(db_session, tmp_path)
    bad = approval(asset, expectedVerificationVersion=1)
    with pytest.raises(HTTPException):
        await approve_owned_fact(db_session, asset, bad, "reviewer", store)
    verify = verification(asset)
    await verify_asset(db_session, asset, verify, "reviewer", store)
    await db_session.commit()
    await verify_asset(db_session, asset, verify, "reviewer", store)
    assert (
        asset.verification["version"] == 1 and len(asset.verification["history"]) == 1
    )
    with pytest.raises(HTTPException) as raised:
        await approve_owned_fact(
            db_session,
            asset,
            approval(asset, start=2, end=2 + len(bad.quote)),
            "reviewer",
            store,
        )
    assert raised.value.status_code == 422
    data = approval(asset)
    fact = await approve_owned_fact(db_session, asset, data, "reviewer", store)
    await db_session.commit()
    assert (
        fact.entity_id is None
        and fact.review_id is None
        and fact.identity_event_id is None
    )
    assert fact.source_ref["evidence"]["run_id"] is None
    assert (await fact_view(db_session, fact))["status"] == "valid"
    assert (
        await approve_owned_fact(db_session, asset, data, "reviewer", store)
    ).id == fact.id
    assert await db_session.scalar(select(func.count()).select_from(ClaimReview)) == 0


@pytest.mark.asyncio
async def test_new_material_revision_invalidates_only_old_fact_and_freezes_new_run_inputs(
    db_session, tmp_path
):
    asset, store = await saved(db_session, tmp_path)
    await verify_asset(db_session, asset, verification(asset), "reviewer", store)
    fact = await approve_owned_fact(
        db_session, asset, approval(asset), "reviewer", store
    )
    audit = await fixture_audit(db_session)
    first = await prepare_run(db_session, "p1", audit.id, enrich=False)
    before = deepcopy(first.manifest)
    body = registration().model_dump(by_alias=True)
    body.update(
        {
            "assetKey": asset.asset_key,
            "expectedVersion": 1,
            "text": TEXT + "新增说明。",
            "idempotencyKey": "register-material-2",
        }
    )
    second = await register_asset(
        db_session, "p1", RegisterAsset(**body), "reviewer", store
    )
    await db_session.commit()
    assert second.version == 2 and await read_text(asset, store) == TEXT
    assert (await fact_view(db_session, fact))["status"] == "needs_review"
    with pytest.raises(HTTPException):
        await register_asset(
            db_session,
            "p1",
            RegisterAsset(**{**body, "idempotencyKey": "stale-new-version"}),
            "reviewer",
            store,
        )
    new = await prepare_run(db_session, "p1", audit.id, enrich=False)
    assert new.manifest["owned_asset_revisions"][0]["id"] == second.id
    assert (
        first.manifest == before
        and before["owned_asset_revisions"][0]["id"] == asset.id
    )


@pytest.mark.asyncio
async def test_material_rejection_and_fact_withdrawal_are_versioned_and_export_becomes_invalid(
    db_session, tmp_path
):
    asset, store = await saved(db_session, tmp_path)
    await verify_asset(db_session, asset, verification(asset), "reviewer", store)
    fact = await approve_owned_fact(
        db_session, asset, approval(asset), "reviewer", store
    )
    await verify_asset(
        db_session,
        asset,
        verification(
            asset,
            expectedVersion=1,
            decision="rejected",
            idempotencyKey="reject-material-1",
        ),
        "reviewer",
        store,
    )
    assert (await fact_view(db_session, fact))["status"] == "needs_review"
    event = WithdrawRequest(
        expectedVersion=1,
        reason="当前资料需要更正后再次核对",
        idempotencyKey="withdraw-owned-fact",
    )
    new = await withdraw_owned_fact(db_session, fact, event, "reviewer")
    await db_session.commit()
    assert new.version == 2 and new.source_ref == fact.source_ref
    assert (await fact_view(db_session, fact))["status"] == "superseded"
    assert (await fact_view(db_session, new))["status"] == "withdrawn"
    assert (await fact_snapshot(db_session, "p1"))[fact.fact_key][
        "effective_status"
    ] == "withdrawn"


@pytest.mark.asyncio
async def test_blob_tampering_cannot_be_verified(db_session, tmp_path):
    asset, store = await saved(db_session, tmp_path)
    store.path(asset.text_key).write_bytes(b"tampered")
    with pytest.raises(IntegrityError):
        await verify_asset(db_session, asset, verification(asset), "reviewer", store)
    assert asset.verification["decision"] == "unreviewed"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["blocked", "challenge", "transient"])
async def test_owned_url_jobs_retain_restricted_or_failed_material_without_fake_fact(
    db_session, tmp_path, monkeypatch, failure
):
    await context(db_session)
    body = registration().model_dump(by_alias=True)
    body.update({"url": "https://example.com/specs", "text": None})
    asset = await register_asset(db_session, "p1", RegisterAsset(**body), "reviewer")
    await db_session.commit()

    async def capture(*_):
        if failure == "blocked":
            raise SourceBlocked("non_public_destination")
        if failure == "transient":
            raise SourceUnavailable("http_error", 503)
        return CapturedSource(
            "https://example.com/specs",
            200,
            b"challenge",
            "环境异常\n当前环境异常，完成验证后即可继续访问。\n去验证",
            "unknown",
        )

    queue = JobQueue(TestSessionLocal, "owned-test")
    lease = await queue.claim()
    if failure == "transient":
        async with TestSessionLocal() as db:
            job = await db.get(AnalysisJob, lease.id)
            job.max_attempts = 1
            await db.commit()
    await execute(
        queue, Pipeline(queue, LocalBlobStore(tmp_path), capture=capture), lease
    )
    await db_session.refresh(asset)
    if failure == "challenge":
        assert asset.status == "captured"
        with pytest.raises(HTTPException):
            await verify_asset(
                db_session,
                asset,
                verification(asset),
                "reviewer",
                LocalBlobStore(tmp_path),
            )
    else:
        assert asset.status in ("blocked", "failed")
        event = RetryAsset(
            reason="来源站点恢复后再次取得材料", idempotencyKey="retry-owned-material"
        )
        job = await retry_asset(db_session, asset, event, "reviewer")
        await db_session.commit()
        assert (await retry_asset(db_session, asset, event, "reviewer")).id == job.id
    assert await db_session.scalar(select(func.count()).select_from(BrandFact)) == 0


@pytest.mark.asyncio
async def test_stale_owned_fetch_cannot_publish_after_lease_replacement(
    db_session, tmp_path
):
    await context(db_session)
    body = registration().model_dump(by_alias=True)
    body.update({"url": "https://example.com/specs", "text": None})
    asset = await register_asset(db_session, "p1", RegisterAsset(**body), "reviewer")
    await db_session.commit()
    queue = JobQueue(TestSessionLocal, "owned-old")
    lease = await queue.claim()

    async def capture(*_):
        async with TestSessionLocal() as db:
            now = await db_now(db)
            await db.execute(
                update(AnalysisJob)
                .where(AnalysisJob.id == lease.id)
                .values(lease_until=now - timedelta(seconds=1))
            )
            await db.commit()
        await queue.reap()
        return CapturedSource(
            "https://example.com/specs", 200, TEXT.encode(), TEXT, "unknown"
        )

    with pytest.raises(LeaseLost):
        await Pipeline(
            queue, LocalBlobStore(tmp_path), capture=capture
        ).fetch_owned_asset(lease)
    await db_session.refresh(asset)
    assert asset.status == "queued" and asset.snapshot_id is None


@pytest.mark.asyncio
async def test_successful_owned_url_retains_snapshot_and_uses_canonical_document_identity(
    db_session, tmp_path
):
    from app.intelligence.models import SourceDocument, SourceSnapshot
    from app.intelligence.semantics import stable_hash

    await context(db_session)
    document = SourceDocument(
        project_id="p1",
        url="https://example.com/specs",
        url_hash=stable_hash("https://example.com/specs"),
        title="规格页",
    )
    db_session.add(document)
    body = registration().model_dump(by_alias=True)
    body.update({"url": "https://example.com/specs", "text": None})
    asset = await register_asset(db_session, "p1", RegisterAsset(**body), "reviewer")
    await db_session.commit()

    async def capture(*_):
        return CapturedSource(
            "https://example.com/specs", 200, TEXT.encode(), TEXT, "unknown"
        )

    queue = JobQueue(TestSessionLocal, "owned-public")
    store = LocalBlobStore(tmp_path)
    await Pipeline(queue, store, capture=capture).fetch_owned_asset(await queue.claim())
    await db_session.refresh(asset)
    snapshot = await db_session.get(SourceSnapshot, asset.snapshot_id)
    assert snapshot.document_id == document.id
    assert await read_text(asset, store) == TEXT
    assert (
        await db_session.scalar(select(func.count()).select_from(CitationObservation))
        == 0
    )
    assert asset.verification["decision"] == "unreviewed"


@pytest.mark.asyncio
async def test_owned_materials_and_facts_cannot_create_answer_mentions_or_improve_visibility(
    db_session, tmp_path, monkeypatch
):
    from app.intelligence.semantics import checked_result

    asset, store = await saved(db_session, tmp_path)
    await verify_asset(db_session, asset, verification(asset), "reviewer", store)
    await approve_owned_fact(db_session, asset, approval(asset), "reviewer", store)
    audit = await fixture_audit(db_session, text="其他方案可用。")
    run = await prepare_run(db_session, "p1", audit.id)
    await db_session.commit()
    monkeypatch.setattr(settings, "analysis_llm_api_key", "fake")

    async def analyze(value, *_):
        return checked_result(value, {"brand_signals": [], "information_units": []}), {}

    queue = JobQueue(TestSessionLocal, "owned-score-invariant")
    await Pipeline(queue, store, analyze=analyze).analyze_run(await queue.claim())
    await db_session.refresh(run)
    assert run.summary["mentions"] == []
    assert (
        run.summary["brand_presence"]["observations"][0]["state"] == "observed_absent"
    )
    assert run.manifest["owned_asset_revisions"][0]["id"] == asset.id
    assert (
        next(iter(run.manifest["approved_facts"].values()))["source_kind"]
        == "owned_asset_revision"
    )


@pytest.mark.asyncio
async def test_owned_apis_enforce_project_writer_and_safe_source_union(
    client, db_session, tmp_path, monkeypatch
):
    from app.intelligence import owned_assets

    monkeypatch.setattr(settings, "intelligence_enabled", True)
    monkeypatch.setattr(
        owned_assets, "configured_store", lambda: LocalBlobStore(tmp_path)
    )
    asset, store = await saved(db_session, tmp_path)
    await verify_asset(db_session, asset, verification(asset), "reviewer", store)
    fact = await approve_owned_fact(
        db_session, asset, approval(asset), "reviewer", store
    )
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: {
        "scope": "project",
        "pid": "p1",
        "role": "viewer",
        "sub": "viewer",
    }
    try:
        root = "/api/intelligence/projects/p1/owned-assets"
        assert (await client.get(root)).status_code == 200
        assert (await client.get(root + f"/{asset.id}/text")).json()["text"] == TEXT
        result = await client.get(f"/api/intelligence/facts/{fact.id}/source")
        assert (
            result.status_code == 200
            and result.json()["source"]["kind"] == "owned_asset_revision"
        )
        assert "citation_id" not in result.json()["source"]
        assert (
            await client.post(root, json=registration().model_dump(by_alias=True))
        ).status_code == 403
        assert (
            await client.get("/api/intelligence/projects/p2/owned-assets")
        ).status_code == 403
        app.dependency_overrides[get_current_user] = lambda: {
            "scope": "project",
            "pid": "p2",
            "role": "owner",
            "sub": "owner",
        }
        assert (
            await client.get(
                f"/api/intelligence/projects/p2/owned-assets/{asset.id}/text"
            )
        ).status_code == 404
        assert (
            await client.get(f"/api/intelligence/facts/{fact.id}/source")
        ).status_code == 404
    finally:
        app.dependency_overrides.pop(get_current_user, None)
