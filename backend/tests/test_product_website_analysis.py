from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app.api.auth import get_current_user
from app.api.product_website import _product_website_report_html
from app.main import app
from app.models.models import ProductWebsiteAnalysis
from app.adapters.base import PlatformResponse
from app.services import product_website_crawler
from app.services import product_website_ai_citations
from app.services.product_website_ai_citations import (
    build_product_website_citation_prompts,
    configured_product_website_citation_platforms,
    run_product_website_citation_check,
)
from app.services.product_website_crawler import (
    FirecrawlProductWebsiteCrawler,
    NativeProductWebsiteCrawler,
    get_product_website_crawler,
)
from app.services.product_website_analysis_service import (
    _parse_robots_access,
    _validate_llms_text,
    build_result_snapshot,
)


def test_build_result_snapshot_scores_product_page():
    html = """
    <html lang="zh-CN">
      <head>
        <title>Alpha Product - AI visibility platform</title>
        <meta name="description" content="Alpha Product helps teams improve AI search visibility">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <meta property="og:title" content="Alpha Product">
        <meta property="og:description" content="AI visibility platform for SaaS teams">
        <meta name="twitter:card" content="summary_large_image">
        <link rel="canonical" href="https://example.com/product">
        <script type="application/ld+json">{"@type":"Organization","name":"Alpha Product","url":"https://example.com","logo":"https://example.com/logo.png","sameAs":["https://baike.baidu.com/item/alpha","https://zhihu.com/org/alpha"]}</script>
        <script type="application/ld+json">{"@type":"WebSite","name":"Alpha Product","url":"https://example.com","publisher":{"@type":"Organization","name":"Alpha Product"}}</script>
        <script type="application/ld+json">{"@type":"SoftwareApplication","name":"Alpha Product","applicationCategory":"BusinessApplication","operatingSystem":"Web","offers":{"@type":"Offer","price":"0","priceCurrency":"CNY"}}</script>
        <script type="application/ld+json">{"@type":"FAQPage","mainEntity":[{"@type":"Question","name":"When should teams use Alpha Product?","acceptedAnswer":{"@type":"Answer","text":"Teams should use Alpha Product when they need measurable AI visibility."}}]}</script>
        <script type="application/ld+json">{"@type":"HowTo","name":"Improve AI search visibility","step":[{"@type":"HowToStep","text":"Run an audit."}]}</script>
      </head>
      <body>
        <nav><a href="/pricing">Pricing</a><a href="/cases">Customer cases</a></nav>
        <h1>Alpha Product</h1>
        <h2>What is Alpha Product?</h2>
        <p>Alpha Product is an AI search visibility platform for marketing teams. It monitors brand mentions, owned-domain citations, and competitor visibility across DeepSeek, Doubao, Qwen, Hunyuan, and Kimi.</p>
        <h2>How does Alpha Product improve AI search visibility?</h2>
        <p>Alpha Product analyzes 50+ prompts per audit and turns AI citation gaps into weekly optimization actions. In 2025 customer pilots, teams reduced missed citation opportunities by 42% within 30 days.</p>
        <p>It includes visibility monitoring, citation tracking, customer cases, API reports, privacy controls, and executive dashboards for enterprise SaaS teams.</p>
        <h2>Customer evidence and reports</h2>
        <p>Based on our work with 120 marketing teams, Alpha Product identifies source authority issues, schema gaps, and FAQ content opportunities before content teams publish updates.</p>
        <p>Our methodology combines proprietary research, customer case reviews, expert team analysis, data source citation checks, and an updated 2026 benchmark report for DeepSeek, Doubao, Qwen, Hunyuan, and Kimi.</p>
        <p>Alpha Product documents security, privacy, compliance, service SLA, author review, partner media coverage, white paper reports, and customer evidence so AI systems can verify the entity and source quality.</p>
        <ul><li>Customer cases</li><li>White paper reports</li><li>Security and compliance documentation</li></ul>
        <table><tr><th>Metric</th><th>Result</th></tr><tr><td>Citation gap reduction</td><td>42%</td></tr></table>
        <h2>FAQ: When should teams use Alpha Product?</h2>
        <p>Teams should use Alpha Product when they need measurable AI visibility, repeatable audits, competitor comparison, and actionable GEO recommendations.</p>
        <footer>
          sameAs official profiles:
          <a href="https://baike.baidu.com/item/alpha">Baidu Baike</a>
          <a href="https://zhihu.com/org/alpha">Zhihu</a>
          <a href="https://weibo.com/alpha">Weibo</a>
          <a href="https://www.bilibili.com/alpha">Bilibili</a>
          <a href="https://www.douyin.com/user/alpha">Douyin</a>
          ICP filing customer cases white paper security compliance contact email
          ICP 备案 客户案例 白皮书 安全合规 联系方式
        </footer>
      </body>
    </html>
    """

    snapshot = build_result_snapshot(
        html,
        "https://example.com/product",
        {
            "project": {
                "name": "Alpha",
                "product_name": "Alpha Product",
                "product_keywords": ["AI search", "visibility"],
            }
        },
    )

    assert snapshot["score"]["overall"] >= 70
    assert snapshot["score"]["dimensions"]["aiCitability"] >= 70
    assert snapshot["score"]["dimensions"]["eeat"] >= 70
    assert snapshot["geoAudit"]["eeatSignals"]["overall"] == snapshot["score"]["dimensions"]["eeat"]
    assert snapshot["geoAudit"]["eeatSignals"]["subScores"]["experience"] >= 60
    assert snapshot["geoAudit"]["eeatSignals"]["subScores"]["expertise"] >= 70
    assert snapshot["geoAudit"]["eeatSignals"]["subScores"]["trustworthiness"] >= 70
    assert snapshot["dimensionDiagnostics"]["eeat"]["evidence"]
    assert snapshot["score"]["dimensions"]["schemaStructuredData"] >= 80
    assert snapshot["geoAudit"]["schemaQuality"]["propertyScore"] >= 80
    assert snapshot["geoAudit"]["schemaQuality"]["sameAs"]["score"] >= 50
    assert snapshot["geoAudit"]["schemaQuality"]["propertyCompleteness"][0]["missing"] == []
    assert [model["id"] for model in snapshot["geoAudit"]["platformPresence"]["models"]] == [
        "deepseek",
        "doubao",
        "hunyuan",
        "qwen",
        "kimi",
    ]
    assert snapshot["geoAudit"]["platformPresence"]["score"] >= 40
    assert "wenxin" not in str(snapshot["geoAudit"]["platformPresence"]).lower()
    assert "文心" not in str(snapshot["recommendations"])
    assert snapshot["geoAudit"]["methodology"] == "geo-audit-cn"
    assert snapshot["geoAudit"]["businessType"]["type"] == "saas"
    assert snapshot["summary"]["headingsCount"] == 5
    assert snapshot["product"]["keywordCoverage"]["matched"] == 2


