"""Value-workflow contracts. Structural validation is not evidence authorization.

Contracts are wired incrementally as workflows land. Structural validity never
grants identity, fact approval or publication rights.
"""

from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    model_validator,
)

from app.intelligence.semantics import stable_hash

Id = Annotated[str, Field(min_length=1, max_length=200)]
Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceRef(Contract):
    project_id: Id
    run_id: Id | None = None
    owner_kind: Literal["answer", "citation", "snapshot", "owned_asset"]
    owner_id: Id
    segment_id: Id | None = None
    text_hash: Hash
    quote: str = Field(min_length=1, max_length=2000)
    start: StrictInt = Field(ge=0)
    end: StrictInt = Field(gt=0)

    @model_validator(mode="after")
    def coherent_span(self):
        if self.end - self.start != len(self.quote):
            raise ValueError("Evidence uses exact Unicode offsets")
        if self.owner_kind != "owned_asset" and (not self.run_id or not self.segment_id):
            raise ValueError("Observed evidence requires its actual run and segment")
        return self


class CitationReviewSource(Contract):
    kind: Literal["citation_review"]
    review_id: Id
    evidence_id: Id
    citation_id: Id
    snapshot_id: Id
    evidence: EvidenceRef

    @model_validator(mode="after")
    def citation_evidence(self):
        if self.evidence.owner_kind != "snapshot":
            raise ValueError("Citation facts need the reviewed source snapshot")
        return self


class OwnedAssetSource(Contract):
    kind: Literal["owned_asset_revision"]
    asset_id: Id
    asset_revision: StrictInt = Field(ge=1)
    verification_id: Id
    evidence: EvidenceRef

    @model_validator(mode="after")
    def owned_evidence(self):
        if self.evidence.owner_kind != "owned_asset":
            raise ValueError(
                "Owned facts need owned-asset evidence, not a fake citation"
            )
        if self.evidence.owner_id != self.asset_id:
            raise ValueError("Owned evidence must belong to this material revision")
        return self


FactSource = Annotated[
    CitationReviewSource | OwnedAssetSource, Field(discriminator="kind")
]


class BrandTarget(Contract):
    target_id: Id
    registry_brand_id: Id | None = None
    entity_id: Id | None = None
    role: Literal["own", "competitor", "observed", "comparison", "related"]
    identity_state: Literal["confirmed", "pending", "ambiguous"]
    names: tuple[Id, ...] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def target_origin(self):
        if not self.registry_brand_id and not self.entity_id:
            raise ValueError(
                "Target must reference a registry brand or discovered entity"
            )
        return self


class PresenceObservation(Contract):
    target_id: Id
    sample_id: Id
    owner_kind: Literal["answer", "snapshot"]
    state: Literal["present", "observed_absent", "unknown"]
    answer_complete: StrictBool = False
    semantic_complete: StrictBool = False
    identity_confirmed: StrictBool = False
    full_scan_complete: StrictBool = False
    evidence_ids: tuple[Id, ...] = ()
    reason_codes: tuple[Id, ...] = ()

    @model_validator(mode="after")
    def absence_needs_proof(self):
        if self.state == "present" and not self.evidence_ids:
            raise ValueError("Presence requires observed evidence")
        if self.state == "unknown" and not self.reason_codes:
            raise ValueError("Unknown requires an explicit reason")
        if self.state == "observed_absent" and (
            self.owner_kind != "answer"
            or not all(
                (
                    self.answer_complete,
                    self.semantic_complete,
                    self.identity_confirmed,
                    self.full_scan_complete,
                )
            )
            or self.evidence_ids
        ):
            raise ValueError(
                "Absence needs whole-answer proof, never fabricated mentions"
            )
        return self


class ActionTarget(Contract):
    platform: Literal["deepseek", "qwen", "kimi", "doubao", "hunyuan"]
    prompt_hash: Hash
    scenario_id: Id
    dimensions: tuple[
        Literal["presence", "recommendation", "position", "reasons"], ...
    ] = Field(min_length=1)


class TaskQuestion(Contract):
    prompt_hash: Hash
    text: str = Field(min_length=1, max_length=200000)


