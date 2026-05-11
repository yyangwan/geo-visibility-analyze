"""Prompt auto-generation service using LLM.

Generates relevant search prompts based on product category and industry
for auditing AI search visibility. When multiple brands exist, also
generates comparison prompts.
"""

import json
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """你是一个AI搜索优化专家。根据产品品类和行业，生成用户在AI助手中可能会问的搜索问题。

这些问题应该模拟真实用户的行为，涵盖：
- recommend: 推荐类（如"推荐一款好的百万医疗保险"）
- compare: 对比类（如"A和B的百万医疗哪个好"）
- evaluate: 评测类（如"XX保险的百万医疗怎么样"）
- scenario: 场景类（如"百万医疗适合什么人买"）

重要：问题必须围绕产品品类本身，不要围绕某个具体品牌。用户在搜索时用的是品类词，不是品牌名。

请以JSON数组格式返回，每条包含：
- text: 问题文本
- category: 分类（recommend/compare/evaluate/scenario）

只返回JSON数组，不要其他文字。"""

_USER_PROMPT_TEMPLATE = """产品品类：{product_category}
行业：{industry}
需要生成的问题数量：{count}

请生成{count}条用户在AI助手中可能问的与"{product_category}"相关的搜索问题。
问题要自然、多样，模拟真实用户的搜索习惯。
所有问题必须围绕"{product_category}"这个品类本身，不要提及任何具体品牌。
例如："{product_category}哪个好"、"{product_category}怎么选"、"{product_category}推荐"。"""

_COMPARE_PROMPT_TEMPLATE = """产品品类：{product_category}
行业：{industry}
品牌列表：{brand_list}
需要生成的对比问题数量：{count}

请生成{count}条用户在AI助手中可能问的"{product_category}"品牌对比类搜索问题。
每条问题都应该涉及品牌列表中的两个或多个品牌的对比。
问题要自然，模拟真实用户的搜索习惯。
例如："{brand_example}和{brand_example2}的{product_category}哪个好"。"""


async def _call_llm(system_prompt: str, user_prompt: str) -> list[dict]:
    """Call LLM and parse JSON response."""
    api_key, base_url, model = settings.get_llm_config()
    if not api_key:
        logger.warning("No LLM API key configured")
        return []

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.8,
                },
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]

            # Extract JSON
            text = text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()

            return json.loads(text)
    except Exception as e:
        logger.error(f"LLM call failed for prompt generation: {e}")
        return []


async def generate_prompts(
    product_category: str,
    industry: str,
    brand_names: list[str] | None = None,
    count: int = 10,
) -> list[dict]:
    """Generate category-driven prompts via LLM.

    When 2+ brands are provided, also generates comparison prompts.
    """
    effective_category = product_category or industry or "通用"

    # Generate category-driven prompts (no brand names)
    user_prompt = _USER_PROMPT_TEMPLATE.format(
        product_category=effective_category,
        industry=industry or "通用",
        count=count,
    )
    results = await _call_llm(_SYSTEM_PROMPT, user_prompt)

    # If 2+ brands, also generate comparison prompts (with brand names)
    if brand_names and len(brand_names) >= 2 and count > 0:
        brand_list_str = "、".join(brand_names)
        compare_count = min(3, count // 3 + 1)
        compare_prompt = _COMPARE_PROMPT_TEMPLATE.format(
            product_category=effective_category,
            industry=industry or "通用",
            brand_list=brand_list_str,
            brand_example=brand_names[0],
            brand_example2=brand_names[1],
            count=compare_count,
        )
        compare_results = await _call_llm(_SYSTEM_PROMPT, compare_prompt)
        results.extend(compare_results)

    return results
