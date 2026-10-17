from datetime import UTC, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.intelligence.ingestion import prepare_run
from app.intelligence.models import (
    BrandFact,
    ClaimEvidence,
    ClaimReview,
    ContentSegment,
    EntityResolutionEvent,
    IntelligenceEntity,
    SourceFetch,
    SourceSnapshot,
)
from app.intelligence.reviews import (
    FactRequest,
    ReviewRequest,
    WithdrawRequest,
    approve_fact,
    fact_view,
    review_claim,
    review_snapshot,
    withdraw_fact,
)
from app.intelligence.semantics import stable_hash
from app.utils.timezone import utcnow
from tests.test_intelligence_foundation import fixture_audit


async def fixture_evidence(db):
    text = "云衡提供三年质保。"
    source_text = "云衡提供三年质保，仅适用于中国大陆X系列。"
    audit = await fixture_audit(
        db,
        text=text,
        sources=[
            {
                "url": "https://example.com/article",
                "kind": "answer_reference",
                "url_resolution": "exact",
            }
        ],
    )
    first = await prepare_run(db, "p1", audit.id)
    entity = IntelligenceEntity(
        project_id="p1", name="云衡", kind="brand", identity_hash="a" * 64
    )
    snapshot = SourceSnapshot(
        project_id="p1",
        document_id=first.manifest["sources"][0]["document_id"],
        body_hash="b" * 64,
        representation_hash="c" * 64,
        text_key="test.gz",
        text_sha256="d" * 64,
        extractor_version="test",
    )
    db.add_all([entity, snapshot])
    await db.flush()
    db.add(
        EntityResolutionEvent(
            project_id="p1",
            entity_id=entity.id,
            run_id=first.id,
            version=1,
            decision="confirmed",
            canonical_name="云衡",
            reason="按原文确认公司身份",
            actor_id="reviewer",
            idempotency_key="identity-key",
            request_hash="e" * 64,
        )
    )
    db.add(
        SourceFetch(
            project_id="p1",
            audit_id=audit.id,
            document_id=snapshot.document_id,
            snapshot_id=snapshot.id,
            status="captured",
        )
    )
    await db.flush()
    run = await prepare_run(db, "p1", audit.id, enrich=False)
    run.status = "partial"
    answer = ContentSegment(
        project_id="p1",
        owner_kind="answer",
        owner_id=run.manifest["responses"][0]["id"],
        ordinal=0,
        start_offset=0,
        end_offset=len(text),
        text=text,
        segment_hash=stable_hash(text),
        extractor_version="test",
    )
    source = ContentSegment(
        project_id="p1",
        owner_kind="snapshot",
        owner_id=snapshot.id,
        ordinal=0,
        start_offset=0,
        end_offset=len(source_text),
        text=source_text,
        segment_hash=stable_hash(source_text),
        extractor_version="test",
    )
    db.add_all([answer, source])
    await db.flush()
    evidence = ClaimEvidence(
        project_id="p1",
        run_id=run.id,
        claim_hash=stable_hash(text),
        answer_segment_id=answer.id,
        source_segment_id=source.id,
        citation_id=run.manifest["sources"][0]["id"],
        answer_quote=text,
        source_quote=source_text,
        relation="contradicts",
        method="model_verification",
    )
    db.add(evidence)
    await db.commit()
    return run, evidence, entity, source


def review_request(quote, version=0, key="review-key-123", relation="supports"):
    return ReviewRequest(
        relation=relation,
        sourceQuote=quote,
        reason="已检查主体、时间及限定条件",
        expectedVersion=version,
        idempotencyKey=key,
    )


def fact_request(review, entity):
    now = utcnow().replace(tzinfo=UTC)
    return FactRequest(
        reviewId=review.id,
        entityId=entity.id,
        scope="中国大陆X系列产品，按质保协议",
        validFrom=now - timedelta(days=1),
        validUntil=now + timedelta(days=30),
        reason="核对适用系列及质保期限",
        expectedVersion=0,
        idempotencyKey="fact-key-123",
    )


@pytest.mark.asyncio
async def test_review_idempotency_exact_quotes_and_immutable_model_verdict(db_session):
    run, evidence, _, source = await fixture_evidence(db_session)
    data = review_request(source.text)
    review = await review_claim(db_session, run, evidence.id, data, "reviewer")
    await db_session.commit()
    assert (
        await review_claim(db_session, run, evidence.id, data, "reviewer")
    ).id == review.id
    assert (
        evidence.relation == "contradicts" and evidence.review_state == "needs_review"
    )
    assert (await review_snapshot(db_session, "p1", run.id))[evidence.id][
        "relation"
    ] == "supports"
    with pytest.raises(HTTPException) as fake:
        await review_claim(
            db_session,
            run,
            evidence.id,
            review_request("伪造来源片段", 1, "fake-key-123"),
            "reviewer",
        )
    assert fake.value.status_code == 422
    with pytest.raises(HTTPException) as stale:
        await review_claim(
            db_session,
            run,
            evidence.id,
            review_request(source.text, 0, "stale-key-123"),
            "reviewer",
        )
    assert stale.value.status_code == 409


