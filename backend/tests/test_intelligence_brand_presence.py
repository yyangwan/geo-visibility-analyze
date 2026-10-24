from types import SimpleNamespace

from app.intelligence.brand_evaluation import freeze_cohort, summarize_answer_scores
from app.intelligence.brand_presence import (
    build_brand_presence,
    freeze_registered_targets,
)
from app.intelligence.evaluation_reviews import reviewed_cohort
from app.intelligence.readiness import result_readiness


def fixture(*, text="提供其他方案", complete=True, platforms=("qwen",), brands=None):
    brands = brands or [{"id": "own", "name": "云衡"}]
    cells = [
        {
            "sample_id": platform,
            "sample_index": index,
            "platform": platform,
            "prompt_hash": "a" * 64,
            "prompt": "有哪些方案",
            "category": "recommend",
        }
        for index, platform in enumerate(platforms)
    ]
    cohort = reviewed_cohort(freeze_cohort(cells, brands), {"a" * 64: "eligible"})
    manifest = {
        "responses": [
            {
                "id": platform,
                "sample_id": platform,
                "platform": platform,
                "text": text,
                "error": False,
            }
            for platform in platforms
        ],
        "sources": [],
        "brands": brands,
        "brand_targets": freeze_registered_targets(brands),
        "evaluation_cohort": cohort,
        "expected_samples": len(platforms),
    }
    applications = [
        (
            SimpleNamespace(status="completed" if complete else "failed"),
            SimpleNamespace(
                owner_kind="answer",
                owner_id=platform,
                start_offset=0,
                end_offset=len(text),
            ),
            SimpleNamespace(result={"brand_signals": []}),
        )
        for platform in platforms
    ]
    return manifest, applications


def projection(manifest, applications, *, entities=(), mentions=(), signals=None):
    signals = signals or {"observations": []}
    evaluation = summarize_answer_scores(
        manifest, applications, list(entities), list(mentions), signals
    )
    return build_brand_presence(
        manifest, applications, list(entities), list(mentions), signals, evaluation
    )


def test_absent_own_brand_survives_without_inventing_a_mention_or_entity():
    manifest, applications = fixture()
    result = projection(manifest, applications)
    assert result["targets"][0]["entity_id"] is None
    assert result["targets"][0]["target_id"] == "registry:own"
    assert result["observations"][0]["state"] == "observed_absent"
    assert result["observations"][0]["evidence_ids"] == []
    assert result["scope_scores"][0]["total"]["score"] == 0


def test_failed_analysis_is_unknown_and_preserves_the_full_target_row():
    manifest, applications = fixture(complete=False)
    result = projection(manifest, applications)
    assert result["observations"][0]["state"] == "unknown"
    assert "semantic_coverage_incomplete" in result["observations"][0]["reason_codes"]
    assert result["scope_scores"][0]["total"]["score"] is None


def test_pending_planned_answer_never_vanishes_from_the_denominator():
    manifest, _ = fixture()
    manifest["responses"] = []
    result = projection(manifest, [])
    assert result["targets"][0]["name"] == "云衡"
    assert result["observations"][0]["reason_codes"] == ["answer_missing"]
    assert result["scope_scores"][0]["total"]["covered_weight"] == 0


def test_registry_name_not_extracted_is_unknown_instead_of_proven_absence():
    manifest, applications = fixture(text="云衡可作为候选")
    result = projection(manifest, applications)
    assert result["observations"][0]["state"] == "unknown"
    assert (
        "name_or_signal_without_resolved_mention"
        in result["observations"][0]["reason_codes"]
    )


def test_source_only_confirmed_entity_does_not_increase_answer_presence():
    manifest, applications = fixture()
    entity = {
        "id": "found",
        "name": "拓域",
        "kind": "brand",
        "role": "unregistered",
        "identity_status": "confirmed",
    }
    mention = {
        "id": "article-mention",
        "entity_id": "found",
        "owner_kind": "snapshot",
        "owner_id": "article",
    }
    result = projection(manifest, applications, entities=[entity], mentions=[mention])
    target = next(row for row in result["targets"] if row["entity_id"] == "found")
    assert target["source_mentions"] == 1
    assert target["presence_counts"] == {"observed_absent": 1}
    assert (
        next(
            row
            for row in result["observations"]
            if row["target_id"] == target["target_id"]
        )["evidence_ids"]
        == []
    )


def test_unknown_platform_keeps_its_weight_and_does_not_lower_a_missing_total():
    manifest, applications = fixture(platforms=("qwen", "deepseek"))
    applications[1][0].status = "failed"
    result = projection(manifest, applications)
    overall = next(row for row in result["scope_scores"] if row["platform"] == "all")
    assert overall["total"]["score"] is None
    assert overall["total"]["covered_weight"] == 0.5
    assert (
        next(row for row in result["scope_scores"] if row["platform"] == "qwen")[
            "total"
        ]["score"]
        == 0
    )


