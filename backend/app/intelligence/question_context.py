"""Literal suggestions and named human decisions for frozen question contexts."""

import re
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

VERSION = "question-context-v1"
SCENARIOS = {
    "awareness": "认知了解",
    "selection": "方案与品类选型",
    "features": "功能比较",
    "cost": "价格与成本",
    "implementation": "实施适配",
    "proof": "案例与验证",
    "risk": "风险限制",
    "service": "售后服务",
}
Scenario = Literal[
    "awareness",
    "selection",
    "features",
    "cost",
    "implementation",
    "proof",
    "risk",
    "service",
]
KEYWORDS = {
    "awareness": ("是什么", "介绍", "了解", "what is"),
    "selection": ("选型", "选择", "推荐", "替代", "方案", "工具", "平台"),
    "features": ("功能", "参数", "性能", "精度", "对比", "比较", "区别"),
    "cost": ("价格", "报价", "预算", "成本", "收费", "费用", "性价比", "pricing"),
    "implementation": ("实施", "部署", "适配", "集成", "权限", "门店", "兼容", "接入"),
    "proof": ("案例", "测评", "测试", "验证", "证明", "实测", "客户实例"),
    "risk": ("风险", "限制", "缺点", "不足", "隐私", "合规", "安全", "避坑"),
    "service": ("售后", "客服", "培训", "维护", "质保", "保修", "响应时间"),
}


class PromptSpan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quote: str = Field(min_length=1, max_length=2000)
    start: StrictInt = Field(ge=0)
    end: StrictInt = Field(ge=1)