def test_product_website_report_html_includes_geo_child_sections():
    analysis = ProductWebsiteAnalysis(
        id=99,
        workspace_id="workspace-1",
        project_id="project-1",
        target_url="https://example.com/product",
        status="completed",
        stage="completed",
        score_overall=82,
        score_grade="A",
        result_snapshot={
            "score": {"overall": 82, "grade": "A", "dimensions": {"technicalGeo": 80}},
            "page": {"wordCount": 500, "schemaTypes": ["Organization"]},
            "geoAudit": {
                "technicalAudit": {
                    "robots": {"found": True, "domesticScore": 90, "internationalScore": 70, "blockedCritical": []},
                    "llms": {"found": True, "scores": {"overall": 88}, "itemCount": 4, "missingChecks": []},
                    "llmsFull": {"found": False, "scores": {"overall": 0}, "missingChecks": ["fileMissing"]},
                },
                "eeatSignals": {
                    "subScores": {"experience": 70, "expertise": 80, "authoritativeness": 75, "trustworthiness": 85},
                    "evidence": {"experience": ["客户案例信号：1"]},
                    "gaps": {"experience": ["补充更多客户实践。"]},
                },
                "schemaQuality": {
                    "propertyScore": 80,
                    "sameAs": {"score": 60, "domesticUrls": ["https://zhihu.com/org/alpha"]},
                    "propertyCompleteness": [
                        {"type": "Organization", "found": True, "score": 75, "missing": ["logo"]},
                    ],
                },
                "platformPresence": {
                    "score": 64,
                    "models": [
                        {"id": "deepseek", "label": "DeepSeek"},
                        {"id": "doubao", "label": "豆包"},
                        {"id": "hunyuan", "label": "腾讯元宝"},
                        {"id": "qwen", "label": "通义千问"},
                        {"id": "kimi", "label": "Kimi"},
                    ],
                    "platforms": [
                        {"id": "zhihu", "label": "知乎", "found": True, "models": ["deepseek"], "evidence": ["zhihu.com"]},
                    ],
                    "modelAdvice": [
                        {"model": "deepseek", "label": "DeepSeek", "score": 70, "missingPlatforms": ["百度百科/百家号"], "advice": "补齐百科。"},
                    ],
                },
            },
            "contentDetail": {},
            "recommendations": [],
        },
    )

    html = _product_website_report_html(analysis)

    assert "GEO 子流程审计" in html
    assert "技术 GEO：爬虫与 llms.txt" in html
    assert "内容 E-E-A-T 证据" in html
    assert "Schema 属性完整度与 sameAs" in html
    assert "当前智见已接入模型的平台覆盖" in html
    assert "DeepSeek、豆包、腾讯元宝、通义千问、Kimi" in html
    assert "文心" not in html


