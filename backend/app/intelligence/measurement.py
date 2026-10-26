"""Frozen sampling and paired, platform-specific descriptive measurement.

Published assets are human reports, never publication authorization or causal proof.
"""

import asyncio
import random
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import Field, field_validator, model_validator
from sqlalchemy import select

from app.config import settings
from app.intelligence.blob import digest
from app.intelligence.capture_contract import CaptureConditions, StrictFact
from app.intelligence.models import (
    ActionPlan,
    AuditSample,
    CohortRetest,
    ContentSegment,
    IntelligenceRun,
    MeasurementCohort,
    MeasurementResult,
    PromptVersion,
    PublishedAsset,
    SamplingAudit,
    SegmentAnalysis,
)
from app.intelligence.sampling import dispatch_attempt
from app.intelligence.semantics import stable_hash
from app.models.models import Audit, AuditStage, QueryStatus
from app.utils.timezone import utc_isoformat, utcnow

METRIC_VERSION = "paired-source-presence-2"
BOOTSTRAP_DRAWS = 2000
MIN_PROMPTS = 20
MIN_ROUNDS = 3


class CohortRequest(StrictFact):
    baseline_run_id: str = Field(alias="baselineRunId", pattern=r"^[a-f0-9]{32}$")
    title: str = Field(min_length=5, max_length=200)
    comparison_mode: Literal["inferential_v1", "descriptive_v1"] = Field(
        default="inferential_v1", alias="comparisonMode"
    )
    entity_ids: list[str] = Field(
        default_factory=list, alias="entityIds", max_length=20
    )
    brand_target_ids: list[str] = Field(
        default_factory=list, alias="brandTargetIds", max_length=20
    )
    platforms: list[str] = Field(default_factory=list, max_length=20)
    prompt_hashes: list[str] = Field(
        default_factory=list, alias="promptHashes", max_length=1000
    )
    idempotency_key: str = Field(
        alias="idempotencyKey", pattern=r"^[a-zA-Z0-9_-]{8,100}$"
    )

    @field_validator("entity_ids")
    @classmethod
    def unique_ids(cls, values):
        import re

        if len(values) != len(set(values)) or any(
            not re.fullmatch(r"[a-f0-9]{32}", value) for value in values
        ):
            raise ValueError("Unique entity IDs required")
        return values

    @model_validator(mode="after")
    def comparison_scope(self):
        import re

        if self.comparison_mode == "inferential_v1":
            if (
                not self.entity_ids
                or self.brand_target_ids
                or self.platforms
                or self.prompt_hashes
            ):
                raise ValueError(
                    "Inferential mode requires entity IDs and its full frozen sampling scope"
                )
        elif not self.brand_target_ids or self.entity_ids:
            raise ValueError(
                "Descriptive mode requires stable brand targets, not observed entity IDs"
            )
        for values in (self.brand_target_ids, self.platforms, self.prompt_hashes):
            if len(values) != len(set(values)):
                raise ValueError("Comparison scope must not contain duplicates")
        if any(
            not re.fullmatch(r"(?:registry|entity):[a-zA-Z0-9_-]{1,180}", value)
            for value in self.brand_target_ids
        ):
            raise ValueError("Stable brand target IDs required")
        if any(
            not re.fullmatch(r"[a-f0-9]{64}", value) for value in self.prompt_hashes
        ):
            raise ValueError("Frozen prompt hashes required")
        return self


class RetestRequest(StrictFact):
    idempotency_key: str = Field(
        alias="idempotencyKey", pattern=r"^[a-zA-Z0-9_-]{8,100}$"
    )


class CompareRequest(StrictFact):
    followup_run_id: str = Field(alias="followupRunId", pattern=r"^[a-f0-9]{32}$")


class AssetRequest(StrictFact):
    action_id: str = Field(alias="actionId", pattern=r"^[a-f0-9]{32}$")
    url: str = Field(min_length=1, max_length=4096)
    channel: str = Field(
        pattern=r"^(wechat|weibo|xiaohongshu|douyin|website|zhihu|toutiao)$"
    )
    content_hash: str = Field(alias="contentHash", pattern=r"^[a-f0-9]{64}$")
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
            raise ValueError("An HTTP publication URL without credentials is required")
        return value

    @field_validator("published_at")
    @classmethod
    def known_time(cls, value):
        if (
            value.tzinfo is None
            or value.utcoffset() is None
            or value.astimezone(UTC).replace(tzinfo=None) > utcnow()
        ):
            raise ValueError("An observed publication time with timezone is required")
        return value


