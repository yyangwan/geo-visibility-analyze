"""Read saved paired measurements without recomputing or creating audits."""

from copy import deepcopy

from fastapi import HTTPException
from sqlalchemy import or_, select

from app.config import settings
from app.intelligence.actions import stamp
from app.intelligence.measurement import checked_spec
from app.intelligence.models import (
    AuditIntelligenceState,
    IntelligenceRun,
    MeasurementCohort,
    MeasurementResult,
    SegmentAnalysis,
)
from app.intelligence.semantics import stable_hash
from app.utils.timezone import utc_isoformat


async def measurement_history(db, project_id, cohort_id=None, since=None):
    result = {
        "schema": "citation-measurement-history-1",
        "project_id": project_id,
        "enabled": settings.intelligence_enabled,
        "cohorts": [],
        "cohorts_truncated": False,
        "selected_cohort_id": None,
        "results": [],
        "results_truncated": False,
    }
    if not settings.intelligence_enabled:
        return result
    query = select(MeasurementCohort).where(MeasurementCohort.project_id == project_id)
    if since is not None:
        query = query.where(MeasurementCohort.created_at >= since)
    cohorts = (
        await db.scalars(
            query.order_by(
                MeasurementCohort.created_at.desc(), MeasurementCohort.id.desc()
            ).limit(51)
        )
    ).all()
    cohort = cohorts[0] if cohorts else None
    if cohort_id is not None:
        cohort = await db.scalar(query.where(MeasurementCohort.id == cohort_id))
        if cohort is None:
            raise HTTPException(404, "Cohort not found in permitted project history")
    displayed = list(cohorts[:50])
    if cohort and cohort.id not in {row.id for row in displayed}:
        displayed.append(cohort)
    for row in displayed:
        spec = checked_spec(row)
        result["cohorts"].append(
            {
                "id": row.id,
                "title": row.title,
                "baseline_run_id": row.baseline_run_id,
                "created_at": utc_isoformat(row.created_at),
                "expected_samples": len(spec["slots"]),
                "platforms": sorted({slot["platform"] for slot in spec["slots"]}),
                "comparison_mode": spec.get("comparison_mode", "inferential_v1"),
                "metric_version": spec["version"],
                "brand_targets": spec.get("brand_targets", []),
            }
        )
    result["cohorts_truncated"] = len(cohorts) > 50
    if cohort is None:
        return result
    result["selected_cohort_id"] = cohort.id
    checked_spec(cohort)
    query = select(MeasurementResult).where(
        MeasurementResult.project_id == project_id,
        MeasurementResult.cohort_id == cohort.id,
    )
    if since is not None:
        query = query.where(MeasurementResult.created_at >= since)
    rows = (
        await db.scalars(
            query.order_by(
                MeasurementResult.created_at.desc(), MeasurementResult.id.desc()
            ).limit(11)
        )
    ).all()
    result["results_truncated"] = len(rows) > 10
    result["results"] = await saved_measurements(
        db, project_id, [(row, cohort) for row in rows[:10]]
    )
    return result