def test_name_similarity_requires_binding_and_does_not_register_a_discovery():
    manifest, applications = fixture(text="云衡可作为候选")
    entity = {
        "id": "candidate",
        "name": "云衡",
        "kind": "brand",
        "identity_status": "confirmed",
        "role": "unregistered",
    }
    result = projection(manifest, applications, entities=[entity])
    assert len(result["targets"]) == 2
    assert result["targets"][0]["entity_id"] is None
    assert result["targets"][0]["reason_codes"] == ["registered_binding_required"]
    assert result["targets"][1]["registry_brand_id"] is None


def test_unrelated_ambiguous_entity_does_not_block_an_identified_brand():
    manifest, applications = fixture(text="拓域与同名产品")
    entities = [
        {"id": "known", "name": "拓域", "kind": "brand", "identity_status": "confirmed"}
    ]
    mentions = [
        {
            "id": "known-mention",
            "entity_id": "known",
            "owner_kind": "answer",
            "owner_id": "qwen",
            "identity_ambiguity": False,
        },
        {
            "id": "ambiguous-mention",
            "entity_id": "other",
            "owner_kind": "answer",
            "owner_id": "qwen",
            "identity_ambiguity": True,
        },
    ]
    signals = {
        "observations": [
            {
                "id": "signal",
                "response_id": "qwen",
                "entity_id": "known",
                "recommendation": 3,
                "reason_level": "specific",
                "explicit_rank": None,
            }
        ]
    }
    result = projection(
        manifest, applications, entities=entities, mentions=mentions, signals=signals
    )
    row = next(row for row in result["observations"] if row["entity_id"] == "known")
    assert row["state"] == "present"
    assert row["dimensions"]["presence"] == 100
    assert row["dimensions"]["reasons"] == 75


def test_readiness_keeps_source_uncertainty_separate_from_valid_answer_scoring():
    manifest, applications = fixture()
    presence = projection(manifest, applications)
    manifest["sources"] = [{"snapshot_id": "saved"}, {"snapshot_id": None}]
    items = {
        row["key"]: row
        for row in result_readiness(
            manifest,
            {"total_segments": 1, "segment_states": {"completed": 1}},
            {"brand_presence": presence},
            "ready",
        )["items"]
    }
    assert len(items) == 7
    assert items["sources"]["state"] == "partial"
    assert items["scoring"]["state"] == "ready"
    assert items["semantics"]["state"] == "ready"


def test_failed_provider_readiness_explains_a_remedy_without_creating_zero_scores():
    manifest, _ = fixture()
    items = {
        row["key"]: row
        for row in result_readiness(
            manifest,
            {"analysis_error_code": "analysis_provider_insufficient_balance"},
            {},
            "failed",
        )["items"]
    }
    assert items["semantics"]["state"] == "blocked"
    assert "额度不足" in items["semantics"]["detail"]
    assert items["scoring"]["completed"] == 0


def test_alias_collision_blocks_only_affected_registered_targets():
    brands = [
        {"id": "own", "name": "云衡", "aliases": ["同名"]},
        {"id": "other", "name": "别家", "aliases": ["同名"], "is_competitor": True},
        {"id": "independent", "name": "独立品牌", "is_competitor": True},
    ]
    manifest, applications = fixture(brands=brands)
    result = projection(manifest, applications)
    states = {row["target_id"]: row["state"] for row in result["observations"]}
    assert states == {
        "registry:independent": "observed_absent",
        "registry:other": "unknown",
        "registry:own": "unknown",
    }


def test_case_variants_of_one_target_alias_are_not_a_cross_target_collision():
    manifest, applications = fixture(
        brands=[{"id": "own", "name": "CloudBrand", "aliases": ["CLOUDBRAND"]}]
    )
    assert (
        projection(manifest, applications)["observations"][0]["state"]
        == "observed_absent"
    )


def test_pending_prompt_review_retains_weight_and_cannot_publish_zero_total():
    manifest, applications = fixture()
    cohort = manifest["evaluation_cohort"]
    manifest["evaluation_cohort"] = reviewed_cohort(cohort, {"a" * 64: "needs_review"})
    result = projection(manifest, applications)
    assert manifest["evaluation_cohort"]["status"] == "requires_prompt_review"
    assert manifest["evaluation_cohort"]["cells"][0]["weight"]["numerator"] == 1
    assert result["observations"][0]["score"] == 0
    assert result["scope_scores"][0]["total"]["score"] is None


def test_failed_run_detail_exposes_targets_and_safe_blocker_before_any_summary():
    from app.api.intelligence import view
    from app.utils.timezone import utcnow

    manifest, _ = fixture()
    run = SimpleNamespace(id="r1", audit_id=1, revision=1, status="failed", created_at=utcnow(), finished_at=utcnow(), signature="s", input_hash="h", coverage={"analysis_error_code": "analysis_provider_insufficient_balance"}, summary={}, manifest=manifest)
    detail = view(run)
    assert detail["summary"]["brand_presence"]["targets"][0]["entity_id"] is None
    assert detail["summary"]["brand_presence"]["observations"][0]["state"] == "unknown"
    assert "text" not in detail["responses"][0]
    assert detail["readiness"]["items"][1]["state"] == "blocked"
    assert run.summary == {}  # GET must not rewrite the frozen run.