def test_parse_robots_access_detects_blocked_domestic_ai_crawlers():
    robots = """
    User-agent: Baiduspider
    Disallow: /

    User-agent: Bytespider
    Allow: /

    User-agent: *
    Allow: /
    Sitemap: https://example.com/sitemap.xml
    """

    result = _parse_robots_access("https://example.com/robots.txt", robots, 200)

    baidu = next(item for item in result["crawlers"] if item["id"] == "baiduspider")
    bytespider = next(item for item in result["crawlers"] if item["id"] == "bytespider")
    assert baidu["status"] == "blocked"
    assert bytespider["status"] == "allowed"
    assert result["blockedCritical"][0]["id"] == "baiduspider"
    assert result["sitemaps"] == ["https://example.com/sitemap.xml"]
    assert result["domesticScore"] < 100


def test_validate_llms_text_scores_structured_file():
    llms = """
# Alpha Product

> Alpha Product helps SaaS teams monitor AI visibility, citations, competitors, and weekly GEO recommendations.

## Documentation

- [Product overview](https://example.com/product): Describes the AI visibility workflow, citation monitoring, dashboards, and supported platforms.
- [Pricing](https://example.com/pricing): Explains plan tiers, enterprise controls, and monthly reporting options.
- [Customer cases](https://example.com/cases): Summarizes customer evidence, citation gap reductions, and measurable outcomes.
- [Security](https://example.com/security): Covers privacy controls, compliance practices, and data handling.
- [FAQ](https://example.com/faq): Answers common adoption, integration, and reporting questions.

## Key Facts

- Founded in 2025
- Industry: AI visibility analytics
- Customers: B2B SaaS marketing teams

## Contact

- Website: https://example.com
- Email: hello@example.com
    """

    result = _validate_llms_text("https://example.com/llms.txt", llms, 200)

    assert result["found"] is True
    assert result["checks"]["h1Title"] is True
    assert result["checks"]["pageItems"] is True
    assert result["scores"]["overall"] >= 80


def test_build_result_snapshot_includes_technical_access_audit():
    html = """
    <html lang="zh-CN">
      <head>
        <title>Alpha Product</title>
        <meta name="description" content="AI visibility platform">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <link rel="canonical" href="https://example.com/product">
      </head>
      <body>
        <h1>Alpha Product</h1>
        <h2>What is Alpha Product?</h2>
        <p>Alpha Product is an AI search visibility platform for SaaS teams with customer cases, research reports, security, compliance, and citation monitoring.</p>
        <p>Based on 50+ prompts and 2026 benchmark data, it helps teams improve DeepSeek, Doubao, Qwen, Hunyuan, and Kimi visibility.</p>
      </body>
    </html>
    """
    technical_audit = {
        "robots": {
            "found": True,
            "domesticScore": 80,
            "internationalScore": 100,
            "score": 86,
            "blockedCritical": [{"id": "baiduspider", "name": "Baiduspider"}],
            "sitemaps": [],
        },
        "llms": {"found": False, "statusCode": 404, "scores": {"overall": 0}},
        "llmsFull": {"found": False, "statusCode": 404, "scores": {"overall": 0}},
        "score": {"crawlerAccess": 86, "llmsReadiness": 0, "overall": 60},
    }

    snapshot = build_result_snapshot(
        html,
        "https://example.com/product",
        {"project": {"name": "Alpha", "product_name": "Alpha Product", "product_keywords": ["AI visibility"]}},
        {"technicalAudit": technical_audit},
    )

    assert snapshot["technicalAudit"]["score"]["overall"] == 60
    assert snapshot["geoAudit"]["technicalAudit"]["robots"]["blockedCritical"][0]["id"] == "baiduspider"
    recommendation_ids = {item["id"] for item in snapshot["recommendations"]}
    assert "geo_rec_ai_crawlers" in recommendation_ids
    assert "geo_rec_llms_txt" in recommendation_ids