async def saved_measurements(db, project_id, pairs):
    cache = {}

    async def run_state(run_id):
        if run_id in cache:
            return cache[run_id]
        run = await db.scalar(
            select(IntelligenceRun).where(
                IntelligenceRun.project_id == project_id, IntelligenceRun.id == run_id
            )
        )
        fresh = False
        if run and run.status in ("ready", "partial"):
            newest = await db.scalar(
                select(IntelligenceRun.id)
                .where(
                    IntelligenceRun.project_id == project_id,
                    IntelligenceRun.audit_id == run.audit_id,
                )
                .order_by(IntelligenceRun.revision.desc())
                .limit(1)
            )
            state = await db.scalar(
                select(AuditIntelligenceState).where(
                    AuditIntelligenceState.project_id == project_id,
                    AuditIntelligenceState.audit_id == run.audit_id,
                )
            )
            fresh = (
                newest == run.id
                and state is not None
                and (await stamp(db, run, state))[1]
            )
        cache[run_id] = (run, fresh)
        return run, fresh

    items = []
    for row, cohort in pairs:
        spec = checked_spec(cohort)
        baseline, base_fresh = await run_state(cohort.baseline_run_id)
        followup, follow_fresh = await run_state(row.followup_run_id)
        payload = deepcopy(row.result)
        valid = bool(
            baseline
            and followup
            and baseline.audit_id != followup.audit_id
            and baseline.input_hash == spec["baseline_input_hash"]
            and payload.get("version") == spec["version"]
            and payload.get("comparison_mode", "inferential_v1")
            == spec.get("comparison_mode", "inferential_v1")
            and payload.get("cohort_id") == cohort.id
            and payload.get("baseline_run_id") == baseline.id
            and payload.get("followup_run_id") == followup.id
            and payload.get("cohort_specification_hash") == cohort.specification_hash
            and payload.get("baseline_input_hash") == baseline.input_hash
            and payload.get("followup_input_hash") == followup.input_hash
            and row.comparison_signature
            == stable_hash(
                [
                    spec["version"],
                    cohort.specification_hash,
                    [baseline.id, baseline.input_hash, baseline.signature],
                    [followup.id, followup.input_hash, followup.signature],
                ]
            )
        )
        if not valid:
            raise HTTPException(409, "Stored measurement does not match frozen inputs")
        current = base_fresh and follow_fresh
        freshness_basis = "current_snapshots"
        if (
            not current
            and spec.get("comparison_mode") == "descriptive_v1"
            and follow_fresh
        ):
            # A newly confirmed first appearance changes project identity context.
            # That alone cannot erase a previously verified absence under unchanged names.
            current = await frozen_baseline_compatible(db, baseline, spec)
            freshness_basis = "frozen_baseline_current_followup"
        if not current:
            for platform in payload.get("platforms", []):
                for metric in platform["metrics"]:
                    metric.update(difference=None, interval95=None)
                for observation in platform.get("observations", []):
                    observation.update(
                        comparison_state="incompatible",
                        reason_codes=["snapshot_not_current"],
                    )
                    if "publication_window" in observation:
                        observation["publication_window"]["status"] = (
                            "snapshot_not_current"
                        )
                    for target in observation["targets"]:
                        for metric in target["metrics"]:
                            metric.update(
                                observed_delta=None,
                                comparison_state="incompatible",
                                reason_codes=["snapshot_not_current"],
                            )
        items.append(
            {
                **payload,
                "id": row.id,
                "created_at": utc_isoformat(row.created_at),
                "comparison_signature": row.comparison_signature,
                "snapshot_current": current,
                "baseline_snapshot_current": base_fresh,
                "followup_snapshot_current": follow_fresh,
                "freshness_basis": freshness_basis,
                "cohort_title": cohort.title,
                "baseline_audit_id": str(baseline.audit_id),
                "followup_audit_id": str(followup.audit_id),
            }
        )
    return items


async def frozen_baseline_compatible(db, baseline, spec):
    from app.intelligence.action_dependencies import (
        current_questions,
        question_projection,
        scoped_identity,
    )
    from app.intelligence.descriptive_measurement import target_projection

    newest = await db.scalar(
        select(IntelligenceRun.id)
        .where(
            IntelligenceRun.project_id == baseline.project_id,
            IntelligenceRun.audit_id == baseline.audit_id,
        )
        .order_by(IntelligenceRun.revision.desc())
        .limit(1)
    )
    if newest != baseline.id or baseline.status not in ("ready", "partial"):
        return False
    if (
        stable_hash(
            target_projection(
                baseline.summary.get("brand_presence", {}), spec["brand_target_ids"]
            )
        )
        != spec["baseline_brand_projection_hash"]
    ):
        return False
    keys = [(row["platform"], row["prompt_hash"]) for row in spec["slots"]]
    if await current_questions(db, baseline, keys) != question_projection(
        baseline.manifest.get("evaluation_cohort"), keys
    ):
        return False
    segments = list(
        await db.scalars(
            select(SegmentAnalysis.segment_id).where(
                SegmentAnalysis.project_id == baseline.project_id,
                SegmentAnalysis.run_id == baseline.id,
            )
        )
    )
    _scope, frozen, live = await scoped_identity(
        db,
        baseline,
        [row["entity_id"] for row in spec["brand_targets"] if row.get("entity_id")],
        segments,
        spec["brand_target_ids"],
    )
    return frozen["bindings"] == live["bindings"]


async def report_measurements(db, run):
    # Exact Run association: never append the latest project cohort by accident.
    rows = (
        await db.execute(
            select(MeasurementResult, MeasurementCohort)
            .join(
                MeasurementCohort,
                (MeasurementCohort.project_id == MeasurementResult.project_id)
                & (MeasurementCohort.id == MeasurementResult.cohort_id),
            )
            .where(
                MeasurementResult.project_id == run.project_id,
                MeasurementCohort.project_id == run.project_id,
                or_(
                    MeasurementCohort.baseline_run_id == run.id,
                    MeasurementResult.followup_run_id == run.id,
                ),
            )
            .order_by(MeasurementResult.created_at.desc(), MeasurementResult.id.desc())
            .limit(11)
        )
    ).all()
    items = await saved_measurements(db, run.project_id, rows[:10])
    for item in items:
        item["report_role"] = (
            "baseline" if item["baseline_run_id"] == run.id else "followup"
        )
    return {"scope_run_id": run.id, "results": items, "truncated": len(rows) > 10}
