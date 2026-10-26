"""Named execution and acceptance belong to an exact immutable action revision."""

import hashlib
import hmac
from copy import deepcopy
from datetime import UTC
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import HTTPException
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    model_validator,
)
from sqlalchemy import select

from app.config import settings
from app.intelligence.action_revisions import (
    current_revision,
    lock_action_context,
    revision_integrity,
    revision_signature,
)
from app.intelligence.actions import plan_view
from app.intelligence.models import (
    ActionExecution,
    ActionExecutionEvent,
    ActionPlan,
    ActionPlanRevision,
    DraftApproval,
)
from app.intelligence.semantics import stable_hash
from app.utils.timezone import utc_isoformat, utcnow

WRITERS = ("owner", "admin", "editor", "member")
REVIEWERS = ("owner", "admin", "editor")
INITIAL = {
    "status": "not_started",
    "owner": None,
    "due_at": None,
    "delivery": None,
    "acceptance": None,
}


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Artifact(Contract):
    kind: Literal[
        "external_url", "manual_record", "content_workflow", "manual_content_task"
    ]
    title: str = Field(min_length=5, max_length=200)
    url: str | None = Field(default=None, max_length=4096)
    reference_id: str | None = Field(
        default=None, alias="referenceId", pattern=r"^[a-zA-Z0-9_-]{1,191}$"
    )
    notes: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def coherent(self):
        if self.url:
            parsed = urlsplit(self.url)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError(
                    "Delivery URL must be an HTTP reference without credentials"
                )
        service = self.kind in ("content_workflow", "manual_content_task")
        if (
            service
            and (not self.reference_id or self.url)
            or not service
            and self.reference_id
        ):
            raise ValueError(
                "Service artifacts use exact receipt IDs; external addresses are manually registered"
            )
        if (
            self.kind == "external_url"
            and not self.url
            or self.kind == "manual_record"
            and len(self.notes.strip()) < 5
        ):
            raise ValueError(
                "Provide the actual delivery reference or a substantive verification record"
            )
        return self


class AcceptanceCheck(Contract):
    index: StrictInt = Field(ge=0, le=29)
    met: StrictBool
    note: str = Field(default="", max_length=1000)


class ExecutionRequest(Contract):
    expected_revision: StrictInt = Field(alias="expectedRevision", ge=1)
    expected_version: StrictInt = Field(alias="expectedVersion", ge=0)
    operation: Literal[
        "assign", "prepare", "start", "deliver", "return", "accept", "cancel"
    ]
    owner_id: str | None = Field(
        default=None, alias="ownerId", min_length=1, max_length=200
    )
    due_at: AwareDatetime | None = Field(default=None, alias="dueAt")
    artifacts: tuple[Artifact, ...] = Field(default=(), max_length=10)
    acceptance_checks: tuple[AcceptanceCheck, ...] = Field(
        default=(), alias="acceptanceChecks", max_length=30
    )
    reason: str = Field(min_length=5, max_length=2000)
    idempotency_key: str = Field(
        alias="idempotencyKey", pattern=r"^[a-zA-Z0-9_-]{8,100}$"
    )

    @model_validator(mode="after")
    def operation_fields(self):
        if len(self.reason.strip()) < 5:
            raise ValueError("A substantive named execution rationale is required")
        if self.operation == "assign":
            if not {"owner_id", "due_at"}.issubset(self.model_fields_set):
                raise ValueError(
                    "Assignment explicitly retains or clears owner and deadline"
                )
        elif {"owner_id", "due_at"} & self.model_fields_set:
            raise ValueError("Owner/deadline changes require an assignment event")
        if bool(self.artifacts) != (self.operation == "deliver"):
            raise ValueError(
                "Delivery requires artifacts; other operations cannot replace them"
            )
        if self.acceptance_checks and self.operation not in ("accept", "return"):
            raise ValueError("Acceptance checks require a review event")
        if self.operation == "accept" and not self.acceptance_checks:
            raise ValueError("Acceptance requires reviewing the frozen criteria")
        return self


