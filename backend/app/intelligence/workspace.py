"""Audit-scoped content observations and evidence-bound suggestions. Read-only."""

from fastapi import HTTPException
from sqlalchemy import select

from app.config import settings
from app.intelligence.dashboard import audit_processing
from app.intelligence.models import IntelligenceRun
from app.intelligence.reporting import audit_report
from app.models.models import Audit
from app.utils.timezone import utc_isoformat


def observation_contexts(run, item):
    responses = {row["id"]: row for row in run.manifest.get("responses", [])}
    if item["owner_kind"] == "answer":
        response = responses.get(item["owner_id"])
        return (
            [
                {
                    "platform": response["platform"],
                    "response_id": response["id"],
                    "source_id": None,
                    "kind": "answer",
                }
            ]
            if response
            else []
        )
    return [
        {
            "platform": source["platform"],
            "response_id": source["response_id"],
            "source_id": source["id"],
            "kind": source["kind"],
        }
        for source in run.manifest.get("sources", [])
        if (item["owner_kind"] == "citation" and item["owner_id"] == source["id"])
        or (
            item["owner_kind"] == "snapshot"
            and item["owner_id"] == source.get("snapshot_id")
        )
    ]


async def analysis_workspace(db, project_id, audit_id=None):
    result = {
        "schema": "citation-workspace-1",
        "project_id": project_id,
        "enabled": settings.intelligence_enabled,
        "audits": [],
        "audits_truncated": False,
        "selected_audit_id": None,
        "latest_audit_id": None,
        "processing": False,
        "report": None,
        "observations": None,
    }
    if not settings.intelligence_enabled:
        return result
    rows = (
        await db.scalars(
            select(Audit)
            .where(Audit.project_id == project_id)
            .order_by(Audit.created_at.desc(), Audit.id.desc())
            .limit(31)
        )
    ).all()
    if audit_id is not None:
        audit = await db.scalar(
            select(Audit).where(Audit.project_id == project_id, Audit.id == audit_id)
        )
        if audit is None:
            raise HTTPException(404, "Audit not found in project")
    else:
        audit = rows[0] if rows else None
    displayed = list(rows[:30])
    if audit and audit.id not in {row.id for row in displayed}:
        displayed.append(audit)
    result.update(
        audits=[
            {
                "id": str(row.id),
                "status": row.status.value,
                "created_at": utc_isoformat(row.created_at),
            }
            for row in displayed
        ],
        audits_truncated=len(rows) > 30,
        latest_audit_id=str(rows[0].id) if rows else None,
    )
    if audit is None:
        return result
    report = await audit_report(db, audit, include_measurements=False)
    result.update(
        selected_audit_id=str(audit.id),
        report=report,
        processing=await audit_processing(db, audit),
    )
    if report.get("run_id") and report["status"] in ("ready", "partial", "stale"):
        run = await db.scalar(
            select(IntelligenceRun).where(
                IntelligenceRun.project_id == project_id,
                IntelligenceRun.id == report["run_id"],
            )
        )
        result["observations"] = {
            key: [
                {**item, "contexts": contexts}
                for item in run.summary.get(key, [])
                if (contexts := observation_contexts(run, item))
            ]
            for key in ("mentions", "claims")
        }
        result["observations"]["prompt_platforms"] = run.summary.get(
            "prompt_platforms", []
        )
    return result
