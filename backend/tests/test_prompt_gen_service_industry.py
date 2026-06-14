import pytest

from app.services import prompt_gen_service
from app.services.prompt_gen_service import generate_prompts


@pytest.mark.asyncio
async def test_generate_prompts_strips_industry_jargon_from_output():
    prompts = await generate_prompts(
        project_name="Project Delta",
        project_url="https://example.com",
        product_name="",
        product_category="电商",
        industry="企业服务",
        product_description="面向日常使用者，关注易用性和稳定性。",
        product_url="https://product.example.com",
        product_keywords=["电商", "易用性", "稳定性"],
        brand_names=[],
        count=4,
    )

    assert len(prompts) == 4
    assert all("电商" not in prompt["text"] for prompt in prompts)
    assert all("企业服务" not in prompt["text"] for prompt in prompts)
    assert all("行业" not in prompt["text"] for prompt in prompts)


@pytest.mark.asyncio
async def test_generate_prompts_avoids_subject_demonstrative_phrase():
    prompts = await generate_prompts(
        project_name="",
        project_url="https://example.com",
        product_name="手冲咖啡壶",
        product_category="",
        industry="",
        product_description="日常使用者关注易用性、清洁维护和稳定性。",
        product_url="https://product.example.com",
        product_keywords=["手冲咖啡", "咖啡壶", "易用性", "清洁维护", "稳定性"],
        brand_names=[],
        count=6,
    )

    texts = [prompt["text"] for prompt in prompts]
    assert len(texts) == 6
    assert all("这款咖啡壶" not in text for text in texts)
    assert all("这款手冲咖啡壶" not in text for text in texts)
    assert any("手冲咖啡壶" in text for text in texts)


@pytest.mark.asyncio
async def test_generate_prompts_prefers_real_queries_then_llm_fill(monkeypatch):
    harvested_keywords = []

    async def fake_harvest(self, keyword, count=10, sources=None, apply_intent_filter=True):
        harvested_keywords.append(keyword)
        return [f"{keyword}怎么选"]

    async def fake_llm_fill(profile, needed, existing_texts, anchors):
        return [
            {
                "text": f"补充问题{i + 1}",
                "category": "evaluate",
                "quality_score": 0.9,
                "source": "llm",
            }
            for i in range(needed)
        ]

    def fail_render_prompts(*args, **kwargs):
        raise AssertionError("templates should not be used when real queries and LLM fill cover the count")

    monkeypatch.setattr(prompt_gen_service.QueryHarvester, "harvest", fake_harvest)
    monkeypatch.setattr(prompt_gen_service, "_call_prompt_llm_fill", fake_llm_fill)
    monkeypatch.setattr(prompt_gen_service, "render_prompts", fail_render_prompts)

    prompts = await generate_prompts(
        project_name="Project Omega",
        project_url="https://example.com",
        product_name="手冲咖啡壶",
        product_category="咖啡壶",
        industry="",
        product_description="日常使用者关注易用性、清洁维护和稳定性。",
        product_url="https://product.example.com",
        product_keywords=["手冲咖啡", "咖啡壶", "易用性", "清洁维护", "稳定性"],
        brand_names=[],
        count=4,
    )

    assert len(prompts) == 4
    assert all(item["source"] in {"harvested", "llm"} for item in prompts)
    assert harvested_keywords[0] == "手冲咖啡壶"
    assert "Project Omega" in harvested_keywords
    assert "咖啡壶" in harvested_keywords
