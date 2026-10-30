import pytest
from pydantic import TypeAdapter, ValidationError

from app.intelligence.value_contracts import (
    BrandTarget,
    EvidenceRef,
    FactSource,
    ObservationSpec,
    PresenceObservation,
    TaskPackV2,
)


def evidence(**changes):
    return {
        "project_id": "p1",
        "run_id": "r1",
        "owner_kind": "answer",
        "owner_id": "answer1",
        "segment_id": "segment1",
        "text_hash": "a" * 64,
        "quote": "云衡🙂",
        "start": 0,
        "end": 3,
        **changes,
    }


def spec(**changes):
    return {
        "comparison_mode": "descriptive_v1",
        "metric_version": "presence-v1",
        "brand_target_ids": ["brand:absent"],
        "prompt_hashes": ["a" * 64],
        "platforms": ["qwen"],
        "identity_map_hash": "b" * 64,
        "condition_policy_version": "conditions-v1",
        **changes,
    }


def task(**changes):
    return {
        "project_id": "p1",
        "own_target_id": "brand:own",
        "asset_key": "specs-page",
        "fact_change_key": "role-boundary",
        "intent": "website_update",
        "deliverable": "更新角色权限表并注明适用范围",
        "delivery": {
            "asset_title": "角色权限表", "asset_kind": "new_deliverable",
            "audience_status": "unknown", "outline": ["核对角色权限范围", "补充逐项事实依据"],
            "channel": "own_website", "channel_reason": "为目标问题提供可核对的自有资料",
            "questions": [{"prompt_hash": "a" * 64, "text": "如何选择门店权限工具"}],
        },
        "acceptance": ["每项主张具有批准事实"],
        "ai_targets": [
            {
                "platform": "qwen",
                "prompt_hash": "a" * 64,
                "scenario_id": "implementation",
                "dimensions": ["reasons"],
            }
        ],
        "diagnostic_evidence": [evidence()],
        **changes,
    }


def test_unobserved_registry_target_does_not_need_a_fake_entity():
    row = BrandTarget(
        target_id="brand:absent",
        registry_brand_id="registry1",
        role="own",
        identity_state="confirmed",
        names=("云衡",),
    )
    assert row.entity_id is None
    with pytest.raises(ValidationError):
        BrandTarget(
            target_id="unknown", role="own", identity_state="confirmed", names=("云衡",)
        )


@pytest.mark.parametrize(
    "missing",
    [
        "answer_complete",
        "semantic_complete",
        "identity_confirmed",
        "full_scan_complete",
    ],
)
def test_absence_is_rejected_when_any_proof_is_missing(missing):
    values = {
        "target_id": "brand:absent",
        "sample_id": "s1",
        "owner_kind": "answer",
        "state": "observed_absent",
        "answer_complete": True,
        "semantic_complete": True,
        "identity_confirmed": True,
        "full_scan_complete": True,
    }
    PresenceObservation(**values)
    values[missing] = False
    with pytest.raises(ValidationError):
        PresenceObservation(**values)


def test_unknown_needs_reason_and_article_cannot_claim_whole_absence():
    with pytest.raises(ValidationError):
        PresenceObservation(
            target_id="t", sample_id="s", owner_kind="answer", state="unknown"
        )
    PresenceObservation(
        target_id="t",
        sample_id="s",
        owner_kind="snapshot",
        state="unknown",
        reason_codes=("article_completeness_unknown",),
    )
    with pytest.raises(ValidationError):
        PresenceObservation(
            target_id="t",
            sample_id="s",
            owner_kind="snapshot",
            state="observed_absent",
            answer_complete=True,
            semantic_complete=True,
            identity_confirmed=True,
            full_scan_complete=True,
        )


def test_owned_fact_source_requires_no_fabricated_citation_review():
    adapter = TypeAdapter(FactSource)
    value = {
        "kind": "owned_asset_revision",
        "asset_id": "asset1",
        "asset_revision": 1,
        "verification_id": "verify1",
        "evidence": evidence(owner_kind="owned_asset", owner_id="asset1", run_id=None, segment_id=None),
    }
    assert adapter.validate_python(value).kind == "owned_asset_revision"
    with pytest.raises(ValidationError):
        adapter.validate_python({**value, "citation_id": "fake"})
    with pytest.raises(ValidationError):
        adapter.validate_python({**value, "evidence": evidence()})


def test_evidence_uses_unicode_and_does_not_accept_incoherent_offsets():
    assert EvidenceRef(**evidence()).end == 3
    with pytest.raises(ValidationError):
        EvidenceRef(**evidence(end=4))


def test_task_dedup_key_is_stable_but_added_platform_requires_new_signature():
    first = TaskPackV2(**task())
    targets = [*task()["ai_targets"], {**task()["ai_targets"][0], "platform": "kimi"}]
    second = TaskPackV2(**task(ai_targets=targets))
    assert first.task_key() == second.task_key()
    assert first.revision_signature() != second.revision_signature()
    with pytest.raises(ValidationError):
        TaskPackV2(**task(diagnostic_evidence=[evidence(project_id="other")]))


def test_comparison_cache_separates_modes_and_preserves_unobserved_targets():
    description = ObservationSpec(**spec())
    inference = ObservationSpec(**spec(comparison_mode="inferential_v1"))
    assert description.specification_hash() != inference.specification_hash()
    assert description.brand_target_ids == ("brand:absent",)
    with pytest.raises(ValidationError):
        ObservationSpec(**spec(brand_target_ids=["brand:absent", "brand:absent"]))


def test_comparison_scope_set_order_does_not_change_cache_identity():
    first = ObservationSpec(**spec(platforms=["qwen", "kimi"]))
    second = ObservationSpec(**spec(platforms=["kimi", "qwen"]))
    assert first.specification_hash() == second.specification_hash()
