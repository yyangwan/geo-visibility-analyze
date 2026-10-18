"""Strict v2 capture facts; source semantics never inferred from brand lists or counts."""

import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.intelligence.answer_quality import is_status_only_answer
from app.intelligence.blob import digest


class StrictFact(BaseModel):
    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, protected_namespaces=()
    )


class CaptureConditions(StrictFact):
    search_mode: Literal["enabled", "disabled", "unknown"] = Field(
        default="unknown", alias="searchMode"
    )
    model_version: str | None = Field(
        default=None, alias="modelVersion", max_length=100
    )
    account_class: str | None = Field(
        default=None, alias="accountClass", max_length=100
    )
    location: str | None = Field(default=None, max_length=100)


class Answer(StrictFact):
    text: str = Field(min_length=1, max_length=200000)
    status: Literal["complete", "partial"]
    content_hash: str = Field(alias="contentHash", pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def correct_hash(self):
        if is_status_only_answer(self.text):
            raise ValueError("Answer contains only a UI progress label")
        if digest(self.text.encode("utf-8")) != self.content_hash:
            raise ValueError("Answer hash differs from actual UTF-8 bytes")
        return self


class Source(StrictFact):
    ordinal: int = Field(ge=1, le=200)
    kind: Literal[
        "answer_reference",
        "search_candidate",
        "text_link",
        "named_inference",
        "unknown",
    ]
    title: str = Field(default="", max_length=2000)
    url: str | None = Field(default=None, max_length=4096)
    snippet: str = Field(default="", max_length=4000)
    url_resolution: Literal[
        "exact", "site_root", "candidate", "unavailable", "unknown"
    ] = Field(alias="urlResolution")
    answer_anchor: tuple[int, int] | None = Field(default=None, alias="answerAnchor")


class SourcePanel(StrictFact):
    status: Literal["complete", "partial", "unknown", "unavailable"]
    expected_count: int | None = Field(
        default=None, alias="expectedCount", ge=0, le=10000
    )
    captured_count: int = Field(alias="capturedCount", ge=0, le=200)


class Adapter(StrictFact):
    name: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=100)


class CaptureV2(StrictFact):
    contract_version: Literal[2] = Field(alias="contractVersion")
    task_id: str = Field(alias="taskId", min_length=1, max_length=36)
    platform: str = Field(min_length=1, max_length=50)
    surface: Literal["app", "web"]
    observed_at: datetime = Field(alias="observedAt")
    answer: Answer
    conditions: CaptureConditions
    sources: list[Source] = Field(max_length=200)
    source_panel: SourcePanel = Field(alias="sourcePanel")
    adapter: Adapter

    @field_validator("observed_at")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Capture timestamp must include timezone")
        return value

    @model_validator(mode="after")
    def validate_observations(self):
        if self.source_panel.captured_count != len(self.sources):
            raise ValueError("Captured source count differs from actual observations")
        if [source.ordinal for source in self.sources] != list(
            range(1, len(self.sources) + 1)
        ):
            raise ValueError("Source ordinals must be consecutive")
        if (
            self.source_panel.expected_count is not None
            and self.source_panel.expected_count < len(self.sources)
        ):
            raise ValueError(
                "Expected source count cannot be lower than observed count"
            )
        for source in self.sources:
            if source.url_resolution == "exact" and not source.url:
                raise ValueError("Exact URL requires a URL")
            if source.answer_anchor and not (
                0
                <= source.answer_anchor[0]
                < source.answer_anchor[1]
                <= len(self.answer.text)
            ):
                raise ValueError("Answer anchor outside actual answer")
        if (
            len(
                json.dumps(
                    self.model_dump(mode="json", by_alias=True), ensure_ascii=False
                ).encode()
            )
            > 2 * 1024 * 1024
        ):
            raise ValueError("Capture payload exceeds 2MiB")
        return self


class SamplingRequest(StrictFact):
    prompt_ids: list[int] = Field(min_length=1, max_length=100, alias="promptIds")
    platforms: list[Literal["qwen", "deepseek", "doubao", "kimi", "hunyuan"]] = Field(
        min_length=1, max_length=5
    )
    rounds: int = Field(default=1, ge=1, le=10)
    surface: Literal["web", "app", "managed_v1"] = "app"
    conditions: CaptureConditions = Field(default_factory=CaptureConditions)
    idempotency_key: str = Field(alias="idempotencyKey", min_length=8, max_length=100)

    @model_validator(mode="after")
    def unique_inputs(self):
        if len(set(self.prompt_ids)) != len(self.prompt_ids) or len(
            set(self.platforms)
        ) != len(self.platforms):
            raise ValueError("Prompts and platforms must be unique")
        if len(self.prompt_ids) * len(self.platforms) * self.rounds > 1000:
            raise ValueError("Sampling request exceeds 1000 samples")
        if self.surface == "web" and self.platforms != ["qwen"]:
            raise ValueError(
                "The verified web capture handler currently supports qwen only"
            )
        return self


class BrandSnapshot(StrictFact):
    id: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=200)
    aliases: list[str] = Field(default_factory=list, max_length=50)
    is_competitor: bool = False


class SamplingStart(StrictFact):
    sampling: SamplingRequest
    brands: list[BrandSnapshot] = Field(default_factory=list, max_length=100)


class SourceRetry(StrictFact):
    audit_id: int = Field(alias="auditId", gt=0)
    idempotency_key: str = Field(alias="idempotencyKey", min_length=8, max_length=100)
