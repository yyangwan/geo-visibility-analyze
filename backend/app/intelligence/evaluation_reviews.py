"""Named, optimistic and idempotent eligibility decisions for a frozen audit."""

from collections import Counter, defaultdict
from copy import deepcopy
from typing import Annotated, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.intelligence.models import EvaluationCohortReview
from app.intelligence.refresh import request_refresh, state_for_audit
from app.intelligence.semantics import stable_hash


class CohortReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    base_hash: str = Field(alias="baseHash", pattern=r"^[a-f0-9]{64}$")
    decisions: dict[Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")], Literal["eligible", "excluded", "needs_review"]] = Field(
        min_length=1, max_length=100
    )
    reason: str = Field(min_length=5, max_length=2000)
    expected_version: StrictInt = Field(alias="expectedVersion", ge=0)
    idempotency_key: str = Field(
        alias="idempotencyKey", pattern=r"^[a-zA-Z0-9_-]{8,100}$"
    )


def reviewed_cohort(base, decisions):
    prompts = {cell["prompt_hash"] for cell in base["cells"]}
    if set(decisions) != prompts:
        raise HTTPException(422, "Review every frozen prompt exactly once")
    cohort = deepcopy(base)
    for cell in cohort["cells"]:
        if (
            cell["eligibility"] == "excluded"
            and decisions[cell["prompt_hash"]] != "excluded"
        ):
            raise HTTPException(
                422, "Brand-directed prompt cannot enter competitive scoring"
            )
        cell["eligibility"] = decisions[cell["prompt_hash"]]
        cell["eligibility_reason"] = "named_reviewer_decision"
    eligible = [cell for cell in cohort["cells"] if cell["eligibility"] != "excluded"]
    platforms = {cell["platform"] for cell in eligible}
    categories = defaultdict(set)
    counts = Counter((cell["platform"], cell["category"]) for cell in eligible)
    for cell in eligible:
        categories[cell["platform"]].add(cell["category"])
    for cell in cohort["cells"]:
        included = cell["eligibility"] != "excluded"
        cell["weight"] = {
            "numerator": int(included),
            "denominator": len(platforms)
            * len(categories[cell["platform"]])
            * counts[(cell["platform"], cell["category"])]
            if included
            else 1,
        }
    cohort.pop("hash", None)
    cohort.update(
        base_hash=base["hash"],
        status="requires_prompt_review" if any(cell["eligibility"] == "needs_review" for cell in eligible) else "reviewed" if eligible else "no_competitive_prompts",
    )
    cohort["hash"] = stable_hash(cohort)
    return cohort


async def latest_cohort_review(db, project_id, audit_id, *, lock=False):
    query = (
        select(EvaluationCohortReview)
        .where(
            EvaluationCohortReview.project_id == project_id,
            EvaluationCohortReview.audit_id == audit_id,
        )
        .order_by(EvaluationCohortReview.version.desc())
        .limit(1)
    )
    return await db.scalar(query.with_for_update() if lock else query)


async def review_cohort(db, run, data, actor_id):
    await state_for_audit(db, run.project_id, run.audit_id)
    base = run.manifest.get("evaluation_base_cohort") or run.manifest.get(
        "evaluation_cohort"
    )
    if not base or base["hash"] != data.base_hash:
        raise HTTPException(409, "Frozen cohort changed or unavailable")
    if not actor_id or len(data.reason.strip()) < 5:
        raise HTTPException(422, "Named reviewer and reason required")
    request_hash = stable_hash([run.id, actor_id, data.model_dump()])
    previous = await db.scalar(
        select(EvaluationCohortReview).where(
            EvaluationCohortReview.project_id == run.project_id,
            EvaluationCohortReview.idempotency_key == data.idempotency_key,
        ).with_for_update()
    )
    if previous:
        if previous.request_hash != request_hash:
            raise HTTPException(409, "Review idempotency conflict")
        return previous
    latest = await latest_cohort_review(db, run.project_id, run.audit_id, lock=True)
    if data.expected_version != (latest.version if latest else 0):
        raise HTTPException(409, "Review version changed")
    event = EvaluationCohortReview(
        project_id=run.project_id,
        audit_id=run.audit_id,
        run_id=run.id,
        version=data.expected_version + 1,
        base_hash=base["hash"],
        cohort=reviewed_cohort(base, data.decisions),
        reason=data.reason.strip(),
        actor_id=actor_id,
        idempotency_key=data.idempotency_key,
        request_hash=request_hash,
    )
    try:
        async with db.begin_nested():
            db.add(event)
            await db.flush()
    except IntegrityError as error:
        raise HTTPException(409, "Review collided; refresh version") from error
    await request_refresh(
        db, run.project_id, run.audit_id, "evaluation_cohort_reviewed", event.id
    )
    return event
