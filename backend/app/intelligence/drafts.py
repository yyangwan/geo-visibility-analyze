"""Named content authorization binds the exact TaskPack revision and own facts (v3)."""

import hashlib
import hmac
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
)
from sqlalchemy import select

from app.config import settings
from app.intelligence.brief_contracts import CHANNELS, EvidenceBriefV3, OwnFact
from app.intelligence.models import ActionPlan, DraftApproval, OwnedContentAsset
from app.intelligence.semantics import stable_hash
from app.utils.timezone import utc_isoformat, utcnow


class DraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    expected_version: StrictInt = Field(alias="expectedVersion", ge=0)
    expected_revision: StrictInt = Field(alias="expectedRevision", ge=1)
    fact_ids: list[str] = Field(alias="factIds", min_length=1, max_length=100)
    facts_scope_reviewed: StrictBool = Field(alias="factsScopeReviewed")
    channel: CHANNELS
    title: str = Field(min_length=5, max_length=200)
    reason: str = Field(min_length=5, max_length=2000)
    expires_at: AwareDatetime = Field(alias="expiresAt")
    idempotency_key: str = Field(
        alias="idempotencyKey", pattern=r"^[a-zA-Z0-9_-]{8,100}$"
    )


def grant_signature(grant):
    value = stable_hash(
        [
            "evidence-brief-v3",
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


async def selected_facts(db, action, run, ids, *, lock=False, blobs=None):
    from app.intelligence.blob import IntegrityError
    from app.intelligence.fact_qualification import task_fact
    from app.intelligence.value_contracts import TaskPackV2

    if not action.target.get("task_pack"):
        raise HTTPException(409, "Content requires a complete TaskPack v2 revision")
    pack = TaskPackV2.model_validate(action.target["task_pack"])
    if tuple(ids) != pack.own_fact_ids or not ids or len(set(ids)) != len(ids):
        raise HTTPException(
            409, "Select facts in a new action revision before content authorization"
        )
    result = []
    for key in ids:
        try:
            fact = await task_fact(
                db, run, pack, key, lock=lock, read=True, blobs=blobs
            )
        except IntegrityError as error:
            raise HTTPException(
                409, "Saved fact material integrity could not be verified"
            ) from error
        if not fact["eligible"]:
            raise HTTPException(
                409, "Selected own fact is stale, out of scope or unavailable"
            )
        url = None
        if fact["source_kind"] == "owned_asset_revision":
            asset = await db.scalar(
                select(OwnedContentAsset).where(
                    OwnedContentAsset.project_id == run.project_id,
                    OwnedContentAsset.id == fact["source_ref"]["asset_id"],
                )
            )
            url = asset.original_url if asset else None
        else:
            from app.intelligence.models import IntelligenceRun

            origin = await db.scalar(
                select(IntelligenceRun).where(
                    IntelligenceRun.project_id == run.project_id,
                    IntelligenceRun.id == fact["source_ref"]["evidence"]["run_id"],
                )
            )
            url = (
                next(
                    (
                        row.get("url")
                        for row in origin.manifest["sources"]
                        if row["id"] == fact["source_ref"]["citation_id"]
                    ),
                    None,
                )
                if origin
                else None
            )
        result.append(
            OwnFact.model_validate(
                {
                    "id": fact["id"],
                    "version": fact["version"],
                    "ownTargetId": pack.own_target_id,
                    "entityId": fact["entity_id"],
                    "statement": fact["statement"],
                    "scope": fact["scope"],
                    "validFrom": fact["valid_from"],
                    "validUntil": fact["valid_until"],
                    "source": fact["source_ref"],
                    "referenceUrl": url,
                }
            ).model_dump(mode="json")
        )
    return result


def action_binding(revision):
    pack = revision.payload["target"]["task_pack"]
    return {
        "id": revision.id,
        "revision": revision.revision,
        "signature": revision.signature,
        "dependencySignature": stable_hash(revision.dependencies),
        "taskKey": revision.task_key,
        "taskSignature": stable_hash(pack),
    }


def ai_targets(pack, run):
    values = []
    for row in pack.ai_targets:
        question = next(
            q for q in pack.delivery.questions if q.prompt_hash == row.prompt_hash
        )
        observations = [
            {"responseId": response["id"], "conditions": response.get("conditions", {})}
            for response in run.manifest["responses"]
            if response["platform"] == row.platform
            and response["prompt_hash"] == row.prompt_hash
        ]
        values.append(
            {
                "platform": row.platform,
                "promptHash": row.prompt_hash,
                "prompt": question.text,
                "scenarioId": row.scenario_id,
                "dimensions": list(row.dimensions),
                "observations": observations,
            }
        )
    return values


async def authorize_draft(db, action, run, data, actor, *, blobs=None):
    from app.intelligence.action_revisions import current_revision, lock_action_context
    from app.intelligence.actions import plan_view
    from app.intelligence.value_contracts import TaskPackV2

    _, actual_run = await current_revision(db, action)
    await lock_action_context(db, actual_run)
    action = await db.scalar(
        select(ActionPlan)
        .where(ActionPlan.project_id == action.project_id, ActionPlan.id == action.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    request_hash = stable_hash([action.id, data.model_dump(mode="json"), actor])
    previous = await db.scalar(
        select(DraftApproval)
        .where(
            DraftApproval.project_id == action.project_id,
            DraftApproval.idempotency_key == data.idempotency_key,
        )
        .with_for_update()
    )
    if previous:
        if previous.request_hash != request_hash:
            raise HTTPException(409, "Draft approval key conflict")
        return previous
    revision, run = await current_revision(db, action, lock=True)
    latest = await db.scalar(
        select(DraftApproval.version)
        .where(
            DraftApproval.project_id == action.project_id,
            DraftApproval.action_id == action.id,
        )
        .order_by(DraftApproval.version.desc())
        .limit(1)
        .with_for_update()
    )
    if (
        revision.revision != data.expected_revision
        or (latest or 0) != data.expected_version
    ):
        raise HTTPException(409, "Action or draft approval version changed")
    if (
        not actor
        or not data.facts_scope_reviewed
        or min(len(data.reason.strip()), len(data.title.strip())) < 5
    ):
        raise HTTPException(422, "Named scope review and rationale are required")
    if not (await plan_view(db, action, run))["approval_valid"]:
        raise HTTPException(409, "Current investigation plan approval required")
    if not action.target.get("task_pack"):
        raise HTTPException(409, "Content requires a complete TaskPack v2")
    pack = TaskPackV2.model_validate(action.target["task_pack"])
    if pack.intent in ("material_verification", "channel_outreach") or any(
        row.scenario_id == "unreviewed" for row in pack.ai_targets
    ):
        raise HTTPException(
            409,
            "Review investigation intent and original questions before content creation",
        )
    facts = await selected_facts(db, action, run, data.fact_ids, lock=True, blobs=blobs)
    expiry = data.expires_at.astimezone(UTC).replace(tzinfo=None)
    if expiry <= utcnow() or any(
        expiry > parse_utc(fact["validUntil"]) for fact in facts
    ):
        raise HTTPException(422, "Approval expiry must be within current fact validity")
    target = revision.payload["target"]
    payload = {
        "schemaVersion": 3,
        "projectId": run.project_id,
        "runId": run.id,
        "actionId": action.id,
        "title": data.title.strip(),
        "actionRevision": action_binding(revision),
        "taskPack": pack.model_dump(mode="json"),
        "execution": {
            "channel": data.channel,
            "mode": "generate_draft"
            if data.channel in ("wechat", "weibo", "xiaohongshu", "douyin")
            else "manual_task",
        },
        "aiTargets": ai_targets(pack, run),
        "diagnosticContext": {
            "observedFacts": target.get("observed_facts", []),
            "hypotheses": target.get("hypotheses", []),
            "unknowns": target.get("unknowns", []),
            "counterevidence": target.get("counterevidence", []),
            "use": "diagnostic_only",
        },
        "ownFacts": facts,
        "publicationAllowed": False,
    }
    grant = DraftApproval(
        id=uuid4().hex,
        project_id=run.project_id,
        action_id=action.id,
        version=data.expected_version + 1,
        payload=payload,
        input_stamp=stable_hash([revision.signature, facts]),
        actor_id=actor,
        reason=data.reason.strip(),
        expires_at=expiry,
        idempotency_key=data.idempotency_key,
        request_hash=request_hash,
        signature="",
    )
    grant.signature = grant_signature(grant)
    try:
        EvidenceBriefV3.model_validate(grant_payload(grant))
    except ValidationError as error:
        raise HTTPException(
            422, "Evidence Brief exceeds v3 bounds; refine the task without truncation"
        ) from error
    from app.intelligence.reviews import add_event

    await add_event(db, grant)
    return grant


def parse_utc(value):
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


async def export_brief(db, grant, action, run, *, blobs=None):
    from app.intelligence.action_revisions import current_revision
    from app.intelligence.actions import plan_view

    revision, run = await current_revision(db, action)
    latest = await db.scalar(
        select(DraftApproval.id)
        .where(
            DraftApproval.project_id == action.project_id,
            DraftApproval.action_id == action.id,
        )
        .order_by(DraftApproval.version.desc())
        .limit(1)
    )
    if (
        not action.target.get("task_pack")
        or grant.payload.get("schemaVersion") != 3
        or grant.payload.get("actionRevision") != action_binding(revision)
        or latest != grant.id
        or grant.expires_at <= utcnow()
        or not hmac.compare_digest(grant.signature, grant_signature(grant))
    ):
        raise HTTPException(
            409,
            "Content authorization expired, superseded or bound to another revision",
        )
    if not (await plan_view(db, action, run))["approval_valid"]:
        raise HTTPException(409, "Current plan approval is stale")
    facts = await selected_facts(
        db, action, run, [fact["id"] for fact in grant.payload["ownFacts"]], blobs=blobs
    )
    if facts != grant.payload["ownFacts"] or grant.input_stamp != stable_hash(
        [revision.signature, facts]
    ):
        raise HTTPException(409, "Approved own fact material changed")
    try:
        return EvidenceBriefV3.model_validate(grant_payload(grant)).model_dump(
            mode="json", by_alias=True
        )
    except ValidationError as error:
        raise HTTPException(
            409, "Content authorization contract integrity changed"
        ) from error
