"""Read-only reports over immutable runs; no legacy score or history backfill."""

from collections import Counter

from sqlalchemy import select

from app.intelligence.actions import stamp
from app.intelligence.measurement import run_snapshot
from app.intelligence.models import AuditIntelligenceState, IntelligenceRun
from app.intelligence.reviews import review_snapshot
from app.utils.timezone import utc_isoformat


def metric_population(responses, observed, *, answer=False):
    included, excluded = [], []
    for response in responses:
        rid = response["id"]
        reason = observed.get("unusable_reasons", {}).get(rid)
        if answer:
            if response.get("error") or response.get("answer_status") != "complete":
                reason = "incomplete_answer"
            elif rid in observed.get("ambiguous_identity", set()):
                reason = "ambiguous_identity"
        if not reason and rid not in observed["completed"]:
            reason = "analysis_incomplete"
        if reason:
            excluded.append({"response_id": rid, "reason": reason})
        else:
            included.append(rid)
    return included, excluded


def project_report(run, snapshot, fresh, reviews):
    entities = run.summary.get("entities", [])
    platforms = []
    for platform in sorted({row["platform"] for row in snapshot["responses"]}):
        responses = [r for r in snapshot["responses"] if r["platform"] == platform]
        metrics = []
        for name, observed in {
            "answer_presence": snapshot,
            **snapshot.get("source_metrics", {}),
        }.items():
            included, excluded = metric_population(
                responses, observed, answer=name == "answer_presence"
            )
            population = set(included)
            metrics.append(
                {
                    "metric": name,
                    "included": included,
                    "excluded": excluded,
                    "entities": [
                        {
                            "entity_id": entity["id"],
                            "numerator": len(
                                population & observed["hits"].get(entity["id"], set())
                            ),
                            "denominator": len(population),
                        }
                        for entity in entities
                    ],
                }
            )
        platforms.append(
            {"platform": platform, "samples": len(responses), "metrics": metrics}
        )
    return {
        "run_id": run.id,
        "revision": run.revision,
        "finished_at": utc_isoformat(run.finished_at),
        "status": run.status if fresh else "stale",
        "snapshot_current": fresh,
        "coverage": run.coverage,
        "source_observations": run.summary.get("source_observations"),
        "opportunities": run.summary.get("opportunities"),
        "entities": entities,
        "platforms": platforms,
        "diagnoses": run.summary.get("diagnoses", []),
        "evidence": [
            {**item, "human_review": reviews.get(item["id"])}
            for item in run.summary.get("claim_evidence", [])
        ],
        "sources": [
            {
                key: row.get(key)
                for key in (
                    "id",
                    "response_id",
                    "platform",
                    "title",
                    "kind",
                    "snippet",
                    "snapshot_id",
                    "fetch_status",
                )
            }
            for row in run.manifest.get("sources", [])
        ],
        "responses": [
            {
                key: row.get(key)
                for key in ("id", "platform", "prompt", "observed_at", "answer_status")
            }
            for row in snapshot["responses"]
        ],
        "source_states": dict(
            Counter(
                row.get("fetch_status", "unknown")
                for row in run.manifest.get("sources", [])
            )
        ),
    }


async def audit_report(db, audit, *, include_measurements=True):
    run = await db.scalar(
        select(IntelligenceRun)
        .where(
            IntelligenceRun.project_id == audit.project_id,
            IntelligenceRun.audit_id == audit.id,
        )
        .order_by(IntelligenceRun.revision.desc())
        .limit(1)
    )
    result = {
        "schema": "citation-report-1",
        "audit_id": str(audit.id),
        "project_id": audit.project_id,
        "status": "needs_analysis",
        "snapshot_current": False,
    }
    if run is None:
        return result
    result.update(run_id=run.id, revision=run.revision, status=run.status)
    if run.status not in ("ready", "partial"):
        return result
    state = await db.scalar(
        select(AuditIntelligenceState).where(
            AuditIntelligenceState.project_id == audit.project_id,
            AuditIntelligenceState.audit_id == audit.id,
        )
    )
    fresh = (await stamp(db, run, state))[1] if state is not None else False
    report = {
        **result,
        **project_report(
            run,
            await run_snapshot(db, run),
            fresh,
            await review_snapshot(db, run.project_id, run.id),
        ),
    }
    if include_measurements:
        from app.intelligence.measurement_history import report_measurements

        report["measurements"] = await report_measurements(db, run)
    return report