class DeliverySpec(Contract):
    asset_title: str = Field(min_length=1, max_length=500)
    asset_kind: Literal["existing_material", "new_deliverable"]
    material_revision_id: Id | None = None
    material_version: StrictInt | None = Field(default=None, ge=1)
    reference_url: str | None = Field(default=None, max_length=4096)
    audience: tuple[str, ...] = Field(default=(), max_length=20)
    audience_status: Literal["reviewed_prompt", "unknown"]
    outline: tuple[Annotated[str, Field(min_length=5, max_length=2000)], ...] = Field(min_length=2, max_length=20)
    channel: Literal["own_website", "material_library", "reference_publisher"]
    channel_reason: str = Field(min_length=5, max_length=2000)
    questions: tuple[TaskQuestion, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def consistent_asset(self):
        if self.asset_kind == "existing_material" and not (self.material_revision_id and self.material_version) or self.asset_kind == "new_deliverable" and (self.material_revision_id is not None or self.material_version is not None):
            raise ValueError("Existing material requires its actual frozen revision")
        if len({row.prompt_hash for row in self.questions}) != len(self.questions):
            raise ValueError("Keep each original question exactly once")
        return self


class TaskPackV2(Contract):
    version: Literal["task-pack-v2"] = "task-pack-v2"
    project_id: Id
    own_target_id: Id
    asset_key: Id
    fact_change_key: Id
    intent: Literal[
        "content_create",
        "content_update",
        "website_update",
        "fact_correction",
        "channel_outreach",
        "material_verification",
    ]
    deliverable: str = Field(min_length=5, max_length=2000)
    delivery: DeliverySpec
    acceptance: tuple[Annotated[str, Field(min_length=5, max_length=1000)], ...] = (
        Field(min_length=1, max_length=30)
    )
    ai_targets: tuple[ActionTarget, ...] = Field(min_length=1, max_length=100)
    diagnostic_evidence: tuple[EvidenceRef, ...] = Field(min_length=1, max_length=100)
    own_fact_ids: tuple[Id, ...] = Field(default=(), max_length=100)
    missing_materials: tuple[str, ...] = ()

    @model_validator(mode="after")
    def project_boundary(self):
        if any(e.project_id != self.project_id for e in self.diagnostic_evidence):
            raise ValueError("Task evidence must belong to the same project")
        keys = [(t.platform, t.prompt_hash, t.scenario_id) for t in self.ai_targets]
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate AI targets must be merged explicitly")
        if {row.prompt_hash for row in self.delivery.questions} != {row.prompt_hash for row in self.ai_targets}:
            raise ValueError("Every AI target must retain its original question")
        return self

    def task_key(self):
        # Excludes platform/prompt so the same real asset change can share targets.
        return stable_hash(
            [
                self.project_id,
                self.own_target_id,
                self.asset_key,
                self.fact_change_key,
                self.intent,
            ]
        )

    def revision_signature(self):
        # All child targets and evidence remain signed; mutable execution is separate.
        return stable_hash(self.model_dump(mode="json"))


class ObservationSpec(Contract):
    comparison_mode: Literal["descriptive_v1", "inferential_v1"]
    metric_version: Id
    brand_target_ids: tuple[Id, ...] = Field(min_length=1, max_length=100)
    prompt_hashes: tuple[Hash, ...] = Field(min_length=1, max_length=1000)
    platforms: tuple[Literal["deepseek", "qwen", "kimi", "doubao", "hunyuan"], ...] = (
        Field(min_length=1)
    )
    rule_version: Literal["brand-visibility-v1"] = "brand-visibility-v1"
    identity_map_hash: Hash
    condition_policy_version: Id

    @model_validator(mode="after")
    def no_duplicate_scope(self):
        for values in (self.brand_target_ids, self.prompt_hashes, self.platforms):
            if len(set(values)) != len(values):
                raise ValueError("Comparison scope must have unique members")
        return self

    def specification_hash(self):
        fields = self.model_dump(mode="json")
        for key in ("brand_target_ids", "prompt_hashes", "platforms"):
            fields[key] = sorted(fields[key])
        return stable_hash(fields)


class ExecutionState(Contract):
    state: Literal[
        "not_started",
        "preparing_materials",
        "in_progress",
        "delivered",
        "accepted",
        "cancelled",
    ]
    assignee_id: Id | None = None
    due_at: AwareDatetime | None = None
    workflow_id: Id | None = None