def test_crawler_provider_defaults_to_native_without_firecrawl_key(monkeypatch):
    monkeypatch.setattr(product_website_crawler.settings, "product_website_crawler_provider", "firecrawl")
    monkeypatch.setattr(product_website_crawler.settings, "firecrawl_api_key", "")

    crawler = get_product_website_crawler()

    assert isinstance(crawler, NativeProductWebsiteCrawler)


@pytest.mark.asyncio
async def test_firecrawl_crawler_extracts_raw_html(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "success": True,
                "data": {
                    "rawHtml": "<html><head><title>Firecrawl page</title></head><body>Alpha Product</body></html>",
                    "metadata": {"sourceURL": "https://example.com/product"},
                },
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, json, headers):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(product_website_crawler.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(product_website_crawler.settings, "firecrawl_wait_for_ms", 1200)
    monkeypatch.setattr(product_website_crawler.settings, "firecrawl_max_age_ms", 3000)

    crawler = FirecrawlProductWebsiteCrawler(
        api_key="firecrawl-key",
        base_url="https://api.firecrawl.dev",
        timeout_seconds=10,
    )
    result = await crawler.fetch("https://example.com/product")

    assert result.method == "firecrawl_scrape"
    assert result.final_url == "https://example.com/product"
    assert "Firecrawl page" in result.html
    assert captured["url"] == "https://api.firecrawl.dev/v2/scrape"
    assert captured["headers"]["Authorization"] == "Bearer firecrawl-key"
    assert captured["json"]["formats"] == ["rawHtml", "html", "markdown"]
    assert captured["json"]["waitFor"] == 1200


def test_product_website_citation_platforms_are_domestic_only(monkeypatch):
    monkeypatch.setattr(
        product_website_ai_citations.settings,
        "product_website_ai_citation_platforms",
        "deepseek,openai,doubao,hunyuan,qwen,kimi,claude",
    )

    assert configured_product_website_citation_platforms() == [
        "deepseek",
        "doubao",
        "hunyuan",
        "qwen",
        "kimi",
    ]


def test_build_product_website_citation_prompts_use_product_context(monkeypatch):
    monkeypatch.setattr(product_website_ai_citations.settings, "product_website_ai_citation_prompt_limit", 2)

    prompts = build_product_website_citation_prompts(
        {
            "project": {
                "name": "Alpha",
                "industry": "营销科技",
                "product_name": "Alpha Product",
                "product_keywords": ["AI 可见性", "引用监测"],
            }
        },
        "https://example.com/product",
    )

    assert len(prompts) == 2
    assert "Alpha Product" in prompts[0]
    assert "AI 可见性" in prompts[0]


@pytest.mark.asyncio
async def test_run_product_website_citation_check_summarizes_platform_results(monkeypatch):
    class FakeAdapter:
        platform_name = "qwen"

        def set_runtime_context(self, context):
            self.context = context

        async def query(self, prompts):
            return [
                PlatformResponse(
                    platform="qwen",
                    prompt=prompts[0],
                    response_text="Alpha Product is documented at https://example.com/product",
                    citations=[{"url": "https://example.com/product", "title": "Alpha Product"}],
                    search_enabled=True,
                    latency_ms=10,
                )
            ]

    monkeypatch.setattr(product_website_ai_citations.settings, "product_website_ai_citation_enabled", True)
    monkeypatch.setattr(product_website_ai_citations.settings, "product_website_ai_citation_platforms", "qwen")
    monkeypatch.setattr(product_website_ai_citations.settings, "product_website_ai_citation_prompt_limit", 1)
    monkeypatch.setattr(product_website_ai_citations, "get_adapters", lambda platforms: [FakeAdapter()])

    result = await run_product_website_citation_check(
        {"project": {"name": "Alpha", "product_name": "Alpha Product"}},
        "https://example.com/product",
    )

    assert result.enabled is True
    assert result.platforms[0]["platform"] == "qwen"
    assert result.platforms[0]["mentionsProduct"] is True
    assert result.platforms[0]["ownDomainCitationCount"] == 1


@pytest.mark.asyncio
async def test_product_website_citation_check_uses_mobile_capture(monkeypatch):
    created_platforms = []
    queried_prompts = []
    runtime_contexts = []

    class ApiAdapter:
        platform_name = "qwen"

    class FakeMobileAdapter:
        def __init__(self, platform_name):
            self.platform_name = platform_name
            created_platforms.append(platform_name)

        def set_runtime_context(self, context):
            self.context = context
            runtime_contexts.append(context)

        async def query(self, prompts):
            queried_prompts.extend(prompts)
            return [
                PlatformResponse(
                    platform=self.platform_name,
                    prompt=prompts[0],
                    response_text="移动端回答",
                )
            ]

    monkeypatch.setattr(product_website_ai_citations.settings, "product_website_ai_citation_enabled", True)
    monkeypatch.setattr(product_website_ai_citations.settings, "product_website_ai_citation_platforms", "qwen")
    monkeypatch.setattr(product_website_ai_citations.settings, "product_website_ai_citation_prompt_limit", 1)
    monkeypatch.setattr(product_website_ai_citations.settings, "mobile_app_capture_enabled", True)
    monkeypatch.setattr(product_website_ai_citations.settings, "mobile_app_capture_platforms", "qwen")
    monkeypatch.setattr(product_website_ai_citations, "MobileGatewayAdapter", FakeMobileAdapter)
    monkeypatch.setattr(product_website_ai_citations, "get_adapters", lambda platforms: [ApiAdapter()])

    result = await run_product_website_citation_check(
        {"project": {"id": "project-1", "name": "Alpha"}},
        "https://example.com/product",
    )

    assert created_platforms == ["qwen"]
    assert queried_prompts
    assert runtime_contexts[0]["project_id"] == "project-1"
    assert runtime_contexts[0]["analysis_run_id"]
    assert result.platforms[0]["platform"] == "qwen"


@pytest.mark.asyncio
async def test_create_product_website_analysis(client: AsyncClient, db_session):
    project_id = "proj-product-site"

    async def override_current_user():
        return {"scope": "project", "pid": project_id}

    app.dependency_overrides[get_current_user] = override_current_user

    try:
        with patch(
            "app.api.product_website.run_product_website_analysis",
            new=AsyncMock(return_value=None),
        ):
            resp = await client.post(
                "/api/product-website/analyze",
                json={
                    "project_id": project_id,
                    "workspace_id": "workspace-1",
                    "target_url": "https://example.com/product",
                    "project": {
                        "name": "Alpha",
                        "product_name": "Alpha Product",
                        "product_keywords": ["AI search"],
                    },
                    "brands": [
                        {"id": "brand-1", "name": "Alpha", "aliases": [], "is_competitor": False}
                    ],
                    "options": {
                        "enable_ai_citation": True,
                        "crawler_provider": "firecrawl",
                    },
                },
            )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "queued"
    assert data["analysisId"] == data["id"]

    analysis = await db_session.get(ProductWebsiteAnalysis, data["id"])
    assert analysis is not None
    assert analysis.project_id == project_id
    assert analysis.input_snapshot["project"]["product_name"] == "Alpha Product"
    assert analysis.input_snapshot["options"] == {
        "enable_ai_citation": True,
        "crawler_provider": "firecrawl",
    }


@pytest.mark.asyncio
async def test_product_website_trends(client: AsyncClient, db_session):
    project_id = "proj-product-trends"

    async def override_current_user():
        return {"scope": "project", "pid": project_id}

    db_session.add(ProductWebsiteAnalysis(
        workspace_id="workspace-1",
        project_id=project_id,
        target_url="https://example.com",
        status="completed",
        stage="completed",
        score_overall=78.0,
        score_grade="B",
        result_snapshot={"score": {"dimensions": {"technical": 80}}},
        completed_at=datetime.now(timezone.utc),
        input_snapshot={},
    ))
    await db_session.commit()

    app.dependency_overrides[get_current_user] = override_current_user
    try:
        resp = await client.get(f"/api/product-website/projects/{project_id}/trends")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 200
    data = resp.json()
    assert data["projectId"] == project_id
    assert data["summary"]["currentScore"] == 78.0
    assert data["points"][0]["dimensions"] == {"technical": 80}
