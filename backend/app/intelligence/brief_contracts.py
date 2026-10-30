"""Evidence Brief v3 has one complete task and distinct diagnostic/own-fact roles."""

import json
from typing import Literal

from pydantic import AwareDatetime, ConfigDict, Field, StrictInt, model_validator

from app.intelligence.value_contracts import (
    Contract,
    EvidenceRef,
    FactSource,
    Hash,
    Id,
    TaskPackV2,
)

MAX_BRIEF_BYTES = 2_000_000
CHANNELS = Literal[
    "wechat", "weibo", "xiaohongshu", "douyin", "website", "zhihu", "toutiao"
]


class ActionRevisionBinding(Contract):
    id: Id
    revision: StrictInt = Field(ge=1)
    signature: Hash
    dependencySignature: Hash
    taskKey: Hash
    taskSignature: Hash


class OwnFact(Contract):
    id: Id
    version: StrictInt = Field(ge=1)
    ownTargetId: Id
    entityId: Id | None = None
    statement: str = Field(min_length=1, max_length=2000)
    scope: str = Field(min_length=5, max_length=1000)
    validFrom: AwareDatetime
    validUntil: AwareDatetime
    source: FactSource
    referenceUrl: str | None = Field(
        default=None, max_length=4096, pattern=r"^https?://"
    )

    @model_validator(mode="after")
    def exact_fact(self):
        if (
            self.validFrom >= self.validUntil
            or self.source.evidence.quote != self.statement
        ):
            raise ValueError("Own facts preserve their approved quotation and validity")
        return self


class FrozenConditions(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True, protected_namespaces=())
    surface: str | None = Field(default=None, max_length=50)
    search_mode: str | None = Field(default=None, max_length=100)
    model_version: str | None = Field(default=None, max_length=100)
    account_class: str | None = Field(default=None, max_length=100)
    location: str | None = Field(default=None, max_length=100)


class Observation(Contract):
    responseId: Id
    conditions: FrozenConditions


class AITarget(Contract):
    platform: Literal["deepseek", "qwen", "doubao", "kimi", "hunyuan"]
    promptHash: Hash
    prompt: str = Field(min_length=1, max_length=400000)
    scenarioId: Id
    dimensions: tuple[
        Literal["presence", "recommendation", "position", "reasons"], ...
    ] = Field(min_length=1, max_length=4)
    observations: tuple[Observation, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_observations(self):
        if len({row.responseId for row in self.observations}) != len(self.observations):
            raise ValueError("Each target keeps unique captured observations")
        return self


class DiagnosticContext(Contract):
    observedFacts: tuple[str, ...] = ()
    hypotheses: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    counterevidence: tuple[EvidenceRef, ...] = Field(default=(), max_length=100)
    use: Literal["diagnostic_only"] = "diagnostic_only"


class Execution(Contract):
    channel: CHANNELS
    mode: Literal["generate_draft", "manual_task"]

    @model_validator(mode="after")
    def actual_capability(self):
        if (self.channel in ("wechat", "weibo", "xiaohongshu", "douyin")) != (
            self.mode == "generate_draft"
        ):
            raise ValueError("Unsupported generators must remain manual channels")
        return self


class Authorization(Contract):
    id: Id
    version: StrictInt = Field(ge=1)
    scope: Literal["content_draft"]
    signature: Hash
    expiresAt: AwareDatetime


class EvidenceBriefV3(Contract):
    schemaVersion: Literal[3]
    projectId: Id
    runId: Id
    actionId: Id
    title: str = Field(min_length=5, max_length=200)
    actionRevision: ActionRevisionBinding
    taskPack: TaskPackV2
    execution: Execution
    aiTargets: tuple[AITarget, ...] = Field(min_length=1, max_length=100)
    diagnosticContext: DiagnosticContext
    ownFacts: tuple[OwnFact, ...] = Field(min_length=1, max_length=100)
    approval: Authorization
    publicationAllowed: Literal[False]

    @model_validator(mode="after")
    def retain_exact_task(self):
        pack = self.taskPack
        if (
            self.projectId != pack.project_id
            or self.actionRevision.taskKey != pack.task_key()
            or self.actionRevision.taskSignature != pack.revision_signature()
        ):
            raise ValueError("Brief is bound to the exact TaskPack and action revision")
        if pack.intent in ("material_verification", "channel_outreach"):
            raise ValueError(
                "Investigation/outreach tasks cannot become promotional content"
            )
        original = [
            (row.platform, row.prompt_hash, row.scenario_id, row.dimensions)
            for row in pack.ai_targets
        ]
        delivered = [
            (row.platform, row.promptHash, row.scenarioId, row.dimensions)
            for row in self.aiTargets
        ]
        if original != delivered or any(
            not any(
                q.prompt_hash == row.promptHash and q.text == row.prompt
                for q in pack.delivery.questions
            )
            for row in self.aiTargets
        ):
            raise ValueError(
                "Keep every original platform/scenario/question without truncation"
            )
        if tuple(row.id for row in self.ownFacts) != pack.own_fact_ids:
            raise ValueError(
                "Content uses exactly the facts selected in the approved task revision"
            )
        if any(
            row.ownTargetId != pack.own_target_id
            or row.source.evidence.project_id != self.projectId
            or self.approval.expiresAt > row.validUntil
            for row in self.ownFacts
        ):
            raise ValueError("Own facts retain project, brand and validity boundaries")
        if any(
            row.project_id != self.projectId
            for row in self.diagnosticContext.counterevidence
        ):
            raise ValueError("Diagnostic context belongs to the same project")
        if (
            len(
                json.dumps(
                    self.model_dump(mode="json"),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
            )
            > MAX_BRIEF_BYTES
        ):
            raise ValueError(
                "Brief exceeds the shared size bound; refine without truncation"
            )
        return self
