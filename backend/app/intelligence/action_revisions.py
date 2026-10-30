"""Append-only plan revisions, exact task payloads and optimistic named review."""

from copy import deepcopy
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.intelligence.action_dependencies import freeze_dependencies
from app.intelligence.capture_contract import digest
from app.intelligence.models import (
    ActionApproval,
    ActionPlan,
    ActionPlanRevision,
    IntelligenceRun,
    PlatformDiagnosis,
    ProjectContextHead,
)
from app.intelligence.refresh import state_for_audit
from app.intelligence.semantics import stable_hash
from app.intelligence.value_contracts import TaskPackV2
from app.utils.timezone import utc_isoformat


class RevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    expected_revision: StrictInt = Field(alias="expectedRevision", ge=1)
    diagnosis_id: str | None = Field(
        default=None, alias="diagnosisId", pattern=r"^[a-f0-9]{32}$"
    )
    title: str | None = Field(default=None, min_length=5, max_length=2000)
    task_pack: TaskPackV2 | None = Field(default=None, alias="taskPack")
    reason: str = Field(min_length=5, max_length=2000)
    idempotency_key: str = Field(
        alias="idempotencyKey", pattern=r"^[a-zA-Z0-9_-]{8,100}$"
    )


async def lock_action_context(db, run, other_audit_ids=()):
    # Project materials/context writers lock the project head before the audit.
    # Do not create a head for unregistered/operational tasks.
    await db.scalar(
        select(ProjectContextHead)
        .where(ProjectContextHead.project_id == run.project_id)
        .with_for_update()
    )
    for audit_id in sorted({run.audit_id, *other_audit_ids}):
        await state_for_audit(db, run.project_id, audit_id)


def revision_signature(row):
    return stable_hash(
        [
            "action-revision-v1",
            row.id,
            row.project_id,
            row.action_id,
            row.run_id,
            row.revision,
            row.task_key,
            row.payload,
            row.dependencies,
            row.actor_id,
            row.reason,
        ]
    )


def revision_integrity(action, row):
    return bool(
        row
        and row.signature == revision_signature(row)
        and action.project_id == row.project_id
        and action.id == row.action_id
        and action.revision == row.revision
        and action.title == row.payload["title"]
        and action.target == row.payload["target"]
    )


async def current_revision(db, action, *, lock=False):
    query = select(ActionPlanRevision).where(
        ActionPlanRevision.project_id == action.project_id,
        ActionPlanRevision.action_id == action.id,
        ActionPlanRevision.revision == action.revision,
    )
    row = await db.scalar(
        query.with_for_update().execution_options(populate_existing=True)
        if lock
        else query
    )
    if row is None:
        raise HTTPException(409, "Current immutable action revision is missing")
    run = await db.scalar(
        select(IntelligenceRun).where(
            IntelligenceRun.project_id == action.project_id,
            IntelligenceRun.id == row.run_id,
        )
    )
    if run is None:
        raise HTTPException(409, "Action revision evidence run is missing")
    return row, run


def task_key(action, target):
    return target.get("task_key") or stable_hash(
        [action.project_id, "operational", action.diagnosis_id]
    )


async def save_revision(
    db, action, run, target, title, actor, reason, key, request_hash
):
    dependencies = await freeze_dependencies(db, run, target, lock=True)
    row = ActionPlanRevision(
        id=uuid4().hex,
        project_id=action.project_id,
        action_id=action.id,
        run_id=run.id,
        revision=action.revision,
        task_key=task_key(action, target),
        payload={"title": title, "target": deepcopy(target)},
        dependencies=dependencies,
        actor_id=actor,
        reason=reason,
        idempotency_key=key,
        request_hash=request_hash,
        signature="",
    )
    row.signature = revision_signature(row)
    db.add(row)
    await db.flush()
    return row


async def append_initial_revision(db, action, run, actor):
    return await save_revision(
        db,
        action,
        run,
        action.target,
        action.title,
        actor,
        "Created from a published diagnosis",
        action.idempotency_key,
        stable_hash([run.id, action.diagnosis_id, actor]),
    )


def validate_task_edit(seed, edited):
    # Interpretation/coverage comes from a published diagnosis. Edits may refine
    # delivery and select own facts; they cannot invent platform findings or drop evidence.
    original = TaskPackV2.model_validate(seed["task_pack"])
    for field in (
        "project_id",
        "own_target_id",
        "asset_key",
        "fact_change_key",
        "intent",
        "ai_targets",
        "diagnostic_evidence",
        "missing_materials",
    ):
        if getattr(original, field) != getattr(edited, field):
            raise HTTPException(
                422,
                "Changed diagnostic scope requires a published diagnosis with the same task key",
            )
    if edited.delivery.questions != original.delivery.questions:
        raise HTTPException(
            422, "Keep every original question; do not synthesize prompts"
        )
    if (
        edited.delivery.asset_kind != original.delivery.asset_kind
        or edited.delivery.material_revision_id
        != original.delivery.material_revision_id
        or edited.delivery.material_version != original.delivery.material_version
    ):
        raise HTTPException(
            422, "Changed asset provenance requires a new diagnosed task revision"
        )
    if any(
        getattr(edited.delivery, field) != getattr(original.delivery, field)
        for field in ("reference_url", "audience", "audience_status")
    ):
        raise HTTPException(
            422, "Keep frozen material URL and reviewed audience provenance"
        )


