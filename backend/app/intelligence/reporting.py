"""Read-only reports over immutable runs; no legacy score or history backfill."""

from collections import Counter

from sqlalchemy import select

from app.intelligence.actions import run_is_current
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


def project_report(run, snapshot, fresh, reviews, *, include_details=True):
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
    report = {
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
    }
    if not include_details:
        return report
    return {
        **report,
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


async def audit_report(db, audit, *, include_measurements=True, include_details=True, collection=False):
    # Sort only IDs: MySQL otherwise carries multi-MB immutable JSON through filesort.
    run_id = await db.scalar(
        select(IntelligenceRun.id)
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
    if run_id is None:
        return result
    run = await db.scalar(
        select(IntelligenceRun).where(
            IntelligenceRun.project_id == audit.project_id,
            IntelligenceRun.id == run_id,
        )
    )
    result.update(run_id=run.id, revision=run.revision, status=run.status)
    if run.status not in ("ready", "partial"):
        return result
    state = await db.scalar(
        select(AuditIntelligenceState).where(
            AuditIntelligenceState.project_id == audit.project_id,
            AuditIntelligenceState.audit_id == audit.id,
        )
    )
    fresh = await run_is_current(db, run, state) if state is not None else False
    if collection:
        # The capture page needs counters and selector options, not scoring matrices,
        # diagnoses, source evidence or the expensive verified metric populations.
        targets = (run.summary.get("brand_presence") or {}).get("targets")
        if targets is None:
            targets = [{**row, "target_id": "entity:" + row["id"]} for row in run.summary.get("entities", [])]
        return {
            **result,
            "status": run.status if fresh else "stale",
            "snapshot_current": fresh,
            "finished_at": utc_isoformat(run.finished_at),
            "coverage": {key: value for key, value in (run.coverage or {}).items() if key in {
                "total_segments", "segment_states", "total_characters", "analyzed_characters",
                "responses", "failed_responses", "sources", "captured_sources", "complete", "semantic_error_count",
            }},
            "analysis_scope": {
                "id": run.id, "audit_id": str(run.audit_id), "revision": run.revision,
                "targets": [{key: row[key] for key in ("target_id", "name", "role", "identity_status")} for row in targets],
                "platforms": list(dict.fromkeys(row["platform"] for row in [
                    *run.manifest.get("responses", []), *run.summary.get("platforms", []),
                ])),
            },
        }
    report = {
        **result,
        **project_report(
            run,
            await run_snapshot(db, run),
            fresh,
            await review_snapshot(db, run.project_id, run.id) if include_details else {},
            include_details=include_details,
        ),
    }
    if include_measurements:
        from app.intelligence.measurement_history import report_measurements

        report["measurements"] = await report_measurements(db, run)
    return report
