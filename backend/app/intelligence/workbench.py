"""Small project-scoped read models for the customer workbench."""

from collections import Counter

from sqlalchemy import and_, func, select

from app.intelligence.action_revisions import revision_integrity
from app.intelligence.models import (
    ActionExecution,
    ActionPlan,
    ActionPlanRevision,
    AuditIntelligenceState,
    IntelligenceRun,
)
from app.intelligence.visibility_scoring import score_projection
from app.models.models import Audit
from app.utils.timezone import utc_isoformat


def current_revision_join():
    return and_(
        ActionPlanRevision.project_id == ActionPlan.project_id,
        ActionPlanRevision.action_id == ActionPlan.id,
        ActionPlanRevision.revision == ActionPlan.revision,
    )


async def latest_published_score(db, project_id):
    # Return only the frozen scoring inputs and task references, not report bodies.
    scoring = IntelligenceRun.summary["visibility_score"]
    published = (
        await db.execute(
            select(
                IntelligenceRun.id,
                IntelligenceRun.audit_id,
                IntelligenceRun.finished_at,
                scoring.label("scoring"),
                IntelligenceRun.summary["opportunities"]["items"].label(
                    "opportunities"
                ),
            )
            .join(
                AuditIntelligenceState,
                and_(
                    AuditIntelligenceState.project_id == IntelligenceRun.project_id,
                    AuditIntelligenceState.audit_id == IntelligenceRun.audit_id,
                    AuditIntelligenceState.published_run_id == IntelligenceRun.id,
                ),
            )
            .join(
                Audit,
                and_(
                    Audit.id == IntelligenceRun.audit_id,
                    Audit.project_id == IntelligenceRun.project_id,
                ),
            )
            .where(
                IntelligenceRun.project_id == project_id,
                IntelligenceRun.status.in_(("ready", "partial")),
                scoring["rule_version"].as_string().is_not(None),
            )
            .order_by(Audit.created_at.desc(), Audit.id.desc())
            .limit(1)
        )
    ).one_or_none()
    if published is None:
        return None

    opportunities = published.opportunities or []
    score = score_projection(published.scoring, opportunities=opportunities)
    suggestions = score["recommendations"] = score["recommendations"][:3]

    opportunity_ids = {
        item["action_opportunity"]["id"]
        for item in suggestions
        if item.get("action_opportunity")
    }
    task_keys = {
        item["id"]: item["task_key"]
        for item in opportunities
        if item["id"] in opportunity_ids and item.get("task_key")
    }
    if task_keys:
        plans = (
            await db.execute(
                select(ActionPlan, ActionPlanRevision)
                .join(ActionPlanRevision, current_revision_join())
                .where(
                    ActionPlan.project_id == project_id,
                    ActionPlanRevision.run_id == published.id,
                    ActionPlanRevision.task_key.in_(task_keys.values()),
                )
            )
        ).all()
        action_ids = {
            revision.task_key: plan.id
            for plan, revision in plans
            if revision_integrity(plan, revision)
        }
        for item in suggestions:
            opportunity_id = item.get("action_opportunity", {}).get("id")
            action_id = action_ids.get(task_keys.get(opportunity_id))
            if action_id:
                item["action_id"] = action_id

    # The workbench needs the coverage counts, not per-sample identifiers.
    parts = [score["score"], score.get("scope_score")]
    if score["comparison"]:
        score["comparison"].pop("sample_ids", None)
        parts.extend([score["comparison"]["own"], score["comparison"]["other"]])
    for part in parts:
        if part and part.get("coverage"):
            part["coverage"].pop("scored_sample_ids", None)
    return {
        **score,
        "status": "published",
        "audit_id": str(published.audit_id),
        "run_id": published.id,
        "finished_at": utc_isoformat(published.finished_at),
    }


async def project_execution_counts(db, project_id):
    # Join only the current action revision. Accepted historical revisions do
    # not count toward completion after the plan changes.
    status = ActionExecution.snapshot["status"].as_string()
    rows = await db.execute(
        select(status, func.count(ActionPlan.id))
        .outerjoin(ActionPlanRevision, current_revision_join())
        .outerjoin(
            ActionExecution,
            and_(
                ActionExecution.project_id == ActionPlanRevision.project_id,
                ActionExecution.action_id == ActionPlanRevision.action_id,
                ActionExecution.revision_id == ActionPlanRevision.id,
            ),
        )
        .where(ActionPlan.project_id == project_id)
        .group_by(status)
    )
    counts = Counter(dict(rows.all()))
    return {
        "project_id": project_id,
        "not_started": counts[None] + counts["not_started"],
        "in_progress": counts["preparing_materials"] + counts["in_progress"],
        "awaiting_review": counts["delivered"],
        "completed": counts["accepted"],
        "cancelled": counts["cancelled"],
        "total": sum(counts.values()),
    }