@pytest.mark.asyncio
async def test_fact_approval_withdrawal_and_expiration_keep_history(db_session):
    run, evidence, entity, source = await fixture_evidence(db_session)
    review = await review_claim(
        db_session, run, evidence.id, review_request(source.text), "reviewer"
    )
    await db_session.commit()
    data = fact_request(review, entity)
    fact = await approve_fact(db_session, run, data, "reviewer")
    await db_session.commit()
    assert (await approve_fact(db_session, run, data, "reviewer")).id == fact.id
    assert (await fact_view(db_session, fact))["status"] == "valid"
    new_run = await prepare_run(db_session, "p1", run.audit_id, enrich=False)
    assert new_run.manifest["approved_facts"][fact.fact_key]["id"] == fact.id
    request = WithdrawRequest(
        expectedVersion=1,
        reason="质保政策更新，撤回旧陈述",
        idempotencyKey="withdraw-key-123",
    )
    withdrawn = await withdraw_fact(db_session, run, fact, request, "reviewer")
    await db_session.commit()
    assert (
        await withdraw_fact(db_session, run, fact, request, "reviewer")
    ).id == withdrawn.id
    assert withdrawn.version == 2 and fact.state == "approved"
    assert (await fact_view(db_session, fact))["status"] == "superseded"
    assert (await fact_view(db_session, withdrawn))["status"] == "withdrawn"
    assert len((await db_session.scalars(select(BrandFact))).all()) == 2


@pytest.mark.asyncio
async def test_changed_review_invalidates_fact_and_partial_cannot_be_approved(
    db_session,
):
    run, evidence, entity, source = await fixture_evidence(db_session)
    review = await review_claim(
        db_session, run, evidence.id, review_request(source.text), "reviewer"
    )
    fact = await approve_fact(db_session, run, fact_request(review, entity), "reviewer")
    await db_session.commit()
    await review_claim(
        db_session,
        run,
        evidence.id,
        review_request(source.text, 1, "new-review-key", "partial"),
        "reviewer",
    )
    assert (await fact_view(db_session, fact))["status"] == "needs_review"
    assert review.relation == "supports"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", [{"platform": "deepseek"}, {"document_id": "f" * 32}]
)
async def test_cross_platform_evidence_provenance_rejected(db_session, change):
    run, evidence, _, source = await fixture_evidence(db_session)
    run.manifest = {
        **run.manifest,
        "sources": [{**run.manifest["sources"][0], **change}],
    }
    with pytest.raises(HTTPException) as error:
        await review_claim(
            db_session, run, evidence.id, review_request(source.text), "reviewer"
        )
    assert error.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "project,role,actor,expected",
    [
        ("p1", "owner", "reviewer", 202),
        ("p2", "owner", "reviewer", 404),
        ("p1", "viewer", "reviewer", 403),
        ("p1", "owner", "", 403),
    ],
)
async def test_review_api_scope(
    client, db_session, monkeypatch, project, role, actor, expected
):
    from app.api.auth import get_current_user
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "intelligence_enabled", True)
    run, evidence, _, source = await fixture_evidence(db_session)
    app.dependency_overrides[get_current_user] = lambda: {
        "scope": "project",
        "pid": project,
        "role": role,
        "sub": actor,
    }
    try:
        response = await client.post(
            f"/api/intelligence/runs/{run.id}/evidence/{evidence.id}/review",
            json=review_request(source.text).model_dump(by_alias=True),
        )
        assert response.status_code == expected
        assert bool(await db_session.scalar(select(ClaimReview))) == (expected == 202)
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_fact_validity_expiration_and_source_integrity(db_session, monkeypatch):
    import app.intelligence.reviews as reviews_module

    run, evidence, entity, source = await fixture_evidence(db_session)
    review = await review_claim(
        db_session, run, evidence.id, review_request(source.text), "reviewer"
    )
    fact = await approve_fact(db_session, run, fact_request(review, entity), "reviewer")
    await db_session.commit()
    monkeypatch.setattr(
        reviews_module, "utcnow", lambda: fact.valid_until + timedelta(seconds=1)
    )
    assert (await fact_view(db_session, fact))["status"] == "expired"
    snapshot = await db_session.scalar(
        select(SourceSnapshot).where(SourceSnapshot.id == source.owner_id)
    )
    snapshot.integrity_status = "corrupt"
    assert (await fact_view(db_session, fact))["status"] == "needs_review"


@pytest.mark.asyncio
async def test_partial_support_cannot_be_approved_as_fact(db_session):
    run, evidence, entity, source = await fixture_evidence(db_session)
    review = await review_claim(
        db_session,
        run,
        evidence.id,
        review_request(source.text, relation="partial"),
        "reviewer",
    )
    with pytest.raises(HTTPException) as error:
        await approve_fact(db_session, run, fact_request(review, entity), "reviewer")
    assert error.value.status_code == 409
    assert await db_session.scalar(select(BrandFact)) is None


@pytest.mark.asyncio
async def test_human_review_change_invalidates_existing_plan_approval(db_session):
    from app.intelligence.actions import (
        ApprovalRequest,
        PlanRequest,
        approve_plan,
        create_plan,
        persist_diagnoses,
        plan_view,
    )

    run, evidence, _, source = await fixture_evidence(db_session)
    summary = {
        "diagnoses": [
            {"id": "f" * 64, "platform": "qwen", "action": {"task": "核实质保范围"}}
        ]
    }
    await persist_diagnoses(db_session, run, summary)
    plan = await create_plan(
        db_session,
        run,
        PlanRequest(
            diagnosisId=summary["diagnoses"][0]["record_id"],
            idempotencyKey="plan-key-123",
        ),
        "reviewer",
    )
    await approve_plan(
        db_session,
        plan,
        run,
        ApprovalRequest(
            expectedRevision=1,
            reason="按当前证据开展核实",
            scope="investigation_plan",
            idempotencyKey="plan-approval-key",
        ),
        "reviewer",
        "owner",
    )
    assert (await plan_view(db_session, plan, run))["approval_valid"]
    await review_claim(
        db_session, run, evidence.id, review_request(source.text), "reviewer"
    )
    assert (await plan_view(db_session, plan, run))["status"] == "stale"
