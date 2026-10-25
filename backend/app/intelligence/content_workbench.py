"""Project-scoped, read-only inbox of formal content-draft authorizations."""

import hmac

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import aliased

from app.config import settings
from app.intelligence.drafts import export_brief, grant_signature
from app.intelligence.models import ActionPlan, DraftApproval, IntelligenceRun
from app.utils.timezone import utc_isoformat, utcnow


async def content_workbench(db, project_id, cursor=None, approval_id=None):
    result = {
        "schema": "citation-content-workbench-1",
        "project_id": project_id,
        "enabled": settings.intelligence_enabled,
        "cursor": cursor,
        "approval_id": approval_id,
        "items": [],
        "next_cursor": None,
        "publication_allowed": False,
    }
    if not settings.intelligence_enabled:
        return result
    query = select(DraftApproval).where(DraftApproval.project_id == project_id)
    if approval_id:
        query = query.where(DraftApproval.id == approval_id)
    else:
        newer = aliased(DraftApproval)
        query = query.where(
            ~exists(
                select(newer.id).where(
                    newer.project_id == project_id,
                    newer.action_id == DraftApproval.action_id,
                    newer.version > DraftApproval.version,
                )
            )
        )
    if cursor:
        marker = await db.scalar(
            select(DraftApproval).where(
                DraftApproval.project_id == project_id, DraftApproval.id == cursor
            )
        )
        if marker is None:
            raise HTTPException(404, "Cursor not found in project")
        query = query.where(
            or_(
                DraftApproval.created_at < marker.created_at,
                and_(
                    DraftApproval.created_at == marker.created_at,
                    DraftApproval.id < marker.id,
                ),
            )
        )
    rows = (
        await db.scalars(
            query.order_by(
                DraftApproval.created_at.desc(), DraftApproval.id.desc()
            ).limit(31)
        )
    ).all()
    if approval_id and not rows:
        raise HTTPException(404, "Draft approval not found in project")
    for grant in rows[:30]:
        item = {
            "id": grant.id,
            "action_id": grant.action_id,
            "version": grant.version,
            "created_at": utc_isoformat(grant.created_at),
            "expires_at": utc_isoformat(grant.expires_at),
            "title": "材料校验未通过",
            "status": "invalid",
            "run_id": None,
            "audit_id": None,
            "execution": None,
            "ai_targets": [],
            "fact_count": 0,
        }
        if hmac.compare_digest(grant.signature, grant_signature(grant)):
            item["title"] = grant.payload.get("title", "内容草稿材料")
            action = await db.scalar(
                select(ActionPlan).where(
                    ActionPlan.project_id == project_id,
                    ActionPlan.id == grant.action_id,
                )
            )
            run = (
                await db.scalar(
                    select(IntelligenceRun).where(
                        IntelligenceRun.project_id == project_id,
                        IntelligenceRun.id == grant.payload.get("runId"),
                    )
                )
                if action
                else None
            )
            if run:
                item.update(run_id=run.id, audit_id=str(run.audit_id))
                try:
                    brief = await export_brief(db, grant, action, run)
                    item.update(
                        status="ready",
                        execution=brief["execution"],
                        ai_targets=[
                            {"platform": target["platform"], "prompt": target["prompt"]}
                            for target in brief["aiTargets"]
                        ],
                        fact_count=len(brief["ownFacts"]),
                        action_revision=brief["actionRevision"]["revision"],
                    )
                except HTTPException as error:
                    if error.status_code >= 500:
                        raise
                    item["status"] = (
                        "expired" if grant.expires_at <= utcnow() else "stale"
                    )
                except ValidationError:
                    item["status"] = "invalid"
        result["items"].append(item)
    if len(rows) > 30:
        result["next_cursor"] = rows[29].id
    return result
