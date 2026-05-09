"""Prompt auto-generation service using LLM.

Generates relevant search prompts based on brand and industry
for auditing AI search visibility.
"""

import json
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """你是一个AI搜索优化专家。根据品牌名和行业，生成用户在AI助手中可能会问的搜索问题。

这些问题应该模拟真实用户的行为，涵盖：
- recommend: 推荐类（如"推荐一款好的XX"）
- compare: 对比类（如"XX和YY哪个好"）
- evaluate: 评测类（如"XX怎么样"）
- scenario: 场景类（如"XX适合什么人"）

请以JSON数组格式返回，每条包含：
- text: 问题文本
- category: 分类（recommend/compare/evaluate/scenario）

只返回JSON数组，不要其他文字。"""

_USER_PROMPT_TEMPLATE = """品牌名：{brand_name}
行业：{industry}
需要生成的问题数量：{count}

请生成{count}条用户在AI助手中可能问的与该品牌相关的搜索问题。
问题要自然、多样，模拟真实用户的搜索习惯。"""


async def generate_prompts(
    brand_name: str,
    industry: str,
    count: int = 10,
) -> list[dict]:
    """Generate prompts via LLM."""
    api_key, base_url, model = settings.get_llm_config()
    if not api_key:
        logger.warning("No LLM API key configured")
        return []

    user_prompt = _USER_PROMPT_TEMPLATE.format(
        brand_name=brand_name,
        industry=industry or "通用",
        count=count,
    )

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
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
