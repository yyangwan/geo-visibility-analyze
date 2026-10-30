"""Signed human publication reports bound to an immutable delivered task version."""

import hashlib
import hmac
from copy import deepcopy
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import HTTPException
from pydantic import Field, StrictBool, field_validator
from sqlalchemy import select

from app.config import settings
from app.intelligence.action_revisions import (
    current_revision,
    lock_action_context,
    revision_integrity,
)
from app.intelligence.actions import plan_view
from app.intelligence.capture_contract import StrictFact
from app.intelligence.execution import (
    execution_revision,
    head_for,
)
from app.intelligence.execution import (
    signature as execution_signature,
)
from app.intelligence.models import ActionExecutionEvent, ActionPlan, PublishedAsset
from app.intelligence.semantics import stable_hash
from app.utils.timezone import utc_isoformat, utcnow

VERSION = "publication-provenance-v1"
REVIEWERS = {"owner", "admin", "editor"}


class PublicationRequest(StrictFact):
    action_id: str = Field(alias="actionId", pattern=r"^[a-f0-9]{32}$")
    expected_revision: int = Field(alias="expectedRevision", ge=1)
    delivery_event_id: str = Field(alias="deliveryEventId", pattern=r"^[a-f0-9]{32}$")
    artifact_index: int = Field(alias="artifactIndex", ge=0, le=9)
    version_confirmed: StrictBool = Field(alias="versionConfirmed")
    url: str = Field(min_length=1, max_length=4096)
    channel: str = Field(
        pattern=r"^(wechat|weibo|xiaohongshu|douyin|website|zhihu|toutiao)$"
    )
    published_at: datetime = Field(alias="publishedAt")
    reason: str = Field(min_length=5, max_length=2000)
    idempotency_key: str = Field(
        alias="idempotencyKey", pattern=r"^[a-zA-Z0-9_-]{8,100}$"
    )

    @field_validator("url")
    @classmethod
    def public_link(cls, value):
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError(
                "An HTTP publication address without credentials is required"
            )
        return value

    @field_validator("published_at")
    @classmethod
    def observed_time(cls, value):
        if (
            value.utcoffset() is None
            or value.astimezone(UTC).replace(tzinfo=None) > utcnow()
        ):
            raise ValueError("An actual publication time with timezone is required")
        return value

    @field_validator("version_confirmed")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Confirm the delivered version actually published")
        return value

    @field_validator("reason")
    @classmethod
    def substantive_reason(cls, value):
        if len(value.strip()) < 5:
            raise ValueError("A substantive named publication rationale is required")
        return value.strip()


async def scoped_action(db, project, action_id, *, lock=False):
    query = select(ActionPlan).where(
        ActionPlan.project_id == project, ActionPlan.id == action_id
    )
    action = await db.scalar(
        query.with_for_update().execution_options(populate_existing=True)
        if lock
        else query
    )
    if action is None:
        raise HTTPException(404, "Action not found")
    return action


async def lock_publication_action(db, project, action_id, *, other_audit_ids=()):
    action = await scoped_action(db, project, action_id)
    _revision, run = await current_revision(db, action)
    await lock_action_context(db, run, other_audit_ids)
    return await scoped_action(db, project, action_id, lock=True)


async def delivered_version(db, action, number, *, lock=False):
    revision = await execution_revision(db, action, number, lock=lock)
    head = await head_for(db, action, revision, lock=lock)
    delivery = head.snapshot.get("delivery") if head else None
    if not delivery or head.snapshot["status"] not in ("delivered", "accepted"):
        raise HTTPException(
            409, "This task revision has no available delivered content version"
        )
    event = await db.scalar(
        select(ActionExecutionEvent).where(
            ActionExecutionEvent.project_id == action.project_id,
            ActionExecutionEvent.execution_id == head.id,
            ActionExecutionEvent.id == delivery["event_id"],
        )
    )
    if (
        not event
        or event.operation != "deliver"
        or not hmac.compare_digest(
            event.signature, execution_signature(event, revision)
        )
        or event.after.get("delivery") != delivery
    ):
        raise HTTPException(409, "Delivered version integrity changed")
    return revision, head, event