async def validate_own_facts(db, run, target, *, require_frozen=False):
    from app.intelligence.fact_qualification import task_fact

    pack = (
        TaskPackV2.model_validate(target["task_pack"])
        if target.get("task_pack")
        else None
    )
    if not pack:
        return
    if (
        pack.project_id != run.project_id
        or pack.task_key() != target["task_key"]
        or pack.revision_signature() != target["task_signature"]
    ):
        raise HTTPException(409, "Frozen task package integrity changed")
    own = next(
        (
            row
            for row in run.manifest.get("brand_targets", [])
            if row["target_id"] == pack.own_target_id and row["role"] == "own"
        ),
        None,
    )
    if not own:
        raise HTTPException(409, "Task requires its frozen own brand target")
    responses = run.manifest.get("responses", [])
    for question in pack.delivery.questions:
        if not any(
            row["prompt_hash"] == question.prompt_hash
            and row["prompt"] == question.text
            and question.prompt_hash
            == (
                digest(question.text.encode())
                if row.get("prompt_version_id")
                else stable_hash(question.text)
            )
            for row in responses
        ):
            raise HTTPException(
                409, "Task question is not the frozen original question"
            )
    cells = (run.manifest.get("evaluation_cohort") or {}).get("cells", [])
    for ai in pack.ai_targets:
        cell = next(
            (
                row
                for row in cells
                if row["platform"] == ai.platform
                and row["prompt_hash"] == ai.prompt_hash
            ),
            {},
        )
        if not any(
            row["platform"] == ai.platform and row["prompt_hash"] == ai.prompt_hash
            for row in responses
        ) or ai.scenario_id != (cell.get("primary_scenario") or "unreviewed"):
            raise HTTPException(
                409, "Task platform or reviewed scenario is not in the frozen Run"
            )
    for fact_id in pack.own_fact_ids:
        fact = await task_fact(db, run, pack, fact_id, lock=True)
        frozen = next(
            (
                row
                for row in run.manifest.get("approved_facts", {}).values()
                if row["id"] == fact_id
            ),
            None,
        )
        if require_frozen and (
            not frozen
            or any(
                frozen.get(key) != fact.get(key)
                for key in (
                    "version",
                    "statement",
                    "scope",
                    "source_ref",
                    "valid_from",
                    "valid_until",
                    "source_kind",
                    "target_id",
                    "entity_id",
                )
            )
        ):
            raise HTTPException(
                409, "Diagnosed fact version differs from its frozen input"
            )
        if not fact["eligible"]:
            raise HTTPException(
                409,
                "Selected fact must be valid and belong to this own brand; approval is not semantic entailment",
            )


