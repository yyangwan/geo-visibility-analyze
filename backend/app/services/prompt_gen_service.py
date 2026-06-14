"""Prompt auto-generation service - 5-Stage Pipeline

Follows GeniLink framework for high-quality prompt generation:
Stage 1: buildProductProfile - Extract structured profile from inputs
Stage 1.5: harvestRealQueries - Collect authentic user queries from search engines
Stage 2: generatePromptSpecs - Create intent-based prompt specifications
Stage 3: renderPrompts - Fill templates with profile data
Stage 4: lintPrompts - Quality gate validation

Goal: Produce prompts that are brand-free, intent-specific, and naturally
occurring in real user decision scenarios.

NEW: Real query harvesting from search engine autocomplete APIs provides
authentic user questions that real people actually search for.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

import httpx

from app.config import settings

from .query_harvester import harvest_queries, QueryHarvester

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Intent Library (9 Categories)
# ---------------------------------------------------------------------------

_INTENT_CATEGORIES = [
    "recommend",       # 选型/购买 - Product selection and purchase
    "compare",         # 对比/替代 - Comparison and alternatives
    "evaluate",        # 评测/评估 - Evaluation and assessment
    "scenario",        # 场景/使用 - Usage scenarios
    "problem_solution",# 问题解决 - Problem-to-solution mapping
    "alternative_finding", # 替代方案 - Alternative exploration
    "decision_help",   # 决策辅助 - Decision guidance
    "regret_avoidance",# 避坑/风险 - Risk and downside
    "performance_specs", # 参数/性能 - Performance and specs
]

# Base scenarios and personas
_SCENARIOS = [
    "新手入门",
    "日常家用",
    "办公室使用",
    "小户型/空间有限",
    "通勤/外出",
    "露营/旅行",
    "送礼",
    "预算敏感",
]

_PROBLEM_SCENARIOS = [
    "经常坏/故障多",
    "体验不稳定",
    "效果不明显",
    "后期维护麻烦",
    "兼容性问题",
    "安全隐患",
]

_PERSONAS = [
    "新手",
    "日常使用者",
    "小户型家庭",
    "通勤人群",
    "对体验要求高的用户",
    "预算有限的人",
    "专业用户",
    "团队管理员",
]

_BUDGETS = [
    "200元左右",
    "500元以内",
    "千元内",
    "中低预算",
    "性价比优先",
]

_FOCUS_FACTORS = [
    "易用性",
    "清洁维护",
    "稳定性",
    "便携性",
    "容量",
    "耐用性",
    "兼容性",
    "上手门槛",
    "实际体验",
    "性价比",
]

_DECISION_FACTORS = [
    "实际效果",
    "长期耐用性",
    "售后保障",
    "升级空间",
    "隐藏成本",
]

# Note: PERFORMANCE_METRICS are software-specific (响应速度, 并发性能).
# These are NOT used directly in templates to avoid semantic mismatches with physical products.
# Instead, performance_specs templates use {factor} from FOCUS_FACTORS which are universally applicable.
# Example: "手冲咖啡壶的响应速度" is nonsense, but "手冲咖啡壶的易用性" makes sense.
_PERFORMANCE_METRICS = [
    "响应速度",
    "处理能力",
    "并发性能",
    "资源占用",
    "稳定性指标",
    "扩展能力",
]

_ALT_VIEWS = [
    "更省心的版本",
    "更专业的版本",
    "更便携的版本",
    "更耐用的版本",
    "更容易清洁的版本",
]

_GENERIC_SUBJECTS = {
    "SaaS",
    "App",
    "APP",
    "软件服务",
    "工具",
    "产品",
    "平台",
    "系统",
    "服务",
}

# Question Templates per intent (自然口语化 - 模拟真实用户在AI助手中的提问方式)
# 设计原则:
# 1. 用"常用的/市面上的/普通的"代替"普通方案" (更符合日常口语)
# 2. 减少正式感,增加对话感 (用"吗/呢/啊"等语气词)
# 3. 避免过于结构化的句式 (不用"既想要X又想要Y")
# 4. 关注实际体验,而非抽象对比
_QUESTION_TEMPLATES = {
    "recommend": [
        "{persona}用{subject_term}，预算{budget}以内，有推荐的吗？",
        "{scenario}用{subject_term}，选的时候主要看什么？",
        "想买个{factor}好点的{subject_term}，怎么选更合适？",
        "新手买{subject_term}有什么需要注意的吗？",
    ],
    "compare": [
        "{scenario}用{subject_term}，{factor}和{alt_view}哪个更重要？",
        "{subject_term}的{factor_a}和{factor_b}，哪个更影响实际使用？",
        "{persona}觉得{subject_term}好用吗，还是用{alt_view}就够了？",
        "{factor_a}和{factor_b}都想要，{subject_term}该怎么选？",
    ],
    "evaluate": [
        "{subject_term}值得买吗？和{alt_view}比有什么优势？",
        "{scenario}用{subject_term}，实际体验能好多少？",
        "{subject_term}跟市面上常用的比，差别大吗？",
        "{subject_term}哪些卖点是真的有用，哪些是噱头？",
    ],
    "scenario": [
        "{scenario}想用{subject_term}，有什么需要注意的吗？",
        "{scenario}用{subject_term}怎么最顺手？",
        "{scenario}这种情况下，{subject_term}有什么特别的用法吗？",
        "{persona}平时用{subject_term}合适吗？",
    ],
    "problem_solution": [
        "{scenario}用{subject_term}经常出问题吗？怎么解决比较好？",
        "遇到{factor}问题，{subject_term}能帮上忙吗？",
        "{scenario}经常{description_hint}，{subject_term}能缓解吗？",
        "{scenario}用{subject_term}在{factor}方面体验如何？",
    ],
    "alternative_finding": [
        "除了常见的，{subject_term}还有什么好用的替代吗？",
        "{subject_term}跟常用的比，差别大吗？",
        "不想遇到{description_hint}，{subject_term}有更好的选择吗？",
        "{scenario}用{subject_term}，有什么小众但好用的替代吗？",
    ],
    "decision_help": [
        "{persona}选{subject_term}容易遇到哪些坑？",
        "{subject_term}在{factor_a}和{factor_b}之间，实际差别大吗？",
        "买{subject_term}时，{factor}值得多花钱吗？",
        "{scenario}用{subject_term}，主要看哪些参数？哪些可以不用管？",
    ],
    "regret_avoidance": [
        "买了{subject_term}最常见后悔的原因是什么？",
        "{scenario}用{subject_term}有什么常见槽点？",
        "{persona}买{subject_term}前经常忽略什么问题？",
        "{subject_term}在{factor}方面差评多，主要因为什么？",
    ],
    "performance_specs": [
        "{subject_term}实际用起来，{factor}表现怎么样？",
        "{scenario}用{subject_term}，{factor}能达到什么程度？",
        "{subject_term}的{factor_a}和{factor_b}，实际差别大吗？",
        "专业用户挑{subject_term}时，主要看哪些{factor}指标？",
    ],
}

# Quality lint thresholds
_GENERIC_PATTERNS = [
    r"^什么是",  # "什么是XXX"
    r"^XXX是什么",  # "XXX是什么"
    r"^XXX有哪些",  # "XXX有哪些" (too generic)
    r"^介绍一下XXX",  # "介绍一下XXX"
    r"^XXX的?简介",  # "XXX简介"
    r"^XXX是什么意思",  # "XXX是什么意思"
]

_MIN_PROMPT_LENGTH = 8
_MAX_PROMPT_LENGTH = 100

# ---------------------------------------------------------------------------
# Stage 1: Product Profile
# ---------------------------------------------------------------------------


@dataclass
class ProductProfile:
    """Structured product profile extracted from raw inputs."""
    subject: str  # Clean, brand-free subject (e.g., "智能客服系统")
    category: str | None  # Product category (e.g., "SaaS")
    industry: str | None  # Industry (e.g., "企业服务")
    scenarios: list[str]  # Extracted usage scenarios
    personas: list[str]  # Target user personas
    risk_points: list[str]  # Known pain points/risk areas
    comparison_objects: list[str]  # Common alternatives
    focus_factors: list[str]  # Key decision factors
    performance_metrics: list[str]  # Relevant performance metrics
    description_hint: str  # Processed description for template injection

    def to_context(self) -> dict:
        """Convert to context dict for template rendering."""
        all_factors = self.focus_factors + _DECISION_FACTORS
        all_metrics = self.performance_metrics + _PERFORMANCE_METRICS
        usable_factors = [factor for factor in all_factors if not _is_subject_like(factor, self.subject)]
        if len(usable_factors) < 2:
            usable_factors = all_factors
        subject_text = _sanitize_profile_text(self.subject, None)
        subject_term = _choose_subject_term(subject_text, self.category)

        return {
            "subject": subject_text,
            "subject_term": subject_term,
            # Note: industry removed from fallback to avoid leaking classification labels into prompts
            # Real users don't include "企业服务" or "电商" in their natural queries
            "category": _strip_industry_terms(self.category) if self.category else "相关产品",
            "scenario": self.scenarios[0] if self.scenarios else _SCENARIOS[0],
            "persona": self.personas[0] if self.personas else _PERSONAS[0],
            "budget": _BUDGETS[0],
            "factor": usable_factors[0] if usable_factors else all_factors[0],
            "factor_a": usable_factors[0] if usable_factors else all_factors[0],
            "factor_b": usable_factors[1] if len(usable_factors) > 1 else all_factors[1],
            "alt_view": _ALT_VIEWS[0],
            "perf_metric": self.performance_metrics[0] if self.performance_metrics else all_metrics[0],
            "perf_metric_a": self.performance_metrics[0] if self.performance_metrics else all_metrics[0],
            "perf_metric_b": self.performance_metrics[1] if len(self.performance_metrics) > 1 else all_metrics[1],
            "description_hint": self.description_hint,
        }


def _join_keywords(keywords: Iterable[str] | None) -> list[str]:
    values = []
    for keyword in keywords or []:
        if not keyword:
            continue
        item = str(keyword).strip()
        if item:
            values.append(item)
    return values


def _normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _build_brand_pattern(brand_names: list[str]) -> re.Pattern[str] | None:
    names = [re.escape(name.strip()) for name in brand_names if name and name.strip()]
    if not names:
        return None
    return re.compile("|".join(sorted(names, key=len, reverse=True)), re.IGNORECASE)


def _strip_brand_names(text: str, brand_pattern: re.Pattern[str] | None) -> str:
    if not brand_pattern:
        return _normalize_whitespace(text)
    stripped = brand_pattern.sub("", text)
    stripped = re.sub(r"[【】\[\]（）()<>《》]", "", stripped)
    stripped = re.sub(r"\s+", " ", stripped)
    stripped = re.sub(r"\s*[,，、/|]+\s*", " ", stripped)
    return stripped.strip(" -—:：;；,，")


_INDUSTRY_TERMS = (
    "电商",
    "企业服务",
    "B2B",
    "B2C",
    "ToB",
    "ToC",
    "行业",
    "领域",
    "垂直",
)


def _strip_industry_terms(text: str) -> str:
    stripped = text
    for term in _INDUSTRY_TERMS:
        stripped = stripped.replace(term, "")
    stripped = re.sub(r"\s+", " ", stripped)
    return stripped.strip()


def _sanitize_profile_text(text: str, brand_pattern: re.Pattern[str] | None) -> str:
    return _strip_industry_terms(_strip_brand_names(text, brand_pattern))


def _is_subject_like(text: str, subject: str) -> bool:
    normalized = _normalize_whitespace(text)
    subject_normalized = _normalize_whitespace(subject)
    if not normalized or not subject_normalized:
        return False

    compact = re.sub(r"[\W_]+", "", normalized)
    subject_compact = re.sub(r"[\W_]+", "", subject_normalized)
    if not compact or not subject_compact:
        return False
    if compact == subject_compact:
        return True
    if compact in subject_compact or subject_compact in compact:
        return True

    if len(compact) <= 6 and len(subject_compact) <= 12:
        overlap = len(set(compact) & set(subject_compact))
        if overlap >= max(2, min(len(set(compact)), len(set(subject_compact))) - 1):
            return True

    return False


def _is_generic_surface(text: str) -> bool:
    normalized = _normalize_whitespace(text)
    if not normalized:
        return True
    if normalized in _GENERIC_SUBJECTS:
        return True
    compact = re.sub(r"[\W_]+", "", normalized)
    return len(compact) <= 2


def _choose_subject_term(subject: str, category: str | None) -> str:
    subject_text = _normalize_whitespace(subject)
    category_text = _normalize_whitespace(_strip_industry_terms(category or ""))

    if subject_text and not _is_generic_surface(subject_text):
        return subject_text
    if category_text and not _is_generic_surface(category_text):
        return category_text
    if subject_text:
        return subject_text
    return "这个产品"


def _is_generic_subject(value: str) -> bool:
    normalized = _normalize_whitespace(value)
    if not normalized:
        return True
    if normalized in _GENERIC_SUBJECTS:
        return True
    compact = re.sub(r"[\W_]+", "", normalized)
    return len(compact) <= 4


def _choose_subject(
    product_name: str,
    product_category: str,
    industry: str,
    project_name: str,
    keywords: list[str],
    brand_pattern: re.Pattern[str] | None,
) -> str:
    """Pick a brand-free subject for the prompt."""
    candidates = [
        product_name,
        product_category,
        project_name,
    ]

    for raw in candidates:
        value = _sanitize_profile_text(raw or "", brand_pattern)
        if value:
            if any(term in value for term in _INDUSTRY_TERMS):
                continue
            return value

    if keywords:
        keyword_text = "、".join(
            _sanitize_profile_text(k, brand_pattern) for k in keywords[:3]
        )
        keyword_text = keyword_text.strip("、")
        keyword_text = _strip_industry_terms(keyword_text)
        if keyword_text:
            return keyword_text

    return "相关产品"


def _extract_scenarios(description: str, keywords: list[str]) -> list[str]:
    """Extract usage scenarios from description and keywords."""
    scenarios = []
    desc_lower = description.lower()

    # Check for scenario keywords in description
    scenario_keywords = ["场景", "用途", "使用", "适用", "环境"]
    for kw in scenario_keywords:
        if kw in desc_lower:
            scenarios.append(kw)

    # Add keywords that look like scenarios
    for kw in keywords[:3]:
        if any(s in kw for s in ["场景", "环境", "用途", "使用"]):
            scenarios.append(kw)

    return scenarios if scenarios else _SCENARIOS[:2]


def _extract_personas(description: str, keywords: list[str]) -> list[str]:
    """Extract target personas from description and keywords."""
    personas = []
    desc_lower = description.lower()

    # Check for persona indicators
    persona_indicators = ["用户", "人群", "对象", "团队", "企业", "个人"]
    for kw in persona_indicators:
        if kw in desc_lower:
            personas.append(kw)

    # Add keywords that look like personas
    for kw in keywords[:3]:
        if any(p in kw for p in ["用户", "人群", "团队", "企业"]):
            personas.append(kw)

    return personas if personas else _PERSONAS[:2]


def _extract_risk_points(description: str, keywords: list[str]) -> list[str]:
    """Extract risk points and pain areas from description."""
    risks = []
    desc_lower = description.lower()

    # Risk indicators
    risk_indicators = ["问题", "风险", "坑", "陷阱", "注意", "避免", "麻烦"]
    for kw in risk_indicators:
        if kw in desc_lower:
            risks.append(kw)

    # Add keywords that look like risks
    for kw in keywords[:3]:
        if any(r in kw for r in ["问题", "风险", "坑", "注意"]):
            risks.append(kw)

    return risks if risks else _PROBLEM_SCENARIOS[:2]


def _extract_focus_factors(description: str, keywords: list[str], subject: str) -> list[str]:
    """Extract decision factors from description and keywords.

    Avoid reusing words that are already part of the subject, because that
    tends to create awkward prompts like "subject 的 subject".
    """
    factors = []

    # Keywords are often decision factors, but skip ones that duplicate the subject.
    for kw in keywords[:5]:
        normalized = _normalize_whitespace(kw)
        if len(normalized) < 2 or len(normalized) > 6:
            continue
        if _is_subject_like(normalized, subject):
            continue
        factors.append(normalized)

    factors = [factor for factor in factors if not _is_subject_like(factor, subject)]

    # Add defaults until we have enough signal.
    for default_factor in _FOCUS_FACTORS:
        if len(factors) >= 3:
            break
        if default_factor not in factors:
            factors.append(default_factor)

    return factors[:5]


def _extract_performance_metrics(description: str, keywords: list[str]) -> list[str]:
    """Extract performance metrics from description and keywords."""
    metrics = []
    desc_lower = description.lower()

    # Performance indicators
    perf_indicators = ["性能", "速度", "响应", "处理", "并发", "效率", "能力"]
    for kw in perf_indicators:
        if kw in desc_lower:
            metrics.append(kw)

    # Add keywords that look like performance metrics
    for kw in keywords[:3]:
        if any(p in kw for p in ["性能", "速度", "响应", "效率"]):
            metrics.append(kw)

    return metrics if metrics else _PERFORMANCE_METRICS[:2]


def build_product_profile(
    product_name: str = "",
    product_category: str = "",
    industry: str = "",
    project_name: str = "",
    product_description: str = "",
    product_keywords: list[str] | None = None,
    brand_names: list[str] | None = None,
) -> ProductProfile:
    """Stage 1: Build structured product profile from raw inputs.

    This is where we extract meaningful signal (scenarios, personas, risks,
    factors) from the raw description and keywords, instead of just using
    them as template placeholders.
    """
    keywords = _join_keywords(product_keywords)
    brand_pattern = _build_brand_pattern(brand_names or [])
    sanitized_keywords = [
        keyword
        for keyword in (_sanitize_profile_text(k, brand_pattern) for k in keywords)
        if keyword
    ]
    sanitized_description = _sanitize_profile_text(product_description, brand_pattern)

    # Extract subject (brand-free)
    subject = _choose_subject(
        product_name=product_name,
        product_category=product_category,
        industry=industry,
        project_name=project_name,
        keywords=sanitized_keywords,
        brand_pattern=brand_pattern,
    )

    # Extract profile elements
    scenarios = _extract_scenarios(sanitized_description, sanitized_keywords)
    personas = _extract_personas(sanitized_description, sanitized_keywords)
    risk_points = _extract_risk_points(sanitized_description, sanitized_keywords)
    comparison_objects = [_sanitize_profile_text(product_category, brand_pattern)] if product_category else []
    focus_factors = _extract_focus_factors(sanitized_description, sanitized_keywords, subject)
    performance_metrics = _extract_performance_metrics(sanitized_description, sanitized_keywords)
    description_hint = sanitized_description

    return ProductProfile(
        subject=subject,
        category=product_category or None,
        industry=industry or None,
        scenarios=scenarios,
        personas=personas,
        risk_points=risk_points,
        comparison_objects=comparison_objects,
        focus_factors=focus_factors,
        performance_metrics=performance_metrics,
        description_hint=description_hint,
    )


# ---------------------------------------------------------------------------
# Stage 2: Generate Prompt Specifications
# ---------------------------------------------------------------------------


@dataclass
class PromptSpec:
    """Specification for a single prompt."""
    intent: str  # Intent category
    template_index: int  # Which template to use from the intent
    scenario_variant: str  # Which scenario to inject
    persona_variant: str  # Which persona to inject


def generate_prompt_specs(count: int, profile: ProductProfile) -> list[PromptSpec]:
    """Stage 2: Generate prompt specifications with intent distribution.

    Ensures coverage across all 9 intent categories.
    """
    specs = []

    # Cycle through all 9 intents
    for i in range(count):
        intent_idx = i % len(_INTENT_CATEGORIES)
        intent = _INTENT_CATEGORIES[intent_idx]

        # Choose scenario variant based on intent
        if intent == "problem_solution" and i % 2 == 0:
            scenario_variant = profile.scenarios[i % len(profile.scenarios)] if profile.scenarios else _PROBLEM_SCENARIOS[i % len(_PROBLEM_SCENARIOS)]
        else:
            scenario_variant = profile.scenarios[i % len(profile.scenarios)] if profile.scenarios else _SCENARIOS[i % len(_SCENARIOS)]

        persona_variant = profile.personas[i % len(profile.personas)] if profile.personas else _PERSONAS[i % len(_PERSONAS)]

        specs.append(PromptSpec(
            intent=intent,
            template_index=i,
            scenario_variant=scenario_variant,
            persona_variant=persona_variant,
        ))

    return specs


# ---------------------------------------------------------------------------
# Stage 3: Render Prompts
# ---------------------------------------------------------------------------


def render_prompt(spec: PromptSpec, profile: ProductProfile) -> str:
    """Stage 3: Render a single prompt from spec and profile."""
    templates = _QUESTION_TEMPLATES.get(spec.intent, _QUESTION_TEMPLATES["recommend"])
    template = templates[spec.template_index % len(templates)]
    context = profile.to_context()

    # Override context with spec variants
    context["scenario"] = spec.scenario_variant
    context["persona"] = spec.persona_variant

    return _normalize_whitespace(
        template.format(**context)
    )


def render_prompts(specs: list[PromptSpec], profile: ProductProfile) -> list[str]:
    """Stage 3 (batch): Render all prompts from specs."""
    return [render_prompt(spec, profile) for spec in specs]


# ---------------------------------------------------------------------------
# Stage 4: Lint Prompts
# ---------------------------------------------------------------------------


@dataclass
class PromptLintResult:
    """Result of linting a single prompt."""
    passed: bool
    issues: list[str]
    score: float  # 0-1 quality score


def _check_brand_leakage(prompt: str, brand_pattern: re.Pattern[str] | None) -> list[str]:
    """Check for brand name leakage."""
    if not brand_pattern:
        return []
    if brand_pattern.search(prompt):
        return ["品牌名称泄漏"]
    return []


def _check_generic(prompt: str) -> list[str]:
    """Check for overly generic patterns."""
    issues = []
    for pattern in _GENERIC_PATTERNS:
        if re.match(pattern, prompt):
            issues.append(f"泛化模式: {pattern}")
    return issues


def _check_length(prompt: str) -> list[str]:
    """Check prompt length constraints."""
    length = len(prompt)
    if length < _MIN_PROMPT_LENGTH:
        return [f"过短: {length}字符"]
    if length > _MAX_PROMPT_LENGTH:
        return [f"过长: {length}字符"]
    return []


def _calculate_quality_score(prompt: str, issues: list[str]) -> float:
    """Calculate overall quality score (0-1)."""
    base_score = 1.0

    # Deduct for each issue
    for issue in issues:
        if "品牌名称" in issue:
            base_score -= 0.5  # Severe
        elif "泛化" in issue:
            base_score -= 0.3  # Significant
        elif "过短" in issue or "过长" in issue:
            base_score -= 0.1  # Minor

    return max(0.0, base_score)


def lint_prompt(prompt: str, brand_pattern: re.Pattern[str] | None) -> PromptLintResult:
    """Stage 4: Lint a single prompt for quality."""
    issues = []

    issues.extend(_check_brand_leakage(prompt, brand_pattern))
    issues.extend(_check_generic(prompt))
    issues.extend(_check_length(prompt))

    score = _calculate_quality_score(prompt, issues)
    passed = score >= 0.6  # Minimum quality threshold

    return PromptLintResult(passed=passed, issues=issues, score=score)


def lint_prompts(prompts: list[str], brand_pattern: re.Pattern[str] | None) -> list[PromptLintResult]:
    """Stage 4 (batch): Lint all prompts."""
    return [lint_prompt(p, brand_pattern) for p in prompts]


def _dedupe_prompts(items: list[dict]) -> list[dict]:
    """Remove exact duplicate prompts."""
    seen: set[str] = set()
    deduped: list[dict] = []
    for item in items:
        text = item.get("text", "")
        if not text or text in seen:
            continue
        seen.add(text)
        deduped.append(item)
    return deduped


def _backfill_prompts(
    existing: list[dict],
    target_count: int,
    profile: ProductProfile,
    brand_pattern: re.Pattern[str] | None,
) -> list[dict]:
    """Backfill prompts if deduping reduced count."""
    backfilled = list(existing)
    backfill_index = target_count

    while len(backfilled) < target_count:
        # Generate with slight variations
        intent_idx = backfill_index % len(_INTENT_CATEGORIES)
        intent = _INTENT_CATEGORIES[intent_idx]

        spec = PromptSpec(
            intent=intent,
            template_index=backfill_index,
            scenario_variant=profile.scenarios[backfill_index % len(profile.scenarios)] if profile.scenarios else _SCENARIOS[0],
            persona_variant=profile.personas[backfill_index % len(profile.personas)] if profile.personas else _PERSONAS[0],
        )

        text = render_prompt(spec, profile)
        text = f"{text}（更侧重真实使用场景）"

        # Check if this text already exists
        if text not in {item["text"] for item in backfilled}:
            # Lint the backfilled prompt
            lint_result = lint_prompt(text, brand_pattern)
            if lint_result.passed:
                backfilled.append({
                    "text": text,
                    "category": intent,
                    "quality_score": lint_result.score,
                    "source": "backfill",
                })

        backfill_index += 1

        # Prevent infinite loop
        if backfill_index > target_count * 3:
            break

    return backfilled[:target_count]


def _build_harvest_seeds(
    profile: ProductProfile,
    *,
    project_name: str = "",
    product_category: str = "",
    product_keywords: list[str] | None = None,
) -> list[str]:
    seeds: list[str] = []
    seen: set[str] = set()

    candidates = [
        profile.subject,
        project_name,
        product_category,
        *(_join_keywords(product_keywords) if product_keywords else []),
    ]

    for candidate in candidates:
        seed = _sanitize_profile_text(candidate or "", None)
        seed = _normalize_whitespace(seed)
        if len(seed) < 2:
            continue
        if seed in seen:
            continue
        seen.add(seed)
        seeds.append(seed)

    return seeds


def _build_prompt_anchors(
    profile: ProductProfile,
    *,
    product_keywords: list[str] | None = None,
) -> list[str]:
    anchors: list[str] = []
    seen: set[str] = set()

    candidates = [
        profile.subject,
        profile.category or "",
        *(_join_keywords(product_keywords) if product_keywords else []),
    ]

    for candidate in candidates:
        anchor = _sanitize_profile_text(candidate or "", None)
        anchor = _normalize_whitespace(anchor)
        if len(anchor) < 2:
            continue
        if _is_generic_subject(anchor):
            continue
        if anchor in seen:
            continue
        seen.add(anchor)
        anchors.append(anchor)

    return anchors


def _prompt_has_anchor(prompt: str, anchors: list[str]) -> bool:
    normalized = _normalize_whitespace(prompt)
    if not normalized:
        return False
    return any(anchor and anchor in normalized for anchor in anchors)


async def _call_prompt_llm_fill(
    profile: ProductProfile,
    needed: int,
    existing_texts: set[str],
    anchors: list[str],
) -> list[dict]:
    api_key, base_url, model = settings.get_llm_config()
    if not api_key or needed <= 0 or not anchors:
        return []

    subject = _sanitize_profile_text(profile.subject, None)
    subject_term = _choose_subject_term(subject, profile.category)
    payload = {
        "subject": subject,
        "subject_term": subject_term,
        "category": _strip_industry_terms(profile.category) if profile.category else "相关产品",
        "scenarios": profile.scenarios[:3],
        "personas": profile.personas[:3],
        "focus_factors": profile.focus_factors[:5],
        "risk_points": profile.risk_points[:3],
        "description_hint": profile.description_hint[:80],
        "need_count": needed,
        "existing_prompts": list(existing_texts)[:10],
    }

    system_prompt = (
        "你是一个中文问题改写器。"
        "你的任务是生成真实用户会问的搜索/提问句，而不是营销文案。"
        "要求："
        "1. 只输出 JSON 数组，每个元素都是字符串。"
        "2. 句子要自然、口语化、像真实用户。"
        "3. 不要使用“这款”“该款”“这类”这类指代词。"
        "4. 不要包含品牌名、行业名、分类名或生硬的拼接词。"
        "5. 不要重复已有问题，尽量覆盖不同意图。"
        "6. 不要输出解释、前后缀、代码块或多余文本。"
    )
    user_prompt = json.dumps(payload, ensure_ascii=False)

    try:
        async with httpx.AsyncClient(timeout=45, proxy=None) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.4,
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        logger.warning(f"prompt_llm_fill_failed: {exc}")
        return []

    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1] if "\n" in content else content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        logger.warning(f"prompt_llm_fill_invalid_json: {content[:200]}")
        return []

    if not isinstance(parsed, list):
        return []

    results: list[dict] = []
    brand_pattern = _build_brand_pattern([])
    for item in parsed:
        text = ""
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            text = str(item.get("text", "")).strip()

        text = _normalize_whitespace(text)
        if not text or text in existing_texts:
            continue
        if not _prompt_has_anchor(text, anchors):
            continue

        lint_result = lint_prompt(text, brand_pattern)
        if not lint_result.passed:
            continue

        results.append({
            "text": text,
            "category": _infer_intent_from_query(text),
            "quality_score": min(0.98, max(0.7, lint_result.score)),
            "source": "llm",
        })
        existing_texts.add(text)
        if len(results) >= needed:
            break

    return results



# ---------------------------------------------------------------------------
# Main Pipeline
# ---------------------------------------------------------------------------


async def generate_prompts(
    project_name: str = "",
    project_url: str = "",
    product_name: str = "",
    product_category: str = "",
    industry: str = "",
    product_description: str = "",
    product_url: str = "",
    product_keywords: list[str] | None = None,
    brand_names: list[str] | None = None,
    count: int = 10,
    use_real_queries: bool = True,
    harvest_sources: list[str] | None = None,
) -> list[dict]:
    """Generate high-intent, brand-free prompts using 5-stage pipeline.

    Stage 1: buildProductProfile - Extract structured profile
    Stage 1.5: harvestRealQueries - Collect authentic user queries (NEW)
    Stage 2: generatePromptSpecs - Create intent-based prompt specifications
    Stage 3: renderPrompts - Fill templates with profile data
    Stage 4: lintPrompts - Quality gate validation

    Args:
        use_real_queries: If True, harvest real queries from search engines
        harvest_sources: Sources to query ['baidu', 'sogou', 'bing'],
                         defaults to ['baidu', 'sogou']

    Returns list of dicts with: text, category, quality_score
    """
    if count <= 0:
        return []

    # Stage 1: Build product profile
    profile = build_product_profile(
        product_name=product_name,
        product_category=product_category,
        industry=industry,
        project_name=project_name,
        product_description=product_description,
        product_keywords=product_keywords,
        brand_names=brand_names,
    )

    brand_pattern = _build_brand_pattern(brand_names or [])
    prompt_anchors = _build_prompt_anchors(
        profile,
        product_keywords=product_keywords,
    )

    # Stage 1.5: Harvest real user queries (NEW)
    real_queries = []
    real_query_texts = set()  # For deduping

    if use_real_queries:
        harvest_seeds = _build_harvest_seeds(
            profile,
            project_name=project_name,
            product_category=product_category,
            product_keywords=product_keywords,
        )

        if harvest_seeds:
            try:
                harvester = QueryHarvester(timeout=5.0)
                sources = harvest_sources or ["baidu", "sogou", "bing"]

                for harvest_keyword in harvest_seeds:
                    if len(real_queries) >= count:
                        break

                    try:
                        harvested = await harvester.harvest(
                            harvest_keyword,
                            count=max(4, count),
                            sources=sources,
                        )

                        for query_text in harvested:
                            if brand_pattern and brand_pattern.search(query_text):
                                logger.warning(f"Brand leakage in harvested query: {query_text}")
                                continue
                            if query_text in real_query_texts:
                                continue

                            real_queries.append({
                                "text": query_text,
                                "category": _infer_intent_from_query(query_text),
                                "quality_score": 0.95,
                                "source": "harvested",
                            })
                            real_query_texts.add(query_text)

                            if len(real_queries) >= count:
                                break
                    except Exception as seed_error:
                        logger.warning(
                            f"Query harvest failed for seed '{harvest_keyword}': {seed_error}"
                        )

                logger.info(
                    f"Harvested {len(real_queries)} real queries for seeds: {', '.join(harvest_seeds[:5])}"
                )

            except Exception as e:
                logger.warning(f"Query harvest failed: {e}")

    # Calculate how many more prompts we need
    remaining = max(0, count - len(real_queries))

    # Stage 1.6: Fill the gap with LLM-generated natural questions when available.
    llm_prompts = []
    if remaining > 0:
        llm_prompts = await _call_prompt_llm_fill(
            profile,
            remaining,
            real_query_texts,
            prompt_anchors,
        )

    remaining = max(0, count - len(real_queries) - len(llm_prompts))

    # Stage 2 & 3 & 4: Template-based prompts as the last fallback.
    template_prompts = []
    if remaining > 0:
        specs = generate_prompt_specs(remaining, profile)
        prompts_text = render_prompts(specs, profile)
        lint_results = lint_prompts(prompts_text, brand_pattern)

        for spec, text, lint_result in zip(specs, prompts_text, lint_results):
            if lint_result.passed and text not in real_query_texts:
                template_prompts.append({
                    "text": text,
                    "category": spec.intent,
                    "quality_score": lint_result.score,
                    "source": "template",
                })
            elif not lint_result.passed:
                logger.warning(
                    f"prompt_lint_failed intent={spec.intent} issues={lint_result.issues} score={lint_result.score}"
                )

    # Combine: prioritized real queries, then LLM fills, then templates.
    all_prompts = real_queries + llm_prompts + template_prompts

    # Final dedup
    final_prompts = _dedupe_prompts(all_prompts)

    # Backfill if we still don't have enough (network failures, etc)
    if len(final_prompts) < count:
        final_prompts = _backfill_prompts(
            final_prompts, count, profile, _build_brand_pattern(brand_names or [])
        )

    return final_prompts[:count]


def _infer_intent_from_query(query: str) -> str:
    """Infer intent category from a harvested query text.

    Real user queries don't come with intent labels, so we infer them.
    """
    query_lower = query.lower()

    # Pattern matching for intent inference
    intent_patterns = {
        "recommend": [r"推荐", r"怎么选", r"哪个好", r"有什么好", r"买哪个"],
        "compare": [r"和.*比", r"区别", r"对比", r"差异", r"哪个重要"],
        "evaluate": [r"值得.*吗", r"好吗", r"怎么样", r"评价", r"如何"],
        "scenario": [r"用.*场景", r"适合.*吗", r"怎么用", r"在.*情况下"],
        "problem_solution": [r"怎么解决", r"问题", r"故障", r"修复"],
        "alternative_finding": [r"替代", r"还有.*吗", r"其他.*选择"],
        "decision_help": [r"怎么选", r"选.*注意", r"看.*参数"],
        "regret_avoidance": [r"后悔", r"坑", r"槽点", r"差评"],
        "performance_specs": [r"速度", r"性能", r"响应", r"能力"],
    }

    for intent, patterns in intent_patterns.items():
        for pattern in patterns:
            if re.search(pattern, query):
                return intent

    # Default to evaluate if no pattern matches
    return "evaluate"

    # Backfill if we lost prompts to deduping or linting
    if len(prompts) < count:
        prompts = _backfill_prompts(prompts, count, profile, brand_pattern)

    return prompts[:count]