def may_register(role, actor, head):
    return (
        role in REVIEWERS
        or role == "member"
        and (head.snapshot.get("owner") or {}).get("id") == actor
    )


async def publication_options(db, project, action_id, number, actor, role):
    action = await scoped_action(db, project, action_id)
    number = number or action.revision
    revision = await execution_revision(db, action, number)
    result = {
        "action_id": action.id,
        "revision": number,
        "revision_id": revision.id,
        "action_title": revision.payload["title"],
        "is_current": action.revision == number,
        "can_register": False,
        "deliveries": [],
        "publication_allowed": False,
        "task_pack": deepcopy(revision.payload["target"].get("task_pack")),
    }
    try:
        revision, head, event = await delivered_version(db, action, number)
    except HTTPException as error:
        if error.status_code != 409 or "no available delivered" not in str(
            error.detail
        ):
            raise
        return {**result, "unavailable_reason": "delivery_not_ready"}
    result.update(
        can_register=action.revision == number and may_register(role, actor, head),
        execution_status=head.snapshot["status"],
        execution_version=head.version,
    )
    result["deliveries"] = [
        {
            "delivery_event_id": event.id,
            "artifact_index": index,
            "title": item["title"],
            "kind": item["kind"],
            "submitted_at": head.snapshot["delivery"]["submitted_at"],
            "verification": item["verification"],
            "version_fingerprint": stable_hash([revision.id, event.id, index, item]),
            "acceptance_state": head.snapshot["status"],
        }
        for index, item in enumerate(head.snapshot["delivery"]["artifacts"])
    ]
    return result


def report_payload(row):
    provenance = {
        key: value for key, value in row.provenance.items() if key != "signature"
    }
    return [
        VERSION,
        row.id,
        row.project_id,
        row.action_id,
        row.url,
        row.channel,
        row.content_hash,
        utc_isoformat(row.published_at),
        row.actor_id,
        row.reason,
        row.idempotency_key,
        row.request_hash,
        provenance,
    ]


def report_signature(row):
    return hmac.new(
        settings.secret_key.encode(),
        stable_hash(report_payload(row)).encode(),
        hashlib.sha256,
    ).hexdigest()


def asset_view(row):
    if (
        not isinstance(row.provenance, dict)
        or row.provenance.get("version") != VERSION
        or not hmac.compare_digest(
            str(row.provenance.get("signature") or ""), report_signature(row)
        )
    ):
        raise HTTPException(409, "Publication report integrity changed")
    return {
        "id": row.id,
        "action_id": row.action_id,
        "url": row.url,
        "channel": row.channel,
        "published_at": utc_isoformat(row.published_at),
        "reason": row.reason,
        "actor_id": row.actor_id,
        "version_fingerprint": row.content_hash,
        "verification": "human_attested_delivery_version",
        "published_bytes_verified": False,
        "publication_authorized": False,
        "provenance": deepcopy(row.provenance),
    }


