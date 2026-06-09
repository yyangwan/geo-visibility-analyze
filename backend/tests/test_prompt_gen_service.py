import pytest

from app.services.prompt_gen_service import generate_prompts


@pytest.mark.asyncio
async def test_generate_prompts_prioritizes_product_context_without_brand_names():
    prompts = await generate_prompts(
        project_name="Project Alpha",
        project_url="https://example.com",
        product_name="Alpha V60",
        product_category="手冲咖啡壶",
        industry="咖啡器具",
        product_description="适合新手和日常家用，关注易清洁和稳定出品",
        product_url="https://product.example.com",
        product_keywords=["新手", "易清洁", "稳定出品"],
        brand_names=["Alpha", "V60"],
        count=6,
    )

    assert len(prompts) == 6
    assert all(prompt["text"] for prompt in prompts)
    assert all(prompt["category"] in {"recommend", "compare", "evaluate", "scenario"} for prompt in prompts)
    assert any("手冲咖啡壶" in prompt["text"] for prompt in prompts)
    assert any("新手" in prompt["text"] for prompt in prompts)
    assert any("易清洁" in prompt["text"] for prompt in prompts)
    assert all("Alpha" not in prompt["text"] for prompt in prompts)
    assert all("V60" not in prompt["text"] for prompt in prompts)


@pytest.mark.asyncio
async def test_generate_prompts_is_high_intent_and_brand_free_when_category_is_sparse():
    prompts = await generate_prompts(
        project_name="Project Beta",
        project_url="https://example.com",
        product_name="",
        product_category="SaaS",
        industry="软件服务",
        product_description="面向 AI 可见性分析与品牌监测",
        product_url="https://product.example.com",
        product_keywords=["可见性", "分析", "品牌监测"],
        brand_names=["Beta"],
        count=4,
    )

    assert len(prompts) == 4
    assert any("预算" in prompt["text"] for prompt in prompts)
    assert any("怎么选" in prompt["text"] for prompt in prompts)
    assert any("场景" in prompt["text"] or "使用" in prompt["text"] for prompt in prompts)
    assert all("Beta" not in prompt["text"] for prompt in prompts)


@pytest.mark.asyncio
async def test_generate_prompts_refines_generic_subject_with_keywords():
    prompts = await generate_prompts(
        project_name="Project Gamma",
        project_url="https://example.com",
        product_name="",
        product_category="SaaS",
        industry="软件服务",
        product_description="面向 AI 可见性分析与品牌监测",
        product_url="https://product.example.com",
        product_keywords=["可见性", "分析", "品牌监测"],
        brand_names=[],
        count=2,
    )

    assert len(prompts) == 2
    assert all("SaaS可见性分析工具" in prompt["text"] for prompt in prompts)