async def scoped_run(db, project_id, run_id):
    run = await db.scalar(
        select(IntelligenceRun).where(
            IntelligenceRun.project_id == project_id, IntelligenceRun.id == run_id
        )
    )
    if run is None:
        raise HTTPException(404, "Run not found")
    if run.status not in ("ready", "partial"):
        raise HTTPException(409, "Wait for published analysis")
    return run


def identity_context(run, ids):
    return {eid: run.manifest.get("entity_resolutions", {}).get(eid, {}) for eid in ids}


async def create_cohort(db, project_id, data, actor_id):
    # Lock the existing baseline row to serialize cohort idempotency and input freezing.
    run = await db.scalar(
        select(IntelligenceRun)
        .where(
            IntelligenceRun.project_id == project_id,
            IntelligenceRun.id == data.baseline_run_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if run is None:
        raise HTTPException(404, "Baseline run not found")
    request_hash = stable_hash(data.model_dump(mode="json", by_alias=True))
    previous = await db.scalar(
        select(MeasurementCohort)
        .where(
            MeasurementCohort.project_id == project_id,
            MeasurementCohort.idempotency_key == data.idempotency_key,
        )
        .with_for_update()
    )
    if previous:
        if previous.request_hash != request_hash:
            raise HTTPException(409, "Cohort key reused for a different request")
        return previous
    if run.status not in ("ready", "partial"):
        raise HTTPException(409, "Wait for published baseline")
    entities = {item["id"]: item for item in run.summary.get("entities", [])}
    if data.comparison_mode == "inferential_v1" and any(
        eid not in entities for eid in data.entity_ids
    ):
        raise HTTPException(422, "Select entities observed in the baseline")
    samples = (
        await db.scalars(
            select(AuditSample)
            .where(
                AuditSample.project_id == project_id,
                AuditSample.audit_id == run.audit_id,
            )
            .order_by(AuditSample.id)
        )
    ).all()
    if not samples or len(samples) > 1000:
        raise HTTPException(
            422, "A bounded capture-v2 independent sampling baseline is required"
        )
    versions = {
        row.id: row
        for row in (
            await db.scalars(
                select(PromptVersion).where(
                    PromptVersion.project_id == project_id,
                    PromptVersion.id.in_({row.prompt_version_id for row in samples}),
                )
            )
        ).all()
    }
    rows = []
    for sample in samples:
        version = versions.get(sample.prompt_version_id)
        if version is None or digest(version.text.encode("utf-8")) != version.text_hash:
            raise HTTPException(409, "Baseline prompt version is unavailable")
        rows.append(
            {
                "sample_id": sample.id,
                "prompt_version_id": version.id,
                "prompt_hash": version.text_hash,
                "platform": sample.platform,
                "surface": sample.surface,
                "sample_index": sample.sample_index,
                "requested_conditions": sample.conditions,
            }
        )
    specification = {
        "version": METRIC_VERSION,
        "comparison_mode": data.comparison_mode,
        "baseline_run_id": run.id,
        "baseline_input_hash": run.input_hash,
        "analysis_signature": run.signature,
        "alias_signature": stable_hash(
            [
                run.manifest.get("entity_aliases", {}),
                run.manifest.get("mention_bindings", {}),
                run.manifest.get("entity_splits", {}),
                run.manifest.get("brand_context"),
            ]
        ),
        "entity_ids": data.entity_ids,
        "entities": [entities[eid] for eid in data.entity_ids],
        "identity_context": identity_context(run, data.entity_ids),
        "slots": rows,
        "min_prompts": MIN_PROMPTS,
        "min_rounds": MIN_ROUNDS,
        "bootstrap_draws": BOOTSTRAP_DRAWS,
        "metric": "answer_presence",
        "causal_claim_allowed": False,
    }
    if data.comparison_mode == "descriptive_v1":
        from app.intelligence.descriptive_measurement import freeze_descriptive

        specification = freeze_descriptive(run, data, rows)
    cohort = MeasurementCohort(
        project_id=project_id,
        baseline_run_id=run.id,
        title=data.title,
        specification=specification,
        specification_hash=stable_hash(specification),
        actor_id=actor_id,
        idempotency_key=data.idempotency_key,
        request_hash=request_hash,
    )
    db.add(cohort)
    await db.flush()
    return cohort


def checked_spec(cohort):
    from app.intelligence.descriptive_measurement import VERSION as DESCRIPTIVE_VERSION
    from app.intelligence.descriptive_measurement import supported_spec

    mode = cohort.specification.get("comparison_mode", "inferential_v1")
    expected_version = (
        DESCRIPTIVE_VERSION if mode == "descriptive_v1" else METRIC_VERSION
    )
    if (
        stable_hash(cohort.specification) != cohort.specification_hash
        or mode not in ("descriptive_v1", "inferential_v1")
        or cohort.specification.get("version") != expected_version
        or mode == "descriptive_v1"
        and not supported_spec(cohort.specification)
    ):
        raise HTTPException(
            409, "Frozen cohort specification changed or is unsupported"
        )
    return cohort.specification


async def start_retest(db, cohort, data, actor_id):
    cohort = await db.scalar(
        select(MeasurementCohort)
        .where(
            MeasurementCohort.project_id == cohort.project_id,
            MeasurementCohort.id == cohort.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    previous = await db.scalar(
        select(CohortRetest)
        .where(
            CohortRetest.project_id == cohort.project_id,
            CohortRetest.cohort_id == cohort.id,
            CohortRetest.idempotency_key == data.idempotency_key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if previous:
        return previous
    spec = checked_spec(cohort)
    baseline = await scoped_run(db, cohort.project_id, cohort.baseline_run_id)
    if baseline.input_hash != spec["baseline_input_hash"]:
        raise HTTPException(409, "Frozen baseline input changed")
    audit = Audit(
        project_id=cohort.project_id,
        status=QueryStatus.RUNNING,
        stage=AuditStage.QUERYING,
        platforms_json=sorted({slot["platform"] for slot in spec["slots"]}),
        brands_json=baseline.manifest.get("brands", []),
    )
    db.add(audit)
    await db.flush()
    db.add(
        SamplingAudit(
            project_id=cohort.project_id,
            audit_id=audit.id,
            idempotency_key=stable_hash([cohort.id, data.idempotency_key]),
            request_hash=stable_hash([cohort.specification_hash, data.idempotency_key]),
            expected_samples=len(spec["slots"]),
            max_attempts=settings.mobile_app_capture_max_attempts,
        )
    )
    for slot in spec["slots"]:
        version = await db.get(PromptVersion, slot["prompt_version_id"])
        if (
            not version
            or version.project_id != cohort.project_id
            or version.text_hash != slot["prompt_hash"]
            or digest(version.text.encode("utf-8")) != slot["prompt_hash"]
        ):
            raise HTTPException(409, "Frozen prompt version is unavailable")
        conditions = CaptureConditions.model_validate(
            slot["requested_conditions"]
        ).model_dump(by_alias=True)
        sample = AuditSample(
            project_id=cohort.project_id,
            audit_id=audit.id,
            prompt_version_id=version.id,
            platform=slot["platform"],
            surface=slot["surface"],
            sample_index=slot["sample_index"],
            conditions=conditions,
            condition_hash=stable_hash([slot["surface"], conditions]),
        )
        db.add(sample)
        await db.flush()
        await dispatch_attempt(db, sample, 1)
    row = CohortRetest(
        project_id=cohort.project_id,
        cohort_id=cohort.id,
        audit_id=audit.id,
        actor_id=actor_id,
        idempotency_key=data.idempotency_key,
    )
    db.add(row)
    await db.flush()
    return row


async def run_snapshot(db, run):
    from app.intelligence.source_metrics import source_snapshot

    applications = (
        await db.execute(
            select(SegmentAnalysis, ContentSegment)
            .join(ContentSegment, SegmentAnalysis.segment_id == ContentSegment.id)
            .where(
                SegmentAnalysis.project_id == run.project_id,
                SegmentAnalysis.run_id == run.id,
                ContentSegment.project_id == run.project_id,
                ContentSegment.owner_kind == "answer",
            )
        )
    ).all()
    states = defaultdict(list)
    for application, segment in applications:
        states[segment.owner_id].append((application.status, segment))
    completed = set()
    for response in run.manifest.get("responses", []):
        rows = sorted(states[response["id"]], key=lambda item: item[1].start_offset)
        position = 0
        valid = bool(rows)
        for status, segment in rows:
            if (
                status != "completed"
                or segment.start_offset != position
                or segment.end_offset - segment.start_offset != len(segment.text)
            ):
                valid = False
            position = segment.end_offset
        if (
            valid
            and position == len(response.get("text", ""))
            and "".join(segment.text for _, segment in rows) == response.get("text", "")
        ):
            completed.add(response["id"])
    hits = defaultdict(set)
    ambiguous = set()
    for mention in run.summary.get("mentions", []):
        if mention["owner_kind"] == "answer":
            if mention.get("identity_ambiguity"):
                completed.discard(mention["owner_id"])
                ambiguous.add(mention["owner_id"])
            hits[mention["entity_id"]].add(mention["owner_id"])
    return {
        "brand_presence": run.summary.get("brand_presence") or {},
        "evaluation_cohort": run.manifest.get("evaluation_cohort") or {},
        "entity_aliases": run.manifest.get("entity_aliases", {}),
        "source_metrics": await source_snapshot(db, run),
        "responses": run.manifest.get("responses", []),
        "completed": completed,
        "ambiguous_identity": ambiguous,
        "hits": hits,
        "signature": run.signature,
        "alias_signature": stable_hash(
            [
                run.manifest.get("entity_aliases", {}),
                run.manifest.get("mention_bindings", {}),
                run.manifest.get("entity_splits", {}),
                run.manifest.get("brand_context"),
            ]
        ),
        "identity_context": run.manifest.get("entity_resolutions", {}),
    }


def slot_key(item):
    return (
        item.get("prompt_version_id"),
        item.get("platform"),
        item.get("surface"),
        item.get("sample_index"),
    )


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(UTC) if parsed.utcoffset() is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def usable(response, snapshot):
    if response.get("error") or response.get("answer_status") != "complete":
        return "incomplete_answer"
    if response["id"] in snapshot.get("ambiguous_identity", set()):
        return "ambiguous_identity"
    if response["id"] in snapshot.get("unusable_reasons", {}):
        return snapshot["unusable_reasons"][response["id"]]
    if response["id"] not in snapshot["completed"]:
        return "incomplete_analysis"
    return None


def known_conditions(item):
    conditions = item.get("conditions") or {}
    if conditions.get("searchMode") not in ("enabled", "disabled") or any(
        not isinstance(conditions.get(key), str)
        or not conditions[key].strip()
        or conditions[key].casefold() in ("unknown", "unavailable")
        for key in ("modelVersion", "accountClass", "location")
    ):
        return None
    return {
        key: conditions[key]
        for key in ("searchMode", "modelVersion", "accountClass", "location")
    }


def bootstrap(clusters, seed):
    rng = random.Random(seed)
    totals = [
        (sum(pair[0] for pair in rows), sum(pair[1] for pair in rows), len(rows))
        for rows in clusters.values()
    ]
    draws = []
    for _ in range(BOOTSTRAP_DRAWS):
        selected = [rng.choice(totals) for _ in totals]
        draws.append(
            sum(after - before for before, after, _ in selected)
            / sum(count for _, _, count in selected)
        )
    draws.sort()
    return [
        draws[int((BOOTSTRAP_DRAWS - 1) * 0.025)],
        draws[int((BOOTSTRAP_DRAWS - 1) * 0.975)],
    ]


def source_trace_examples(observed, snapshot, platform, entity_id, expected):
    examples = {}
    for response in snapshot["responses"]:
        if response["platform"] != platform or slot_key(response) not in expected:
            continue
        hit = next(
            (
                row
                for row in observed["traces"].get(response["id"], [])
                if row["entity_id"] == entity_id
            ),
            None,
        )
        if hit:
            examples[response["id"]] = [hit]
        if len(examples) >= 20:
            break
    return examples


def compare_snapshots(spec, baseline, followup, *, include_sources=True):
    if spec.get("comparison_mode") == "descriptive_v1":
        from app.intelligence.descriptive_measurement import compare_descriptive

        return compare_descriptive(spec, baseline, followup)
    base_rows, next_rows = defaultdict(list), defaultdict(list)
    for row in baseline["responses"]:
        base_rows[slot_key(row)].append(row)
    for row in followup["responses"]:
        next_rows[slot_key(row)].append(row)
    baseline_ids = Counter(row["id"] for row in baseline["responses"])
    followup_ids = Counter(row["id"] for row in followup["responses"])
    signature_equal = (
        spec["analysis_signature"] == baseline["signature"] == followup["signature"]
    )
    identity_equal = spec.get("alias_signature", stable_hash({})) == followup.get(
        "alias_signature", stable_hash({})
    ) and spec["identity_context"] == {
        eid: followup["identity_context"].get(eid, {}) for eid in spec["entity_ids"]
    }
    platforms = []
    expected = {slot_key(slot) for slot in spec["slots"]}
    for platform in sorted({slot["platform"] for slot in spec["slots"]}):
        included, excluded = [], []
        descriptive = {"baseline": [], "followup": []}
        for slot in [s for s in spec["slots"] if s["platform"] == platform]:
            key = slot_key(slot)
            before, after = base_rows[key], next_rows[key]
            for phase, candidates, snapshot in (
                ("baseline", before, baseline),
                ("followup", after, followup),
            ):
                if len(candidates) == 1 and not usable(candidates[0], snapshot):
                    descriptive[phase].append(candidates[0])
            reason = (
                "missing_baseline"
                if not before
                else "missing_followup"
                if not after
                else "ambiguous_sample"
                if len(before) != 1
                or len(after) != 1
                or baseline_ids[before[0]["id"]] != 1
                or followup_ids[after[0]["id"]] != 1
                else None
            )
            if reason is None:
                b, a = before[0], after[0]
                btime, atime = (
                    timestamp(b.get("observed_at")),
                    timestamp(a.get("observed_at")),
                )
                reason = usable(b, baseline) or usable(a, followup)
                if not reason and (not btime or not atime or atime <= btime):
                    reason = "observation_not_later"
                if not reason and (not known_conditions(b) or not known_conditions(a)):
                    reason = "unknown_conditions"
                if not reason and known_conditions(b) != known_conditions(a):
                    reason = "conditions_changed"
                if (
                    not reason
                    and b.get("prompt_hash") != slot["prompt_hash"]
                    or not reason
                    and a.get("prompt_hash") != slot["prompt_hash"]
                ):
                    reason = "prompt_version_changed"
                if not reason and not signature_equal:
                    reason = "analysis_signature_changed"
                if not reason and not identity_equal:
                    reason = "entity_mapping_changed"
            trace = {
                "baseline_sample_id": slot["sample_id"],
                "prompt_version_id": slot["prompt_version_id"],
                "sample_index": slot["sample_index"],
                "baseline_response_id": before[0]["id"] if len(before) == 1 else None,
                "followup_response_id": after[0]["id"] if len(after) == 1 else None,
            }
            if reason:
                excluded.append({**trace, "reason": reason})
            else:
                included.append({**trace, "conditions": known_conditions(before[0])})
        counts = Counter(item["prompt_version_id"] for item in included)
        valid = []
        for item in included:
            if counts[item["prompt_version_id"]] < MIN_ROUNDS:
                excluded.append({**item, "reason": "insufficient_rounds"})
            else:
                valid.append(item)
        prompt_count = len({row["prompt_version_id"] for row in valid})
        status = (
            "valid_comparison"
            if prompt_count >= MIN_PROMPTS
            else "insufficient"
            if included
            else "descriptive_only"
            if descriptive["baseline"] and descriptive["followup"]
            else "undetermined"
        )
        metrics = []
        for eid in spec["entity_ids"]:
            before_count = sum(
                row["baseline_response_id"] in baseline["hits"].get(eid, set())
                for row in valid
            )
            after_count = sum(
                row["followup_response_id"] in followup["hits"].get(eid, set())
                for row in valid
            )
            clusters = defaultdict(list)
            for row in valid:
                clusters[row["prompt_version_id"]].append(
                    (
                        int(
                            row["baseline_response_id"]
                            in baseline["hits"].get(eid, set())
                        ),
                        int(
                            row["followup_response_id"]
                            in followup["hits"].get(eid, set())
                        ),
                    )
                )
            metrics.append(
                {
                    "entity_id": eid,
                    "metric": "answer_presence",
                    "baseline": {"numerator": before_count, "denominator": len(valid)},
                    "followup": {"numerator": after_count, "denominator": len(valid)},
                    "difference": (after_count - before_count) / len(valid)
                    if status == "valid_comparison"
                    else None,
                    "interval95": bootstrap(
                        clusters, stable_hash([spec, platform, eid])
                    )
                    if status == "valid_comparison"
                    else None,
                    "descriptive": {
                        phase: {
                            "numerator": sum(
                                row["id"] in snapshot["hits"].get(eid, set())
                                for row in descriptive[phase]
                            ),
                            "denominator": len(descriptive[phase]),
                        }
                        for phase, snapshot in (
                            ("baseline", baseline),
                            ("followup", followup),
                        )
                    },
                }
            )
        extra = [
            {"response_id": row["id"], "reason": "outside_frozen_cohort"}
            for row in followup["responses"]
            if row["platform"] == platform and slot_key(row) not in expected
        ]
        platforms.append(
            {
                "platform": platform,
                "status": status,
                "expected_samples": sum(
                    s["platform"] == platform for s in spec["slots"]
                ),
                "prompt_clusters": prompt_count,
                "included": valid,
                "excluded": excluded,
                "out_of_cohort": extra,
                "metrics": metrics,
            }
        )
    if include_sources:
        from app.intelligence.source_metrics import METRICS

        for metric in METRICS:
            before_metric = baseline.get("source_metrics", {}).get(metric)
            after_metric = followup.get("source_metrics", {}).get(metric)
            if before_metric is None or after_metric is None:
                continue
            extra = compare_snapshots(
                spec,
                {**baseline, **before_metric, "ambiguous_identity": set()},
                {**followup, **after_metric, "ambiguous_identity": set()},
                include_sources=False,
            )
            for platform in platforms:
                source_platform = next(
                    row
                    for row in extra["platforms"]
                    if row["platform"] == platform["platform"]
                )
                platform.setdefault("source_populations", {})[metric] = {
                    key: source_platform[key]
                    for key in ("status", "prompt_clusters", "included", "excluded")
                }
                for value in source_platform["metrics"]:
                    eid = value["entity_id"]
                    platform["metrics"].append(
                        {
                            **value,
                            "metric": metric,
                            "status": source_platform["status"],
                            "paired_positive_response_ids": {
                                phase: sorted(
                                    {
                                        row[f"{phase}_response_id"]
                                        for row in source_platform["included"]
                                        if row[f"{phase}_response_id"]
                                        in observed["hits"].get(eid, set())
                                    }
                                )
                                for phase, observed in (
                                    ("baseline", before_metric),
                                    ("followup", after_metric),
                                )
                            },
                            "traces": {
                                phase: source_trace_examples(
                                    observed,
                                    snapshot,
                                    platform["platform"],
                                    eid,
                                    expected,
                                )
                                for phase, observed, snapshot in (
                                    ("baseline", before_metric, baseline),
                                    ("followup", after_metric, followup),
                                )
                            },
                        }
                    )
    return {
        "version": METRIC_VERSION,
        "comparison_mode": "inferential_v1",
        "analysis_signature_equal": signature_equal,
        "identity_context_equal": identity_equal,
        "platforms": platforms,
        "method": {
            "name": "paired_prompt_cluster_bootstrap",
            "draws": BOOTSTRAP_DRAWS,
            "confidence": 0.95,
            "min_prompts": MIN_PROMPTS,
            "min_rounds": MIN_ROUNDS,
        },
        "causal_claim_allowed": False,
        "business_metrics": {
            "clicks": "unknown",
            "conversions": "unknown",
            "roi": "unknown",
        },
        "limitations": [
            "Observational change cannot establish attribution to a content action",
            "Platform internal model changes may be unobserved",
            "Thresholds require pilot power validation",
            "Excluded samples limit the measured population",
            "Stored archive presence does not establish complete published article coverage or use by the model",
            "Multiple outcomes are exploratory; intervals are pointwise, not multiplicity adjusted",
        ],
    }


async def compare_cohort(db, cohort, data, actor_id):
    cohort = await db.scalar(
        select(MeasurementCohort)
        .where(
            MeasurementCohort.project_id == cohort.project_id,
            MeasurementCohort.id == cohort.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    spec = checked_spec(cohort)
    existing = await db.scalar(
        select(MeasurementResult)
        .where(
            MeasurementResult.project_id == cohort.project_id,
            MeasurementResult.cohort_id == cohort.id,
            MeasurementResult.followup_run_id == data.followup_run_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if existing:
        return existing
    baseline = await scoped_run(db, cohort.project_id, cohort.baseline_run_id)
    followup = await scoped_run(db, cohort.project_id, data.followup_run_id)
    if baseline.audit_id == followup.audit_id:
        raise HTTPException(
            422,
            "A new independent sampling audit is required, not an enrichment revision",
        )
    if baseline.input_hash != spec["baseline_input_hash"]:
        raise HTTPException(409, "Frozen baseline input changed")
    result = await asyncio.to_thread(
        compare_snapshots,
        spec,
        await run_snapshot(db, baseline),
        await run_snapshot(db, followup),
    )
    result.update(
        {
            "baseline_run_id": baseline.id,
            "followup_run_id": followup.id,
            "cohort_id": cohort.id,
            "entities": spec["entities"],
            "baseline_input_hash": baseline.input_hash,
            "followup_input_hash": followup.input_hash,
            "cohort_specification_hash": cohort.specification_hash,
            "comparison_analysis_signature": baseline.signature
            if baseline.signature == followup.signature
            else None,
        }
    )
    row = MeasurementResult(
        project_id=cohort.project_id,
        cohort_id=cohort.id,
        followup_run_id=followup.id,
        comparison_signature=stable_hash(
            [
                spec["version"],
                cohort.specification_hash,
                [baseline.id, baseline.input_hash, baseline.signature],
                [followup.id, followup.input_hash, followup.signature],
            ]
        ),
        result=result,
        actor_id=actor_id,
    )
    db.add(row)
    await db.flush()
    return row


async def record_asset(db, project_id, data, actor_id):
    action = await db.scalar(
        select(ActionPlan)
        .where(ActionPlan.project_id == project_id, ActionPlan.id == data.action_id)
        .with_for_update()
    )
    if action is None:
        raise HTTPException(404, "Action not found")
    request_hash = stable_hash(data.model_dump(mode="json", by_alias=True))
    previous = await db.scalar(
        select(PublishedAsset).where(
            PublishedAsset.project_id == project_id,
            PublishedAsset.idempotency_key == data.idempotency_key,
        )
    )
    if previous:
        if previous.request_hash != request_hash:
            raise HTTPException(409, "Asset key reused for a different request")
        return previous
    asset = PublishedAsset(
        project_id=project_id,
        action_id=action.id,
        url=data.url,
        channel=data.channel,
        content_hash=data.content_hash,
        published_at=data.published_at.astimezone(UTC).replace(tzinfo=None),
        reason=data.reason,
        actor_id=actor_id,
        idempotency_key=data.idempotency_key,
        request_hash=request_hash,
    )
    db.add(asset)
    await db.flush()
    return asset


def asset_view(row):
    return {
        "id": row.id,
        "action_id": row.action_id,
        "url": row.url,
        "channel": row.channel,
        "content_hash": row.content_hash,
        "published_at": utc_isoformat(row.published_at),
        "verification": "human_reported",
        "reason": row.reason,
        "publication_authorized": False,
    }