class QuestionReview(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    status: Literal["confirmed", "needs_review"]
    primary_scenario: Scenario | None = Field(default=None, alias="primaryScenario")
    secondary_scenarios: list[Scenario] = Field(
        default_factory=list, alias="secondaryScenarios", max_length=7
    )
    brand_scope: Literal["neutral", "directed", "uncertain"] = Field(alias="brandScope")
    brand_references: list[PromptSpan] = Field(
        default_factory=list, alias="brandReferences", max_length=20
    )
    intent: str | None = Field(default=None, max_length=500)
    audience: list[PromptSpan] = Field(default_factory=list, max_length=20)
    usage_context: list[PromptSpan] = Field(
        default_factory=list, alias="usageContext", max_length=20
    )
    requirements: list[PromptSpan] = Field(default_factory=list, max_length=30)
    constraints: list[PromptSpan] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def consistent(self):
        if (
            len(set(self.secondary_scenarios)) != len(self.secondary_scenarios)
            or self.primary_scenario in self.secondary_scenarios
        ):
            raise ValueError("Primary and secondary scenarios must be distinct")
        if self.status == "confirmed" and (
            self.primary_scenario is None or self.brand_scope == "uncertain"
        ):
            raise ValueError(
                "Confirmed context requires a primary scenario and resolved brand scope"
            )
        if self.brand_scope == "neutral" and self.brand_references:
            raise ValueError(
                "Neutral question cannot also have directed brand references"
            )
        if self.brand_scope == "directed" and not self.brand_references:
            raise ValueError("Directed question requires a verbatim reference")
        return self


def literal_matches(text, phrase):
    pattern = re.escape(phrase)
    if phrase.isascii() and phrase[0].isalnum():
        pattern = r"(?<![A-Za-z0-9_])" + pattern
    if phrase.isascii() and phrase[-1].isalnum():
        pattern += r"(?![A-Za-z0-9_])"
    return [
        {"quote": match.group(), "start": match.start(), "end": match.end()}
        for match in re.finditer(pattern, text, flags=re.IGNORECASE | re.ASCII)
    ]


def question_proposal(prompt, names):
    suggestions = [
        {
            "scenario": key,
            "evidence": [
                span for word in words for span in literal_matches(prompt, word)
            ],
        }
        for key, words in KEYWORDS.items()
    ]
    suggestions = [row for row in suggestions if row["evidence"]]
    references = []
    for name in names:
        for span in literal_matches(prompt, name):
            before, after = (
                prompt[max(0, span["start"] - 8) : span["start"]],
                prompt[span["end"] : span["end"] + 20],
            )
            directed = bool(
                re.search(r"(?:介绍|评价|推荐|对比|比较|了解)(?:一下)?\s*$", before)
                or re.match(
                    r"\s*(?:如何|怎么样|好不好|好吗|适合|支持|提供|和|与|vs\b|相比|是否|能否|能不能|值得|实施|价格|报价|收费|优势|缺点|是什么|做什么)",
                    after,
                    re.IGNORECASE,
                )
            )
            references.append(
                {
                    **span,
                    "registered_name": name,
                    "screen": "directed" if directed else "ambiguous",
                    "fixed_exclusion": bool(
                        re.search(
                            r"(?:品牌|产品|公司|厂商|供应商)\s*[:：]?\s*$", before
                        )
                        or re.match(r"\s*(?:品牌|产品|公司|集团)", after)
                    ),
                }
            )
    return {
        "version": VERSION,
        "method": "literal_rules_unreviewed",
        "scenarios": suggestions,
        "registered_references": references,
        "review": None,
    }


def validate_review(prompt, review, eligibility, fixed_excluded):
    for field in (
        "brand_references",
        "audience",
        "usage_context",
        "requirements",
        "constraints",
    ):
        for span in getattr(review, field):
            if (
                not (span.start < span.end <= len(prompt))
                or prompt[span.start : span.end] != span.quote
            ):
                raise HTTPException(
                    422,
                    "Question evidence must match its frozen prompt and Unicode offsets",
                )
    if review.brand_scope != "neutral" and eligibility == "eligible":
        raise HTTPException(
            422, "Directed or uncertain questions cannot enter neutral competition"
        )
    if review.brand_scope == "uncertain" and eligibility != "needs_review":
        raise HTTPException(422, "Uncertain question must retain review status")
    if fixed_excluded and review.brand_scope != "directed":
        raise HTTPException(
            422, "Explicit registered-brand question must remain directed"
        )


def scenario_summary(cohort):
    rows = (cohort or {}).get("cells", [])
    return {
        "version": VERSION,
        "taxonomy": SCENARIOS,
        "total_questions": len({row["prompt_hash"] for row in rows}),
        "reviewed_questions": len(
            {
                row["prompt_hash"]
                for row in rows
                if (row.get("question_context", {}).get("review") or {}).get("status")
                == "confirmed"
            }
        ),
        "scope_policy": "reviewed-primary-only-secondary-tags-do-not-add-samples",
    }


def scenario_projection(manifest, presence):
    from app.intelligence.brand_evaluation import WEIGHTS, aggregate_scores
    from app.intelligence.competitive_comparison import scoped_cohort

    cohort = manifest.get("evaluation_cohort")
    summary = scenario_summary(cohort)
    questions = {}
    for cell in (cohort or {}).get("cells", []):
        questions.setdefault(
            cell["prompt_hash"],
            {
                "prompt_hash": cell["prompt_hash"],
                "prompt": cell["prompt"],
                "category": cell["category"],
                "eligibility": cell["eligibility"],
                "primary_scenario": cell.get("primary_scenario"),
                "question_context": cell.get("question_context"),
            },
        )
    rows = []
    for scenario in sorted(
        {
            cell["primary_scenario"]
            for cell in (cohort or {}).get("cells", [])
            if cell.get("primary_scenario") and cell["weight"]["numerator"]
        }
    ):
        for platform in [
            "all",
            *sorted(
                {
                    cell["platform"]
                    for cell in cohort["cells"]
                    if cell.get("primary_scenario") == scenario
                    and cell["weight"]["numerator"]
                }
            ),
        ]:
            subset = scoped_cohort(
                cohort,
                "scenario" if platform == "all" else "platform_scenario",
                scenario if platform == "all" else (platform, scenario),
            )
            samples = {cell["sample_id"] for cell in subset["cells"]}
            for target in presence["targets"]:
                observations = [
                    row
                    for row in presence["observations"]
                    if row["target_id"] == target["target_id"]
                    and row["sample_id"] in samples
                ]
                rows.append(
                    {
                        "target_id": target["target_id"],
                        "scenario": scenario,
                        "platform": platform,
                        "sample_count": len(samples),
                        "sample_ids": sorted(samples),
                        "cohort_hash": subset["hash"],
                        "total": aggregate_scores(
                            subset,
                            {row["sample_id"]: row["score"] for row in observations},
                        ),
                        "dimensions": {
                            key: aggregate_scores(
                                subset,
                                {
                                    row["sample_id"]: row["dimensions"][key]
                                    for row in observations
                                },
                            )
                            for key in WEIGHTS
                        },
                    }
                )
    return {
        **summary,
        "questions": list(questions.values()),
        "scope_scores": rows,
        "weight_policy": "condition-original-frozen-weights-on-reviewed-primary",
    }