async def revise_plan(db, action, data, actor):
    if (
        not actor
        or len(data.reason.strip()) < 5
        or data.title is not None
        and len(data.title.strip()) < 5
    ):
        raise HTTPException(
            422, "Named revision requires a meaningful reason and title"
        )
    # Resolve run before locking, then re-read the action pointer under its row lock.
    _, origin = await current_revision(db, action)
    diagnosis = None
    candidate_run = origin
    if data.diagnosis_id:
        diagnosis = await db.scalar(
            select(PlatformDiagnosis).where(
                PlatformDiagnosis.project_id == action.project_id,
                PlatformDiagnosis.id == data.diagnosis_id,
            )
        )
        if diagnosis is None:
            raise HTTPException(404, "Diagnosis not found in project")
        candidate_run = await db.scalar(
            select(IntelligenceRun).where(
                IntelligenceRun.project_id == action.project_id,
                IntelligenceRun.id == diagnosis.run_id,
            )
        )
    await lock_action_context(db, origin, [candidate_run.audit_id])
    action = await db.scalar(
        select(ActionPlan)
        .where(ActionPlan.project_id == action.project_id, ActionPlan.id == action.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    request_hash = stable_hash([action.id, data.model_dump(mode="json"), actor])
    old = await db.scalar(
        select(ActionPlanRevision)
        .where(
            ActionPlanRevision.project_id == action.project_id,
            ActionPlanRevision.idempotency_key == data.idempotency_key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if old:
        if old.request_hash != request_hash:
            raise HTTPException(409, "Action revision request key conflict")
        return old
    if action.revision != data.expected_revision:
        raise HTTPException(409, "Action revision changed; reload before revising")
    current, run = await current_revision(db, action, lock=True)
    if not revision_integrity(action, current):
        raise HTTPException(409, "Action revision integrity changed")
    target, title = (
        deepcopy(current.payload["target"]),
        data.title.strip() if data.title else current.payload["title"],
    )
    if data.diagnosis_id:
        if (
            not current.payload["target"].get("task_key")
            and diagnosis.id != action.diagnosis_id
        ):
            raise HTTPException(
                409, "Operational task cannot be rebound to a different diagnosis"
            )
        if (
            not diagnosis.payload.get("action")
            or task_key(action, diagnosis.payload) != current.task_key
        ):
            raise HTTPException(
                409, "Rebase requires an actionable diagnosis for the same logical task"
            )
        run = candidate_run
        target = deepcopy(diagnosis.payload)
        title = data.title.strip() if data.title else target["action"]["task"]
    if data.task_pack:
        if not target.get("task_pack"):
            raise HTTPException(422, "Operational task has no TaskPack to edit")
        validate_task_edit(target, data.task_pack)
        target["task_pack"] = data.task_pack.model_dump(mode="json")
        target["task_signature"] = data.task_pack.revision_signature()
        target["action"] = {**target["action"], "task": data.task_pack.deliverable}
        title = data.title.strip() if data.title else data.task_pack.deliverable
    await validate_own_facts(db, run, target)
    # All changes, including refreshed selected facts, become a new immutable row.
    dependencies = await freeze_dependencies(db, run, target, lock=True)
    if (
        current.payload == {"title": title, "target": target}
        and current.run_id == run.id
        and stable_hash(current.dependencies) == stable_hash(dependencies)
    ):
        raise HTTPException(409, "No task or relevant dependency change to revise")
    try:
        async with db.begin_nested():
            action.revision += 1
            action.title, action.target, action.status = title, target, "proposed"
            return await save_revision(
                db,
                action,
                run,
                target,
                title,
                actor,
                data.reason.strip(),
                data.idempotency_key,
                request_hash,
            )
    except IntegrityError as error:
        raise HTTPException(
            409, "Action revision collided; reload current revision"
        ) from error


def payload_diff(before, after, path=""):
    if isinstance(before, dict) and isinstance(after, dict):
        return [
            item
            for key in sorted(set(before) | set(after))
            for item in payload_diff(before.get(key), after.get(key), path + "/" + key)
        ]
    return (
        [] if before == after else [{"field": path, "before": before, "after": after}]
    )


async def revision_changes(db, row):
    from app.intelligence.action_dependencies import dependency_changes

    previous = await db.scalar(
        select(ActionPlanRevision).where(
            ActionPlanRevision.project_id == row.project_id,
            ActionPlanRevision.action_id == row.action_id,
            ActionPlanRevision.revision == row.revision - 1,
        )
    )
    return (
        {
            "task": payload_diff(previous.payload, row.payload),
            "dependencies": dependency_changes(
                previous.dependencies["expected"], row.dependencies["expected"]
            ),
        }
        if previous
        else {"task": [], "dependencies": []}
    )


async def revision_history(db, action, offset=0, limit=20):
    import hmac

    from app.intelligence.actions import signature
    from app.intelligence.models import DraftApproval

    grants = (
        await db.scalars(
            select(DraftApproval).where(
                DraftApproval.project_id == action.project_id,
                DraftApproval.action_id == action.id,
            )
        )
    ).all()

    rows = (
        await db.scalars(
            select(ActionPlanRevision)
            .where(
                ActionPlanRevision.project_id == action.project_id,
                ActionPlanRevision.action_id == action.id,
            )
            .order_by(ActionPlanRevision.revision.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    result = []
    for row in rows:
        approval = await db.scalar(
            select(ActionApproval).where(
                ActionApproval.project_id == action.project_id,
                ActionApproval.action_id == action.id,
                ActionApproval.revision == row.revision,
            )
        )
        intact = row.signature == revision_signature(row)
        result.append(
            {
                "id": row.id,
                "revision": row.revision,
                "run_id": row.run_id,
                "task_key": row.task_key,
                "signature": row.signature,
                "dependency_signature": stable_hash(row.dependencies),
                "payload": deepcopy(row.payload),
                "dependencies": deepcopy(row.dependencies),
                "actor_id": row.actor_id,
                "reason": row.reason,
                "created_at": utc_isoformat(row.created_at),
                "is_current": row.revision == action.revision,
                "integrity_valid": intact,
                "changes_from_previous": await revision_changes(db, row),
                "draft_approval_ids": [
                    grant.id
                    for grant in grants
                    if (grant.payload.get("actionRevision") or {}).get("id") == row.id
                ],
                "approval": {
                    "id": approval.id,
                    "actor_id": approval.actor_id,
                    "role": approval.role,
                    "reason": approval.reason,
                    "signature": approval.signature,
                    "historical_signature_valid": intact
                    and approval.input_stamp == stable_hash(row.dependencies)
                    and hmac.compare_digest(
                        approval.signature, signature(row, approval)
                    ),
                    "execution_authorized": False,
                }
                if approval
                else None,
            }
        )
    return result
