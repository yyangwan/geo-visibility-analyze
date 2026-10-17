"""Formal content-draft authorization and a server-derived evidence Brief v2."""

import hashlib
import hmac
from datetime import UTC
from typing import Literal

from fastapi import HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from app.config import settings
from app.intelligence.actions import plan_view, stamp
from app.intelligence.capture_contract import CaptureConditions
from app.intelligence.models import (
    AuditIntelligenceState,
    BrandFact,
    ClaimEvidence,
    ClaimReview,
    DraftApproval,
    IntelligenceRun,
)
from app.intelligence.refresh import state_for_audit
from app.intelligence.reviews import add_event, evidence_material, fact_view
from app.intelligence.semantics import stable_hash
from app.utils.timezone import utc_isoformat, utcnow

CHANNELS = Literal[
    "wechat", "weibo", "xiaohongshu", "douyin", "website", "zhihu", "toutiao"
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class DraftRequest(Strict):
    expected_version: int = Field(alias="expectedVersion", ge=0)
    fact_ids: list[str] = Field(alias="factIds", min_length=1, max_length=20)
    channel: CHANNELS
    title: str = Field(min_length=5, max_length=200)
    reason: str = Field(min_length=5, max_length=2000)
    expires_at: AwareDatetime = Field(alias="expiresAt")
    idempotency_key: str = Field(
        alias="idempotencyKey", pattern=r"^[a-zA-Z0-9_-]{8,100}$"
    )


class FactSource(Strict):
    citationId: str = Field(pattern=r"^[a-f0-9]{32}$")
    snapshotId: str = Field(pattern=r"^[a-f0-9]{32}$")
    url: str = Field(min_length=1, max_length=4096, pattern=r"^https?://")


class DraftFact(Strict):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    version: int = Field(ge=1)
    entityId: str = Field(pattern=r"^[a-f0-9]{32}$")
    statement: str = Field(min_length=1, max_length=2000)
    scope: str = Field(min_length=5, max_length=1000)
    validFrom: AwareDatetime
    validUntil: AwareDatetime
    source: FactSource


class DraftTarget(Strict):
    platform: Literal["qwen", "deepseek", "doubao", "kimi", "hunyuan"]
    promptHash: str = Field(pattern=r"^[a-f0-9]{64}$")
    prompt: str = Field(min_length=1, max_length=5000)
    conditions: CaptureConditions = Field(default_factory=CaptureConditions)


class DraftExecution(Strict):
    channel: CHANNELS
    mode: Literal["generate_draft", "manual_task"]


class DraftAuthorization(Strict):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    version: int = Field(ge=1)
    scope: Literal["content_draft"]
    signature: str = Field(pattern=r"^[a-f0-9]{64}$")
    expiresAt: AwareDatetime


class EvidenceBriefV2(Strict):
    schemaVersion: Literal[2]
    projectId: str = Field(min_length=1, max_length=50)
    runId: str = Field(pattern=r"^[a-f0-9]{32}$")
    actionId: str = Field(pattern=r"^[a-f0-9]{32}$")
    title: str = Field(min_length=5, max_length=200)
    execution: DraftExecution
    aiTargets: list[DraftTarget] = Field(min_length=1, max_length=5)
    facts: list[DraftFact] = Field(min_length=1, max_length=20)
    approval: DraftAuthorization
    publicationAllowed: Literal[False]


def grant_signature(grant):
    value = stable_hash(
        [
            grant.id,
            grant.action_id,
            grant.version,
            grant.payload,
            grant.input_stamp,
            grant.actor_id,
            grant.reason,
            utc_isoformat(grant.expires_at),
            "content_draft",
        ]
    )
    return hmac.new(
        settings.secret_key.encode(), value.encode(), hashlib.sha256
    ).hexdigest()


async def selected_facts(db, action, run, ids):
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "Fact IDs must be unique")
    result = []
    for fact_id in ids:
        fact = await db.scalar(
            select(BrandFact).where(
                BrandFact.project_id == run.project_id, BrandFact.id == fact_id
            )
        )
        if fact is None:
            raise HTTPException(404, "Fact outside project")
        if (await fact_view(db, fact))["status"] != "valid":
            raise HTTPException(409, "Fact no longer valid")
        review = await db.scalar(
            select(ClaimReview).where(
                ClaimReview.project_id == run.project_id,
                ClaimReview.id == fact.review_id,
            )
        )
        evidence = await db.scalar(
            select(ClaimEvidence).where(
                ClaimEvidence.project_id == run.project_id,
                ClaimEvidence.id == review.evidence_id,
            )
        )
        origin = await db.scalar(
            select(IntelligenceRun).where(
                IntelligenceRun.project_id == run.project_id,
                IntelligenceRun.id == review.run_id,
            )
        )
        _, citation, _ = await evidence_material(db, origin, evidence)
        target_source = next(
            (
                source
                for source in run.manifest["sources"]
                if source["id"] == citation["id"]
            ),
            None,
        )
        if (
            not target_source
            or target_source["snapshot_id"] != citation["snapshot_id"]
            or citation["id"] not in action.target.get("source_ids", [])
            or citation["platform"] != action.target.get("platform")
        ):
            raise HTTPException(
                409, "Facts must belong to this platform target and snapshot"
            )
        result.append(
            {
                "id": fact.id,
                "version": fact.version,
                "entityId": fact.entity_id,
                "statement": fact.statement,
                "scope": fact.scope,
                "validFrom": utc_isoformat(fact.valid_from),
                "validUntil": utc_isoformat(fact.valid_until),
                "source": {
                    "citationId": citation["id"],
                    "snapshotId": citation["snapshot_id"],
                    "url": citation["url"],
                },
            }
        )
    return result