async def record_asset(db, project, data, actor, role):
    action = await lock_publication_action(db, project, data.action_id)
    request_hash = stable_hash([actor, data.model_dump(mode="json", by_alias=True)])
    previous = await db.scalar(
        select(PublishedAsset)
        .where(
            PublishedAsset.project_id == project,
            PublishedAsset.idempotency_key == data.idempotency_key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if previous:
        if previous.request_hash != request_hash:
            raise HTTPException(409, "Publication operation key conflict")
        asset_view(previous)
        return previous
    revision, run = await current_revision(db, action, lock=True)
    if action.revision != data.expected_revision or not revision_integrity(
        action, revision
    ):
        raise HTTPException(409, "Action revision changed; refresh publication choices")
    revision, head, event = await delivered_version(
        db, action, data.expected_revision, lock=True
    )
    if not may_register(role, actor, head):
        raise HTTPException(403, "Assigned member or authorized reviewer required")
    artifacts = head.snapshot["delivery"]["artifacts"]
    if event.id != data.delivery_event_id or data.artifact_index >= len(artifacts):
        raise HTTPException(409, "Selected delivered version changed")
    artifact = deepcopy(artifacts[data.artifact_index])
    fingerprint = stable_hash([revision.id, event.id, data.artifact_index, artifact])
    provenance = {
        "version": VERSION,
        "revision_id": revision.id,
        "revision": revision.revision,
        "run_id": revision.run_id,
        "action_title": revision.payload["title"],
        "task_key": revision.task_key,
        "delivery_event_id": event.id,
        "artifact_index": data.artifact_index,
        "artifact": artifact,
        "execution_version": head.version,
        "delivery_submitted_at": head.snapshot["delivery"]["submitted_at"],
        "execution_status": head.snapshot["status"],
        "acceptance": deepcopy(head.snapshot.get("acceptance")),
        "plan_approval_valid_at_registration": (await plan_view(db, action, run))[
            "approval_valid"
        ],
        "version_claim": "same_as_selected_delivery",
        "fingerprint_kind": "delivery_record_not_published_bytes",
    }
    row = PublishedAsset(
        id=uuid4().hex,
        project_id=project,
        action_id=action.id,
        url=data.url,
        channel=data.channel,
        content_hash=fingerprint,
        published_at=data.published_at.astimezone(UTC).replace(tzinfo=None),
        reason=data.reason,
        actor_id=actor,
        idempotency_key=data.idempotency_key,
        request_hash=request_hash,
        provenance=provenance,
    )
    row.provenance = {**provenance, "signature": report_signature(row)}
    db.add(row)
    await db.flush()
    return row


async def freeze_action_link(db, action, data):
    revision, _run = await current_revision(db, action, lock=True)
    if action.revision != data.expected_action_revision or not revision_integrity(
        action, revision
    ):
        raise HTTPException(409, "Action revision changed; refresh observation scope")
    pack = revision.payload["target"].get("task_pack")
    if not pack or pack["own_target_id"] not in data.brand_target_ids:
        raise HTTPException(
            422, "Include the original task own-brand target in this observation"
        )
    assets = []
    for asset_id in data.publication_asset_ids:
        row = await db.scalar(
            select(PublishedAsset).where(
                PublishedAsset.project_id == action.project_id,
                PublishedAsset.id == asset_id,
            )
        )
        if row is None or row.action_id != action.id:
            raise HTTPException(404, "Publication record not found in this action")
        view = asset_view(row)
        if view["provenance"]["revision_id"] != revision.id:
            raise HTTPException(409, "Publication belongs to another task revision")
        assets.append(view)
    return {
        "policy_version": "publication-window-v1",
        "action_id": action.id,
        "revision": revision.revision,
        "revision_id": revision.id,
        "action_title": revision.payload["title"],
        "task_key": revision.task_key,
        "task_pack_hash": stable_hash(pack),
        "ai_targets": deepcopy(pack["ai_targets"]),
        "publications": assets,
        "publication_authorized": False,
    }


def add_publication_windows(result, spec):
    from app.intelligence.measurement import timestamp

    link = spec.get("action_link")
    if not link:
        return result
    result["action_link"] = deepcopy(link)
    for platform in result["platforms"]:
        for observation in platform.get("observations", []):
            before, after = (
                timestamp(observation.get("baseline_observed_at")),
                timestamp(observation.get("followup_observed_at")),
            )
            windows = []
            for asset in link["publications"]:
                published = timestamp(asset["published_at"])
                # Delivery submission is a record timestamp, not proof of when content existed.
                status = (
                    "sampling_time_unknown"
                    if not before or not after or not published
                    else "time_ordered"
                    if before < published < after
                    else "publication_not_between_samples"
                )
                windows.append(
                    {
                        "asset_id": asset["id"],
                        "published_at": asset["published_at"],
                        "status": status,
                    }
                )
            observation["publication_window"] = {
                "status": "publication_not_registered"
                if not windows
                else "time_ordered"
                if all(row["status"] == "time_ordered" for row in windows)
                else "not_qualified",
                "assets": windows,
                "causal_claim_allowed": False,
            }
    return result