def signature(event, revision):
    value = stable_hash(
        [
            "action-execution-v1",
            event.id,
            event.project_id,
            event.execution_id,
            revision.action_id,
            revision.id,
            revision.signature,
            event.version,
            event.operation,
            event.actor_id,
            event.actor_role,
            event.reason,
            event.before,
            event.after,
            event.previous_signature,
            event.request_hash,
            event.idempotency_key,
            utc_isoformat(event.created_at),
        ]
    )
    return hmac.new(
        settings.secret_key.encode(), value.encode(), hashlib.sha256
    ).hexdigest()


async def execution_revision(db, action, number, *, lock=False):
    query = select(ActionPlanRevision).where(
        ActionPlanRevision.project_id == action.project_id,
        ActionPlanRevision.action_id == action.id,
        ActionPlanRevision.revision == number,
    )
    row = await db.scalar(
        query.with_for_update().execution_options(populate_existing=True)
        if lock
        else query
    )
    if not row:
        raise HTTPException(404, "Action revision not found")
    if not hmac.compare_digest(row.signature, revision_signature(row)):
        raise HTTPException(409, "Action revision integrity changed")
    return row


async def head_for(db, action, revision, *, lock=False):
    query = select(ActionExecution).where(
        ActionExecution.project_id == action.project_id,
        ActionExecution.revision_id == revision.id,
    )
    head = await db.scalar(
        query.with_for_update().execution_options(populate_existing=True)
        if lock
        else query
    )
    if head:
        query = select(ActionExecutionEvent).where(
            ActionExecutionEvent.project_id == head.project_id,
            ActionExecutionEvent.execution_id == head.id,
            ActionExecutionEvent.version == head.version,
        )
        last = await db.scalar(
            query.with_for_update().execution_options(populate_existing=True)
            if lock
            else query
        )
        if (
            not last
            or last.after != head.snapshot
            or head.signature != last.signature
            or not hmac.compare_digest(last.signature, signature(last, revision))
        ):
            raise HTTPException(
                409, "Execution projection integrity changed; stop using this version"
            )
    return head


def projection(action, revision, head):
    return {
        "version": head.version if head else 0,
        "action_id": action.id,
        "revision": revision.revision,
        "revision_id": revision.id,
        "is_current": action.revision == revision.revision,
        "snapshot": deepcopy(head.snapshot if head else INITIAL),
        "publication_allowed": False,
        "acceptance_is_visibility_improvement": False,
    }


async def read_execution(db, action, number):
    revision = await execution_revision(db, action, number)
    head = await head_for(db, action, revision)
    return projection(action, revision, head)


async def execution_events(db, action, number, before=None, limit=20):
    revision = await execution_revision(db, action, number)
    head = await head_for(db, action, revision)
    query = (
        select(ActionExecutionEvent).where(
            ActionExecutionEvent.project_id == action.project_id,
            ActionExecutionEvent.execution_id == head.id,
        )
        if head
        else None
    )
    rows = []
    if query is not None:
        if before is not None:
            query = query.where(ActionExecutionEvent.version <= before)
        rows = (
            await db.scalars(
                query.order_by(ActionExecutionEvent.version.desc()).limit(limit + 1)
            )
        ).all()
    return {
        "action_id": action.id,
        "revision_id": revision.id,
        "revision": number,
        "events": [
            {
                "id": row.id,
                "version": row.version,
                "operation": row.operation,
                "actor_id": row.actor_id,
                "actor_role": row.actor_role,
                "reason": row.reason,
                "created_at": utc_isoformat(row.created_at),
                "before": row.before,
                "after": row.after,
                "integrity_valid": hmac.compare_digest(
                    row.signature, signature(row, revision)
                ),
            }
            for row in rows[:limit]
        ],
        "next_before": rows[limit - 1].version - 1 if len(rows) > limit else None,
    }


def authority(user, action, data, wire_hash):
    proof = user.get("execution_authority") or {}
    if (
        not isinstance(proof, dict)
        or user.get("scope") != "project"
        or user.get("role") not in WRITERS
        or not user.get("sub")
        or user.get("pid") != action.project_id
        or proof.get("action_id") != action.id
        or proof.get("revision") != data.expected_revision
        or proof.get("request_hash") != wire_hash
    ):
        raise HTTPException(403, "Verified Portal execution authority required")
    return proof