async def authorize_draft(db, action, run, data, actor):
    state = await state_for_audit(db, run.project_id, run.audit_id)
    request_hash = stable_hash([action.id, data.model_dump(mode="json"), actor])
    previous = await db.scalar(
        select(DraftApproval)
        .where(
            DraftApproval.project_id == run.project_id,
            DraftApproval.idempotency_key == data.idempotency_key,
        )
        .with_for_update()
    )
    if previous:
        if previous.request_hash != request_hash:
            raise HTTPException(409, "Draft approval key conflict")
        return previous
    latest = await db.scalar(
        select(DraftApproval.version)
        .where(
            DraftApproval.project_id == run.project_id,
            DraftApproval.action_id == action.id,
        )
        .order_by(DraftApproval.version.desc())
        .with_for_update()
        .limit(1)
    )
    if (latest or 0) != data.expected_version:
        raise HTTPException(409, "Draft approval version changed")
    if action.target.get("kind") not in (
        "negative_observation",
        "claim_review",
        "source_concentration",
    ):
        raise HTTPException(
            409, "Collection and identity tasks cannot become content Briefs"
        )
    if not (await plan_view(db, action, run))["approval_valid"]:
        raise HTTPException(409, "Current investigation plan approval required")
    facts = await selected_facts(db, action, run, data.fact_ids)
    expiry = data.expires_at.astimezone(UTC).replace(tzinfo=None)
    if (
        expiry <= utcnow()
        or any(expiry > parse_utc(fact["validUntil"]) for fact in facts)
        or len(data.reason.strip()) < 5
        or len(data.title.strip()) < 5
    ):
        raise HTTPException(
            422, "Approval requires rationale and expiry within fact validity"
        )
    value, fresh = await stamp(db, run, state)
    if not fresh:
        raise HTTPException(409, "Draft evidence stale")
    payload = {
        "schemaVersion": 2,
        "projectId": run.project_id,
        "runId": run.id,
        "actionId": action.id,
        "title": data.title.strip(),
        "execution": {
            "channel": data.channel,
            "mode": "generate_draft"
            if data.channel in ("wechat", "weibo", "xiaohongshu", "douyin")
            else "manual_task",
        },
        "aiTargets": [
            {
                "platform": action.target["platform"],
                "promptHash": action.target["prompt_hash"],
                "prompt": action.target["prompt"],
                "conditions": action.target.get("conditions", {}),
            }
        ],
        "facts": facts,
        "publicationAllowed": False,
    }
    # Validate every bound, never truncate a fact or prompt to fit the consumer.
    from uuid import uuid4

    grant = DraftApproval(
        id=uuid4().hex,
        project_id=run.project_id,
        action_id=action.id,
        version=data.expected_version + 1,
        payload=payload,
        input_stamp=value,
        actor_id=actor,
        reason=data.reason.strip(),
        expires_at=expiry,
        idempotency_key=data.idempotency_key,
        request_hash=request_hash,
        signature="",
    )
    grant.signature = grant_signature(grant)
    try:
        EvidenceBriefV2.model_validate(grant_payload(grant))
    except ValidationError as error:
        raise HTTPException(
            422,
            "Evidence Brief exceeds contract bounds; refine the task without truncation",
        ) from error
    await add_event(db, grant)
    return grant


def parse_utc(value):
    from datetime import datetime

    return datetime.fromisoformat(value).astimezone(UTC).replace(tzinfo=None)


def grant_payload(grant):
    return {
        **grant.payload,
        "approval": {
            "id": grant.id,
            "version": grant.version,
            "scope": "content_draft",
            "signature": grant.signature,
            "expiresAt": utc_isoformat(grant.expires_at),
        },
    }


async def export_brief(db, grant, action, run):
    # Export is read-only: missing context must not be silently created by a GET.
    state = await db.scalar(
        select(AuditIntelligenceState).where(
            AuditIntelligenceState.project_id == run.project_id,
            AuditIntelligenceState.audit_id == run.audit_id,
        )
    )
    newest_run = await db.scalar(
        select(IntelligenceRun.id)
        .where(
            IntelligenceRun.project_id == run.project_id,
            IntelligenceRun.audit_id == run.audit_id,
        )
        .order_by(IntelligenceRun.revision.desc())
        .limit(1)
    )
    if state is None or newest_run != run.id or run.status not in ("ready", "partial"):
        raise HTTPException(409, "Current completed evidence context required")
    value, fresh = await stamp(db, run, state)
    latest = await db.scalar(
        select(DraftApproval.id)
        .where(
            DraftApproval.project_id == run.project_id,
            DraftApproval.action_id == action.id,
        )
        .order_by(DraftApproval.version.desc())
        .limit(1)
    )
    if (
        not fresh
        or latest != grant.id
        or grant.expires_at <= utcnow()
        or grant.input_stamp != value
        or not hmac.compare_digest(grant.signature, grant_signature(grant))
    ):
        raise HTTPException(409, "Content draft approval expired or stale")
    if not (await plan_view(db, action, run, state=state))["approval_valid"]:
        raise HTTPException(409, "Plan approval stale")
    facts = await selected_facts(
        db, action, run, [fact["id"] for fact in grant.payload["facts"]]
    )
    if facts != grant.payload["facts"]:
        raise HTTPException(409, "Approved fact payload changed")
    return EvidenceBriefV2.model_validate(grant_payload(grant)).model_dump(mode="json")