async def delivery_artifacts(db, action, revision, data, proof):
    from app.intelligence.brief_contracts import EvidenceBriefV3
    from app.intelligence.drafts import grant_payload, grant_signature

    receipts = proof.get("artifacts") or []
    expected = [
        (row.kind, row.reference_id) for row in data.artifacts if row.reference_id
    ]
    if (
        len(set(expected)) != len(expected)
        or [(row.get("kind"), row.get("reference_id")) for row in receipts] != expected
    ):
        raise HTTPException(
            409, "Delivery service receipts do not match the requested artifacts"
        )
    values = []
    for item in data.artifacts:
        value = item.model_dump(mode="json")
        if not item.reference_id:
            values.append({**value, "verification": "manual_registration"})
            continue
        receipt = next(
            row
            for row in receipts
            if row.get("kind") == item.kind
            and row.get("reference_id") == item.reference_id
        )
        grant = await db.scalar(
            select(DraftApproval).where(
                DraftApproval.project_id == action.project_id,
                DraftApproval.action_id == action.id,
                DraftApproval.id == receipt.get("approval_id"),
            )
        )
        if (
            not grant
            or receipt.get("project_id") != action.project_id
            or receipt.get("action_id") != action.id
            or receipt.get("revision_id") != revision.id
            or grant.payload.get("actionRevision", {}).get("id") != revision.id
            or receipt.get("status")
            != ("succeeded" if item.kind == "content_workflow" else "completed")
            or not hmac.compare_digest(grant.signature, grant_signature(grant))
            or receipt.get("snapshot_hash")
            != stable_hash(
                EvidenceBriefV3.model_validate(grant_payload(grant)).model_dump(
                    mode="json", by_alias=True
                )
            )
        ):
            raise HTTPException(
                409, "Artifact is incomplete or belongs to another action revision"
            )
        values.append({**value, "verification": "service_receipt", "receipt": receipt})
    return values


async def change_execution(db, action, data, user, wire_hash):
    proof = authority(user, action, data, wire_hash)
    _, run = await current_revision(db, action)
    await lock_action_context(db, run)
    action = await db.scalar(
        select(ActionPlan)
        .where(ActionPlan.project_id == action.project_id, ActionPlan.id == action.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    request_hash = stable_hash(
        [action.id, str(user["sub"]), data.model_dump(mode="json")]
    )
    old = await db.scalar(
        select(ActionExecutionEvent)
        .where(
            ActionExecutionEvent.project_id == action.project_id,
            ActionExecutionEvent.idempotency_key == data.idempotency_key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if old:
        if old.request_hash != request_hash:
            raise HTTPException(409, "Execution operation key conflict")
        saved_head = await db.get(ActionExecution, old.execution_id)
        saved_revision = (
            await db.get(ActionPlanRevision, saved_head.revision_id)
            if saved_head
            else None
        )
        if (
            not saved_revision
            or saved_head.action_id != action.id
            or not hmac.compare_digest(old.signature, signature(old, saved_revision))
        ):
            raise HTTPException(409, "Saved execution receipt integrity changed")
        return {
            "replayed": True,
            "event_id": old.id,
            "saved_version": old.version,
            "snapshot": old.after,
        }
    revision, run = await current_revision(db, action, lock=True)
    if action.revision != data.expected_revision or not revision_integrity(
        action, revision
    ):
        raise HTTPException(409, "Action revision changed; refresh execution")
    pack = revision.payload["target"].get("task_pack")
    if not pack:
        raise HTTPException(409, "Execution requires a complete actionable TaskPack")
    head = await head_for(db, action, revision, lock=True)
    if (head.version if head else 0) != data.expected_version:
        raise HTTPException(409, "Execution version changed; refresh before saving")
    before = deepcopy(head.snapshot if head else INITIAL)
    after = deepcopy(before)
    status, role, actor = before["status"], user["role"], str(user["sub"])
    owner = before["owner"]
    manager = role in REVIEWERS
    assigned = bool(owner and owner["id"] == actor)
    if status in ("accepted", "cancelled"):
        raise HTTPException(
            409,
            "This execution is closed; create a new task revision for further changes",
        )
    if (
        data.operation in ("accept", "return")
        and not manager
        or data.operation not in ("assign", "prepare", "accept", "return")
        and not (manager or assigned)
    ):
        raise HTTPException(403, "Current assignee or authorized reviewer required")
    if data.operation in ("start", "deliver", "accept"):
        if not (await plan_view(db, action, run))["approval_valid"]:
            raise HTTPException(
                409, "Current plan or its related evidence needs reapproval"
            )
        if not owner or proof.get("current_owner_id") != owner["id"]:
            raise HTTPException(
                409, "Current assignee is unavailable; arrange an active project member"
            )
    if data.operation == "assign":
        if not manager and (data.owner_id != actor or owner and not assigned):
            raise HTTPException(403, "Only a reviewer may assign other members")
        selected = proof.get("owner")
        if data.owner_id is not None and (
            not isinstance(selected, dict)
            or selected.get("id") != data.owner_id
            or selected.get("role") not in WRITERS
            or not isinstance(selected.get("name"), str)
            or not 1 <= len(selected["name"]) <= 200
        ):
            raise HTTPException(
                403, "Assignee must be a verified active workspace member"
            )
        after["owner"] = (
            {"id": selected["id"], "name": selected["name"], "role": selected["role"]}
            if data.owner_id
            else None
        )
        after["due_at"] = (
            utc_isoformat(data.due_at.astimezone(UTC).replace(tzinfo=None))
            if data.due_at
            else None
        )
    elif data.operation == "prepare":
        if not manager and not assigned:
            raise HTTPException(403, "Arrange a responsible member before preparation")
        if status != "not_started":
            raise HTTPException(409, "Preparation can only start once")
        after["status"] = "preparing_materials"
    elif data.operation == "start":
        if status not in ("not_started", "preparing_materials"):
            raise HTTPException(409, "Task is not ready to start")
        after["status"] = "in_progress"
    elif data.operation == "deliver":
        if status != "in_progress":
            raise HTTPException(409, "Only an active task can submit delivery")
        after["status"] = "delivered"
        after["delivery"] = {
            "event_id": uuid4().hex,
            "actor_id": actor,
            "submitted_at": utc_isoformat(utcnow()),
            "artifacts": await delivery_artifacts(db, action, revision, data, proof),
            "notes": data.reason.strip(),
        }
        after["acceptance"] = None
    elif data.operation in ("accept", "return"):
        if status != "delivered" or not before["delivery"]:
            raise HTTPException(
                409, "Review requires this revision's submitted delivery"
            )
        checks = [row.model_dump() for row in data.acceptance_checks]
        if len({row["index"] for row in checks}) != len(checks) or any(
            row["index"] >= len(pack["acceptance"]) for row in checks
        ):
            raise HTTPException(
                422, "Review checks must match the original task criteria"
            )
        if data.operation == "accept" and (
            sorted(row["index"] for row in checks)
            != list(range(len(pack["acceptance"])))
            or not all(row["met"] for row in checks)
        ):
            raise HTTPException(
                422, "Acceptance requires every frozen criterion to be met"
            )
        after["status"] = "accepted" if data.operation == "accept" else "in_progress"
        after["acceptance"] = {
            "delivery_event_id": before["delivery"]["event_id"],
            "actor_id": actor,
            "decision": data.operation,
            "checks": [
                {**row, "criterion": pack["acceptance"][row["index"]]} for row in checks
            ],
            "reason": data.reason.strip(),
        }
    else:
        after["status"] = "cancelled"
    if after == before:
        raise HTTPException(422, "Execution did not change")
    if head is None:
        head = ActionExecution(
            id=uuid4().hex,
            project_id=action.project_id,
            action_id=action.id,
            revision_id=revision.id,
            version=1,
            snapshot=after,
            signature="",
        )
        db.add(head)
        await db.flush()
    number = data.expected_version + 1
    event_id = (
        after["delivery"]["event_id"] if data.operation == "deliver" else uuid4().hex
    )
    event = ActionExecutionEvent(
        id=event_id,
        project_id=action.project_id,
        execution_id=head.id,
        version=number,
        operation=data.operation,
        actor_id=actor,
        actor_role=role,
        reason=data.reason.strip(),
        before=before,
        after=after,
        previous_signature=head.signature or "",
        signature="",
        idempotency_key=data.idempotency_key,
        request_hash=request_hash,
        created_at=utcnow(),
    )
    event.signature = signature(event, revision)
    head.version, head.snapshot, head.signature = number, after, event.signature
    db.add(event)
    await db.flush()
    return {
        "replayed": False,
        "event_id": event.id,
        "saved_version": number,
        **projection(action, revision, head),
    }
