"""Product website analysis service.

The engine keeps analysis deterministic while allowing the crawl provider to be
configured: native fetch by default, Firecrawl when explicitly enabled.
"""

from __future__ import annotations

import json
import re
import time
from urllib.robotparser import RobotFileParser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.models import (
    ProductWebsiteAnalysis,
    ProductWebsiteCrawlLog,
    ProductWebsiteEventLog,
    ProductWebsiteStageRun,
)
from app.config import settings
from app.services.product_website_ai_citations import (
    configured_product_website_citation_platforms,
    run_product_website_citation_check,
)
from app.services.product_website_crawler import get_product_website_crawler


@dataclass
class ExtractedPage:
    title: str | None = None
    description: str | None = None
    canonical: str | None = None
    lang: str | None = None
    charset: str | None = None
    viewport: str | None = None
    open_graph: dict[str, str] = field(default_factory=dict)
    twitter_card: dict[str, str] = field(default_factory=dict)
    robots_meta: str | None = None
    headings: list[dict[str, Any]] = field(default_factory=list)
    paragraphs: list[str] = field(default_factory=list)
    links: list[dict[str, str]] = field(default_factory=list)
    schema_types: list[str] = field(default_factory=list)
    schema_nodes: list[dict[str, Any]] = field(default_factory=list)
    image_count: int = 0
    images_missing_alt: int = 0
    list_count: int = 0
    table_count: int = 0
    nav_text: str = ""
    footer_text: str = ""
    body_text: str = ""


class ProductWebsiteHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.page = ExtractedPage()
        self._tag_stack: list[str] = []
        self._current_heading: dict[str, Any] | None = None
        self._current_paragraph: list[str] | None = None
        self._current_link: dict[str, str] | None = None
        self._current_json_ld: list[str] | None = None
        self._title_parts: list[str] = []
        self._body_parts: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key.lower(): value or "" for key, value in attrs}
        self._tag_stack.append(tag)

        if tag == "html" and attr_map.get("lang"):
            self.page.lang = attr_map["lang"]
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            self._handle_meta(attr_map)
        elif tag == "link" and attr_map.get("rel", "").lower() == "canonical":
            self.page.canonical = attr_map.get("href") or None
        elif re.fullmatch(r"h[1-6]", tag):
            self._current_heading = {
                "level": int(tag[1]),
                "text": "",
                "id": attr_map.get("id") or None,
            }
        elif tag == "p":
            self._current_paragraph = []
        elif tag == "a":
            self._current_link = {"text": "", "href": attr_map.get("href", "")}
        elif tag == "img":
            self.page.image_count += 1
            if not attr_map.get("alt", "").strip():
                self.page.images_missing_alt += 1
        elif tag in {"ul", "ol"}:
            self.page.list_count += 1
        elif tag == "table":
            self.page.table_count += 1
        elif tag == "script" and attr_map.get("type", "").lower() == "application/ld+json":
            self.page.schema_types.append("JSON-LD")
            self._current_json_ld = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            title = " ".join("".join(self._title_parts).split())
            self.page.title = title or self.page.title
        elif re.fullmatch(r"h[1-6]", tag) and self._current_heading:
            self._current_heading["text"] = " ".join(self._current_heading["text"].split())
            if self._current_heading["text"]:
                self.page.headings.append(self._current_heading)
            self._current_heading = None
        elif tag == "p" and self._current_paragraph is not None:
            text = " ".join("".join(self._current_paragraph).split())
            if text:
                self.page.paragraphs.append(text)
            self._current_paragraph = None
        elif tag == "a" and self._current_link is not None:
            self._current_link["text"] = " ".join(self._current_link["text"].split())
            if self._current_link.get("href"):
                self.page.links.append(self._current_link)
            self._current_link = None
        elif tag == "script" and self._current_json_ld is not None:
            json_text = "".join(self._current_json_ld)
            try:
                nodes = _json_ld_nodes(json.loads(json_text))
                self.page.schema_nodes.extend(nodes)
                for node in nodes:
                    for schema_type in _schema_type_names(node.get("@type")):
                        if schema_type and schema_type not in self.page.schema_types:
                            self.page.schema_types.append(schema_type)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            for match in re.finditer(r'"@type"\s*:\s*"([^"]+)"', json_text):
                schema_type = match.group(1)
                if schema_type and schema_type not in self.page.schema_types:
                    self.page.schema_types.append(schema_type)
            self._current_json_ld = None

        if self._tag_stack:
            self._tag_stack.pop()

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self._title_parts.append(text)
        if self._current_heading is not None:
            self._current_heading["text"] += f" {text}"
        if self._current_paragraph is not None:
            self._current_paragraph.append(text)
        if self._current_link is not None:
            self._current_link["text"] += f" {text}"
        if self._current_json_ld is not None:
            self._current_json_ld.append(data)
        if self._tag_stack and self._tag_stack[-1] not in {"script", "style"}:
            self._body_parts.append(text)
            if "nav" in self._tag_stack:
                self.page.nav_text = " ".join((self.page.nav_text, text)).strip()
            if "footer" in self._tag_stack:
                self.page.footer_text = " ".join((self.page.footer_text, text)).strip()

    def close(self) -> None:
        super().close()
        self.page.body_text = " ".join(" ".join(self._body_parts).split())

    def _handle_meta(self, attrs: dict[str, str]) -> None:
        name = attrs.get("name", "").lower()
        prop = attrs.get("property", "").lower()
        content = attrs.get("content", "")
        if not content:
            return
        if name == "description":
            self.page.description = content
        elif name == "robots":
            self.page.robots_meta = content
        elif name == "viewport":
            self.page.viewport = content
        elif attrs.get("charset"):
            self.page.charset = attrs["charset"]
        elif prop.startswith("og:"):
            self.page.open_graph[prop] = content
        elif name.startswith("twitter:"):
            self.page.twitter_card[name] = content


def extract_page(html: str) -> ExtractedPage:
    parser = ProductWebsiteHTMLParser()
    parser.feed(html)
    parser.close()
    return parser.page


def _word_count(text: str) -> int:
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin_words = len(re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*", text))
    return chinese_chars + latin_words


def _grade(score: float) -> str:
    if score >= 90:
        return "A+"
    if score >= 80:
        return "A"
    if score >= 70:
        return "B"
    if score >= 60:
        return "C"
    if score >= 50:
        return "D"
    return "F"


def _score_page(page: ExtractedPage, project: dict) -> dict:
    body = page.body_text.lower()
    title = (page.title or "").lower()
    description = (page.description or "").lower()
    product_name = (project.get("product_name") or project.get("name") or "").strip().lower()
    keywords = [kw.lower() for kw in project.get("product_keywords", []) if isinstance(kw, str)]
    word_count = _word_count(page.body_text)
    h1_count = sum(1 for heading in page.headings if heading["level"] == 1)

    structure = 25
    if h1_count == 1:
        structure += 25
    if len(page.headings) >= 3:
        structure += 25
    if page.schema_types:
        structure += 25

    semantic = 35
    if product_name and product_name in body:
        semantic += 25
    if keywords:
        semantic += min(30, round(sum(1 for kw in keywords if kw in body) / len(keywords) * 30))
    if page.description:
        semantic += 10

    density = 20
    if word_count >= 300:
        density += 30
    if word_count >= 800:
        density += 25
    if len(page.paragraphs) >= 5:
        density += 25

    authority = 35
    if any(token in body for token in ["案例", "客户", "认证", "报告", "数据", "case", "customer"]):
        authority += 30
    if len([link for link in page.links if link.get("href", "").startswith("http")]) >= 3:
        authority += 20
    if word_count >= 800:
        authority += 15

    technical = 0
    technical += 18 if page.title else 0
    technical += 18 if page.description else 0
    technical += 14 if page.canonical else 0
    technical += 14 if page.lang else 0
    technical += 14 if page.viewport else 0
    technical += 12 if page.open_graph else 0
    technical += 10 if page.schema_types else 0

    readability = 40
    if len(page.paragraphs) >= 4:
        readability += 25
    if page.headings:
        readability += 20
    if word_count and page.paragraphs and word_count / max(len(page.paragraphs), 1) < 220:
        readability += 15

    product_clarity = 20
    if product_name and product_name in title:
        product_clarity += 25
    if product_name and product_name in description:
        product_clarity += 20
    if product_name and product_name in body:
        product_clarity += 25
    if keywords:
        product_clarity += min(30, round(sum(1 for kw in keywords if kw in body) / len(keywords) * 30))

    dimensions = {
        "structure": min(structure, 100),
        "semantic": min(semantic, 100),
        "density": min(density, 100),
        "authority": min(authority, 100),
        "technical": min(technical, 100),
        "readability": min(readability, 100),
        "productClarity": min(product_clarity, 100),
    }
    overall = round(
        dimensions["structure"] * 0.15
        + dimensions["semantic"] * 0.20
        + dimensions["density"] * 0.15
        + dimensions["authority"] * 0.15
        + dimensions["technical"] * 0.15
        + dimensions["readability"] * 0.10
        + dimensions["productClarity"] * 0.10,
        1,
    )
    return {"overall": overall, "grade": _grade(overall), "dimensions": dimensions}


def _recommendations(page: ExtractedPage, score: dict, project: dict) -> list[dict]:
    recs: list[dict] = []
    product_name = project.get("product_name") or project.get("name") or ""
    if not page.title or (product_name and product_name.lower() not in (page.title or "").lower()):
        recs.append({
            "id": "rec_title_product",
            "dimension": "productClarity",
            "priority": "high",
            "impact": "high",
            "effort": "small",
            "title": "在 title 中明确产品名称和用途",
            "problem": "页面 title 没有清楚承载产品识别信号。",
            "evidence": [page.title or "title 缺失"],
            "actions": ["将产品名、核心用途和行业关键词写入 title。"],
            "expectedLift": 6,
        })
    if not page.description:
        recs.append({
            "id": "rec_meta_description",
            "dimension": "technical",
            "priority": "high",
            "impact": "medium",
            "effort": "small",
            "title": "补充 meta description",
            "problem": "页面缺少描述，AI 和搜索系统缺少摘要信号。",
            "evidence": ["description 缺失"],
            "actions": ["用 80 到 160 字说明产品对象、场景和核心价值。"],
            "expectedLift": 5,
        })
    if not page.schema_types:
        recs.append({
            "id": "rec_schema",
            "dimension": "technical",
            "priority": "medium",
            "impact": "medium",
            "effort": "medium",
            "title": "增加 Product 或 SoftwareApplication Schema",
            "problem": "页面缺少结构化数据，AI 难以稳定识别产品实体。",
            "evidence": ["未检测到 JSON-LD Schema"],
            "actions": ["增加 Product、SoftwareApplication、Organization 或 FAQPage JSON-LD。"],
            "expectedLift": 5,
        })
    if score["dimensions"]["density"] < 60:
        recs.append({
            "id": "rec_content_depth",
            "dimension": "density",
            "priority": "medium",
            "impact": "high",
            "effort": "medium",
            "title": "补充产品场景、功能和 FAQ",
            "problem": "页面正文信息密度不足。",
            "evidence": [f"正文词数约 {_word_count(page.body_text)}"],
            "actions": ["增加目标用户、使用场景、关键功能、案例和 FAQ。"],
            "expectedLift": 8,
        })
    return recs


DIMENSION_LABELS = {
    "structure": "页面结构",
    "semantic": "语义覆盖",
    "density": "内容深度",
    "authority": "权威信号",
    "technical": "技术可读性",
    "readability": "阅读体验",
    "productClarity": "产品清晰度",
    "aiCitationReadiness": "AI 引用准备度",
}


def _score_status(value: int | float | None) -> str:
    if value is None:
        return "unknown"
    if value >= 80:
        return "strong"
    if value >= 60:
        return "watch"
    return "weak"


def _keyword_coverage(page: ExtractedPage, project: dict) -> dict:
    keywords = [kw for kw in project.get("product_keywords", []) if isinstance(kw, str) and kw.strip()]
    body = page.body_text.lower()
    matched = [kw for kw in keywords if kw.lower() in body]
    return {
        "total": len(keywords),
        "matched": len(matched),
        "missing": [kw for kw in keywords if kw not in matched],
        "matchedKeywords": matched,
        "coverageRate": round(len(matched) / len(keywords) * 100, 1) if keywords else None,
    }


def _link_counts(page: ExtractedPage) -> tuple[int, int]:
    internal = len([link for link in page.links if not link.get("href", "").startswith("http")])
    external = len([link for link in page.links if link.get("href", "").startswith("http")])
    return internal, external


AI_CRAWLER_GROUPS = [
    {"id": "baiduspider", "name": "Baiduspider", "operator": "百度", "market": "domestic", "weight": 20, "aliases": ["baiduspider", "baiduspider-image"]},
    {"id": "bytespider", "name": "Bytespider", "operator": "字节跳动", "market": "domestic", "weight": 20, "aliases": ["bytespider", "toutiaospider"]},
    {"id": "deepseek", "name": "DeepSeekBot", "operator": "DeepSeek", "market": "domestic", "weight": 15, "aliases": ["deepseek", "deepseekbot"]},
    {"id": "tencent", "name": "TencentCloudBot", "operator": "腾讯", "market": "domestic", "weight": 15, "aliases": ["tencentcloudbot", "sosoospider"]},
    {"id": "kimi", "name": "Moonshot/Kimi", "operator": "月之暗面", "market": "domestic", "weight": 10, "aliases": ["moonshot", "kimi"]},
    {"id": "search360", "name": "Yisouspider/360Spider", "operator": "360 搜索", "market": "domestic", "weight": 7, "aliases": ["yisouspider", "360spider"]},
    {"id": "sogou", "name": "Sogou web spider", "operator": "搜狗", "market": "domestic", "weight": 6, "aliases": ["sogou web spider", "sogou push spider"]},
    {"id": "alibaba", "name": "AlibabaCloud/Robozilla", "operator": "阿里巴巴", "market": "domestic", "weight": 7, "aliases": ["alibabacloud-cn-hangzhou", "robozilla"]},
    {"id": "gptbot", "name": "GPTBot", "operator": "OpenAI", "market": "international", "weight": 20, "aliases": ["gptbot"]},
    {"id": "oai-searchbot", "name": "OAI-SearchBot", "operator": "OpenAI", "market": "international", "weight": 25, "aliases": ["oai-searchbot", "chatgpt-user"]},
    {"id": "claudebot", "name": "ClaudeBot", "operator": "Anthropic", "market": "international", "weight": 20, "aliases": ["claudebot"]},
    {"id": "perplexitybot", "name": "PerplexityBot", "operator": "Perplexity", "market": "international", "weight": 20, "aliases": ["perplexitybot"]},
    {"id": "google-extended", "name": "Google-Extended", "operator": "Google", "market": "international", "weight": 15, "aliases": ["google-extended", "googleother"]},
]


def _site_root(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}"


def _robots_user_agents(robots_text: str) -> set[str]:
    agents: set[str] = set()
    for line in robots_text.splitlines():
        match = re.match(r"\s*user-agent\s*:\s*(.+?)\s*$", line, flags=re.I)
        if match:
            agents.add(match.group(1).strip().lower())
    return agents


def _robots_sitemaps(robots_text: str) -> list[str]:
    sitemaps: list[str] = []
    for line in robots_text.splitlines():
        match = re.match(r"\s*sitemap\s*:\s*(.+?)\s*$", line, flags=re.I)
        if match:
            sitemaps.append(match.group(1).strip())
    return sitemaps[:10]


def _robots_crawl_delay(robots_text: str, aliases: list[str]) -> int | None:
    active = False
    for line in robots_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        agent_match = re.match(r"user-agent\s*:\s*(.+?)\s*$", stripped, flags=re.I)
        if agent_match:
            user_agent = agent_match.group(1).strip().lower()
            active = user_agent == "*" or user_agent in aliases
            continue
        if active:
            delay_match = re.match(r"crawl-delay\s*:\s*(\d+)", stripped, flags=re.I)
            if delay_match:
                return int(delay_match.group(1))
    return None


def _parse_robots_access(robots_url: str, robots_text: str, status_code: int | None) -> dict:
    found = status_code == 200 and bool(robots_text.strip())
    parser = RobotFileParser()
    if found:
        parser.set_url(robots_url)
        parser.parse(robots_text.splitlines())
    mentioned_agents = _robots_user_agents(robots_text)
    crawler_rows = []
    for crawler in AI_CRAWLER_GROUPS:
        aliases = [alias.lower() for alias in crawler["aliases"]]
        mentioned = any(alias in mentioned_agents for alias in aliases)
        wildcard = "*" in mentioned_agents
        if not found:
            status = "allowed_by_default"
        else:
            allowed = any(parser.can_fetch(alias, "/") for alias in aliases)
            status = "allowed" if allowed else "blocked"
        crawler_rows.append({
            "id": crawler["id"],
            "name": crawler["name"],
            "operator": crawler["operator"],
            "market": crawler["market"],
            "weight": crawler["weight"],
            "status": status,
            "mentioned": mentioned,
            "usesWildcard": wildcard and not mentioned,
            "crawlDelay": _robots_crawl_delay(robots_text, aliases) if found else None,
        })

    def weighted_score(market: str) -> int:
        rows = [row for row in crawler_rows if row["market"] == market]
        total = sum(int(row["weight"]) for row in rows)
        allowed = sum(int(row["weight"]) for row in rows if row["status"] != "blocked")
        return round(allowed / total * 100) if total else 0

    domestic_score = weighted_score("domestic")
    international_score = weighted_score("international")
    blocked = [row for row in crawler_rows if row["status"] == "blocked"]
    return {
        "url": robots_url,
        "found": found,
        "statusCode": status_code,
        "sitemaps": _robots_sitemaps(robots_text) if found else [],
        "crawlers": crawler_rows,
        "domesticScore": domestic_score,
        "internationalScore": international_score,
        "score": round(domestic_score * 0.7 + international_score * 0.3),
        "blockedCritical": [row for row in blocked if row["market"] == "domestic"],
        "summary": f"国内 AI 爬虫可访问评分 {domestic_score}/100，国际 AI 爬虫可访问评分 {international_score}/100。",
    }


def _validate_llms_text(url: str, text: str, status_code: int | None) -> dict:
    found = status_code == 200 and bool(text.strip())
    lines = [line.rstrip() for line in text.splitlines()] if found else []
    nonempty = [line for line in lines if line.strip()]
    item_pattern = re.compile(r"^\s*-\s*\[[^\]]+\]\((https?://[^)]+)\)\s*[:：-]?\s*(.*)$")
    items = [item_pattern.match(line) for line in lines]
    items = [item for item in items if item]
    absolute_url_count = len(items)
    described_count = len([item for item in items if item.group(2).strip()])
    checks = {
        "h1Title": bool(nonempty and nonempty[0].startswith("# ")),
        "quotedDescription": any(line.strip().startswith(">") for line in nonempty[:5]),
        "h2Sections": len([line for line in lines if line.startswith("## ")]) >= 1,
        "pageItems": len(items) >= 5,
        "absoluteUrls": absolute_url_count == len(items) and len(items) > 0,
        "itemDescriptions": described_count >= max(1, round(len(items) * 0.8)) if items else False,
        "keyFacts": _contains_any(text, ["关键事实", "Key Facts", "事实", "成立于", "总部", "客户", "users", "customers"]),
        "contact": _contains_any(text, ["联系", "Contact", "邮箱", "email", "support@", "电话"]),
        "reasonableLength": 30 <= len(lines) <= 220,
    }
    completeness = min(100, 15 + (20 if checks["h1Title"] else 0) + (15 if checks["quotedDescription"] else 0) + (20 if checks["h2Sections"] else 0) + min(30, len(items) * 3))
    accuracy = min(100, 25 + (30 if checks["absoluteUrls"] else 0) + (25 if checks["itemDescriptions"] else 0) + (20 if checks["keyFacts"] else 0))
    usefulness = min(100, 20 + (20 if checks["quotedDescription"] else 0) + (20 if checks["h2Sections"] else 0) + (20 if checks["pageItems"] else 0) + (10 if checks["keyFacts"] else 0) + (10 if checks["contact"] else 0))
    score = round(completeness * 0.4 + accuracy * 0.35 + usefulness * 0.25) if found else 0
    return {
        "url": url,
        "found": found,
        "statusCode": status_code,
        "lineCount": len(lines),
        "itemCount": len(items),
        "checks": checks,
        "scores": {
            "completeness": round(completeness) if found else 0,
            "accuracy": round(accuracy) if found else 0,
            "usefulness": round(usefulness) if found else 0,
            "overall": score,
        },
        "missingChecks": [key for key, value in checks.items() if not value],
    }


async def _fetch_aux_text(client: httpx.AsyncClient, url: str) -> tuple[int | None, str, str | None]:
    try:
        response = await client.get(url, headers={"User-Agent": "Mozilla/5.0 GeniLinkBot/1.0"})
        if response.status_code >= 400:
            return response.status_code, "", None
        return response.status_code, response.text[:200_000], str(response.url)
    except Exception as exc:  # pragma: no cover - network failures vary
        return None, "", str(exc)


async def analyze_product_website_technical_access(final_url: str) -> dict:
    root = _site_root(final_url)
    robots_url = urljoin(root + "/", "robots.txt")
    llms_url = urljoin(root + "/", "llms.txt")
    llms_full_url = urljoin(root + "/", "llms-full.txt")
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=min(settings.product_website_crawler_timeout_seconds, 12),
        trust_env=False,
    ) as client:
        robots_status, robots_text, robots_error = await _fetch_aux_text(client, robots_url)
        llms_status, llms_text, llms_error = await _fetch_aux_text(client, llms_url)
        llms_full_status, llms_full_text, llms_full_error = await _fetch_aux_text(client, llms_full_url)

    robots = _parse_robots_access(robots_url, robots_text, robots_status)
    llms = _validate_llms_text(llms_url, llms_text, llms_status)
    llms_full = _validate_llms_text(llms_full_url, llms_full_text, llms_full_status)
    llms_score = llms["scores"]["overall"]
    if llms_full["found"]:
        llms_score = round(llms_score * 0.75 + llms_full["scores"]["overall"] * 0.25)
    return {
        "robots": {**robots, "error": robots_error if robots_status is None else None},
        "llms": {**llms, "error": llms_error if llms_status is None else None},
        "llmsFull": {**llms_full, "error": llms_full_error if llms_full_status is None else None},
        "score": {
            "crawlerAccess": robots["score"],
            "llmsReadiness": llms_score,
            "overall": round(robots["score"] * 0.7 + llms_score * 0.3),
        },
    }


def _apply_technical_audit_to_score(score: dict, technical_audit: dict | None) -> dict:
    if not technical_audit:
        return score
    dimensions = dict(score.get("dimensions") or {})
    base_technical = dimensions.get("technicalGeo")
    if base_technical is not None:
        access_score = (technical_audit.get("score") or {}).get("overall")
        if access_score is not None:
            dimensions["technicalGeo"] = round(base_technical * 0.65 + access_score * 0.35)
    updated = {**score, "dimensions": dimensions, "technicalAudit": technical_audit}
    updated["overall"] = round(
        dimensions.get("aiCitability", 0) * 0.25
        + dimensions.get("brandAuthority", 0) * 0.20
        + dimensions.get("eeat", 0) * 0.20
        + dimensions.get("technicalGeo", 0) * 0.15
        + dimensions.get("schemaStructuredData", 0) * 0.10
        + dimensions.get("platformOptimization", 0) * 0.10,
        1,
    )
    updated["grade"] = _grade(updated["overall"])
    return updated


def _content_detail(
    page: ExtractedPage,
    final_url: str,
    project: dict,
    crawler_diagnostics: dict | None,
) -> dict:
    parsed = urlparse(final_url)
    internal_count, external_count = _link_counts(page)
    headings_by_level = {
        f"h{level}": [heading.get("text", "") for heading in page.headings if heading.get("level") == level][:12]
        for level in range(1, 4)
    }
    cta_tokens = ("联系", "咨询", "试用", "演示", "购买", "注册", "预约", "了解", "demo", "trial", "contact")
    cta_candidates = [
        link for link in page.links
        if any(token in (link.get("text") or "").lower() for token in cta_tokens)
    ][:12]
    external_samples = [
        link for link in page.links
        if link.get("href", "").startswith("http") and parsed.hostname not in link.get("href", "")
    ][:12]
    return {
        "metadata": {
            "finalUrl": final_url,
            "domain": parsed.hostname,
            "title": page.title,
            "description": page.description,
            "canonical": page.canonical,
            "lang": page.lang,
            "charset": page.charset,
            "viewport": page.viewport,
        },
        "headings": {
            **headings_by_level,
            "outline": page.headings[:30],
            "total": len(page.headings),
        },
        "paragraphs": [
            {"text": paragraph[:420], "wordCount": _word_count(paragraph)}
            for paragraph in page.paragraphs[:10]
        ],
        "links": {
            "internalCount": internal_count,
            "externalCount": external_count,
            "ctaCandidates": cta_candidates,
            "externalSamples": external_samples,
        },
        "images": {
            "total": page.image_count,
            "missingAlt": page.images_missing_alt,
            "missingAltRate": round(page.images_missing_alt / page.image_count * 100, 1) if page.image_count else 0,
        },
        "schema": {
            "jsonLdTypes": page.schema_types,
            "rawCount": len(page.schema_types),
        },
        "keywordCoverage": _keyword_coverage(page, project),
        "crawl": {
            "provider": (crawler_diagnostics or {}).get("provider", "native_fetch"),
            "statusCode": (crawler_diagnostics or {}).get("statusCode"),
            "durationMs": (crawler_diagnostics or {}).get("durationMs"),
            "method": (crawler_diagnostics or {}).get("method"),
        },
    }


def _dimension_diagnostics(page: ExtractedPage, score: dict, project: dict) -> dict:
    dimensions = score.get("dimensions") or {}
    word_count = _word_count(page.body_text)
    h1_count = sum(1 for heading in page.headings if heading.get("level") == 1)
    internal_count, external_count = _link_counts(page)
    keyword_coverage = _keyword_coverage(page, project)
    product_name = project.get("product_name") or project.get("name") or ""
    diagnostics = {
        "structure": {
            "summary": f"检测到 {len(page.headings)} 个标题节点、{h1_count} 个 H1、{len(page.schema_types)} 类结构化数据。",
            "evidence": [f"H1 数量：{h1_count}", f"标题层级数量：{len(page.headings)}", f"Schema：{', '.join(page.schema_types) or '未检测到'}"],
            "issues": [],
            "opportunities": ["保持唯一 H1，按 H2/H3 承载功能、场景、客户价值、FAQ 等可引用段落。"],
        },
        "semantic": {
            "summary": f"产品关键词覆盖 {keyword_coverage.get('matched')}/{keyword_coverage.get('total')}。",
            "evidence": [f"已覆盖：{', '.join(keyword_coverage.get('matchedKeywords') or []) or '暂无'}", f"未覆盖：{', '.join(keyword_coverage.get('missing') or []) or '暂无'}"],
            "issues": [],
            "opportunities": ["将核心关键词分布到标题、首屏摘要、功能段落和 FAQ 问答中，避免只在导航或页脚出现。"],
        },
        "density": {
            "summary": f"正文约 {word_count} 词，段落 {len(page.paragraphs)} 个。",
            "evidence": [f"正文词数：{word_count}", f"段落数：{len(page.paragraphs)}"],
            "issues": [],
            "opportunities": ["补足适用对象、业务场景、关键能力、差异化优势、实施路径和常见问题。"],
        },
        "authority": {
            "summary": f"检测到外部链接 {external_count} 个，内部链接 {internal_count} 个。",
            "evidence": [f"外部链接：{external_count}", f"内部链接：{internal_count}"],
            "issues": [],
            "opportunities": ["加入客户案例、数据来源、认证资质、媒体报道或白皮书链接，增强 AI 可采信信号。"],
        },
        "technical": {
            "summary": "检查 title、description、canonical、lang、viewport、OpenGraph 与 Schema 等机器可读信号。",
            "evidence": [
                f"title：{'有' if page.title else '缺失'}",
                f"description：{'有' if page.description else '缺失'}",
                f"canonical：{'有' if page.canonical else '缺失'}",
                f"lang：{'有' if page.lang else '缺失'}",
                f"viewport：{'有' if page.viewport else '缺失'}",
            ],
            "issues": [],
            "opportunities": ["补齐基础 meta 与 JSON-LD，让搜索引擎和大模型更稳定地识别页面实体。"],
        },
        "readability": {
            "summary": f"平均每段约 {round(word_count / max(len(page.paragraphs), 1)) if page.paragraphs else 0} 词。",
            "evidence": [f"段落数：{len(page.paragraphs)}", f"标题数：{len(page.headings)}"],
            "issues": [],
            "opportunities": ["用短段落、列表、FAQ 和对比表组织内容，提升模型抽取摘要时的稳定性。"],
        },
        "productClarity": {
            "summary": f"产品名“{product_name or '未配置'}”在标题、描述和正文中的显性程度。",
            "evidence": [
                f"title 包含产品名：{'是' if product_name and product_name.lower() in (page.title or '').lower() else '否'}",
                f"description 包含产品名：{'是' if product_name and product_name.lower() in (page.description or '').lower() else '否'}",
                f"正文包含产品名：{'是' if product_name and product_name.lower() in page.body_text.lower() else '否'}",
            ],
            "issues": [],
            "opportunities": ["在首屏明确“产品是什么、服务谁、解决什么问题、为什么可信”。"],
        },
    }
    technical_audit = score.get("technicalAudit") or {}
    if technical_audit:
        robots = technical_audit.get("robots") or {}
        llms = technical_audit.get("llms") or {}
        llms_full = technical_audit.get("llmsFull") or {}
        audit_score = technical_audit.get("score") or {}
        blocked_domestic = robots.get("blockedCritical") or []
        diagnostics["technicalGeo"] = {
            "summary": f"已按 geo-crawlers 与 geo-llmstxt 子流程检测 robots.txt、AI 爬虫访问和 llms.txt，就绪度 {audit_score.get('overall', '--')}/100。",
            "evidence": [
                f"robots.txt：{'已发现' if robots.get('found') else '未发现'}，爬虫访问 {audit_score.get('crawlerAccess', '--')}/100",
                f"国内被阻止爬虫：{', '.join(item.get('name', '') for item in blocked_domestic) or '暂无'}",
                f"llms.txt：{'已发现' if llms.get('found') else '未发现'}，评分 {(llms.get('scores') or {}).get('overall', 0)}/100",
                f"llms-full.txt：{'已发现' if llms_full.get('found') else '未发现'}",
            ],
            "issues": [],
            "opportunities": [
                "优先确保 Baiduspider、Bytespider、DeepSeekBot、TencentCloudBot、Moonshot/Kimi、Sogou、360、AlibabaCloud 等国内关键爬虫未被阻止。",
                "在根目录发布 llms.txt，包含网站简介、核心页面、关键事实和联系信息；复杂站点可补充 llms-full.txt。",
                "在 robots.txt 中保留 Sitemap 指令，帮助 AI 爬虫发现核心页面。",
            ],
        }
    for key, item in diagnostics.items():
        value = dimensions.get(key)
        status = _score_status(value)
        if status == "weak":
            item["issues"].append("当前维度低于商业化产品页建议基线，需要优先补齐。")
        elif status == "watch":
            item["issues"].append("当前维度具备基础信号，但仍有明显提升空间。")
        item["score"] = value
        item["status"] = status
        item["label"] = DIMENSION_LABELS.get(key, key)
    return diagnostics


def _recommendation(
    rec_id: str,
    dimension: str,
    priority: str,
    impact: str,
    effort: str,
    title: str,
    problem: str,
    evidence: list[str],
    actions: list[str],
    expected_lift: int,
    success_metric: str,
    examples: list[str] | None = None,
) -> dict:
    return {
        "id": rec_id,
        "dimension": dimension,
        "dimensionLabel": DIMENSION_LABELS.get(dimension, dimension),
        "priority": priority,
        "impact": impact,
        "effort": effort,
        "title": title,
        "problem": problem,
        "detail": problem,
        "evidence": evidence,
        "actions": actions,
        "expectedLift": expected_lift,
        "successMetric": success_metric,
        "examples": examples or [],
    }


def _recommendations(page: ExtractedPage, score: dict, project: dict) -> list[dict]:
    recs: list[dict] = []
    product_name = project.get("product_name") or project.get("name") or ""
    dimensions = score.get("dimensions") or {}
    keyword_coverage = _keyword_coverage(page, project)
    word_count = _word_count(page.body_text)
    h1_count = sum(1 for heading in page.headings if heading.get("level") == 1)
    internal_count, external_count = _link_counts(page)
    if not page.title or (product_name and product_name.lower() not in (page.title or "").lower()):
        recs.append(_recommendation(
            "rec_title_product", "productClarity", "high", "high", "small",
            "在 title 中明确产品名和核心用途",
            "页面 title 没有稳定承载产品实体，AI 与搜索系统容易把页面识别为普通官网或营销页。",
            [page.title or "title 缺失"],
            [
                "将产品名、目标用户和核心用途写入 title，控制在 30-60 个中文字符。",
                "title 与 H1 保持同一产品实体，不使用过度抽象的品牌口号替代产品说明。",
                "在 OpenGraph title 中同步同样的产品实体表达。",
            ],
            6,
            "title、H1、OG title 均包含产品名，重新分析后产品清晰度提升到 80+。",
            [f"{product_name or '产品名'} - 面向企业的 AI 可见性分析平台"],
        ))
    if not page.description:
        recs.append(_recommendation(
            "rec_meta_description", "technical", "high", "medium", "small",
            "补充 meta description",
            "页面缺少摘要描述，搜索引擎和大模型缺少可直接抽取的页面说明。",
            ["description 缺失"],
            [
                "用 80-160 个中文字符说明产品服务对象、核心场景和差异化价值。",
                "自然包含 1-2 个核心产品关键词，不堆砌同义词。",
                "确保 description 与首屏正文保持一致，避免模型抽取到冲突信息。",
            ],
            5,
            "重新抓取后 technical 维度提升，页面摘要在报告中可被完整识别。",
        ))
    if not page.schema_types:
        recs.append(_recommendation(
            "rec_schema", "technical", "medium", "medium", "medium",
            "增加 Product 或 SoftwareApplication Schema",
            "页面缺少结构化数据，AI 难以稳定识别产品实体、组织主体、功能和问答关系。",
            ["未检测到 JSON-LD Schema"],
            [
                "增加 SoftwareApplication 或 Product JSON-LD，包含 name、description、applicationCategory、url、provider。",
                "增加 Organization JSON-LD，并用 sameAs 指向权威社媒、备案、产品文档或品牌主页。",
                "如页面包含 FAQ，补充 FAQPage JSON-LD，问题与正文可见内容保持一致。",
            ],
            5,
            "Schema rawCount 大于 0，technical 维度提升，报告展示出可识别 Schema 类型。",
        ))
    if dimensions.get("density", 0) < 70:
        recs.append(_recommendation(
            "rec_content_depth", "density", "medium", "high", "medium",
            "补充产品场景、功能证据和 FAQ",
            "页面正文信息密度不足，AI 很难生成具体、可引用的产品答案。",
            [f"正文词数约 {word_count}", f"段落数 {len(page.paragraphs)}"],
            [
                "增加“适用对象/业务场景/核心能力/交付结果/典型问题”五类内容块。",
                "每个核心能力给出具体输入、输出、使用流程和业务指标，不只写泛化价值口号。",
                "增加 5-8 个 FAQ，覆盖价格、部署、数据安全、竞品差异、适用行业等高频问题。",
            ],
            8,
            "正文词数达到 800+，段落不少于 8 个，density 维度提升到 75+。",
        ))
    if dimensions.get("structure", 0) < 75 or h1_count != 1:
        recs.append(_recommendation(
            "rec_heading_structure", "structure", "medium", "medium", "small",
            "重构 H1/H2/H3 信息层级",
            "标题层级不足或 H1 不规范，会降低模型定位核心主题与段落边界的能力。",
            [f"H1 数量：{h1_count}", f"标题总数：{len(page.headings)}"],
            [
                "保留唯一 H1，直接描述产品定位。",
                "用 H2 承载核心模块：产品能力、适用场景、客户案例、数据安全、FAQ。",
                "H3 用于拆分功能点和证据，避免连续大段正文没有标题。",
            ],
            5,
            "H1 数量为 1，H2/H3 覆盖核心内容块，structure 维度达到 80+。",
        ))
    if dimensions.get("semantic", 0) < 75 or keyword_coverage.get("missing"):
        recs.append(_recommendation(
            "rec_keyword_coverage", "semantic", "high", "high", "medium",
            "补齐产品关键词与实体关系",
            "核心关键词覆盖不完整，AI 在回答相关问题时更可能引用竞品或泛行业内容。",
            [
                f"覆盖 {keyword_coverage.get('matched')}/{keyword_coverage.get('total')}",
                f"缺失关键词：{', '.join(keyword_coverage.get('missing') or []) or '暂无'}",
            ],
            [
                "将缺失关键词分配到不同正文模块，不集中堆在同一段。",
                "围绕每个关键词补一句“是什么”、一句“解决什么问题”、一句“适用什么场景”。",
                "在 FAQ 中用自然问句覆盖长尾表达，例如“什么场景适合使用……”。",
            ],
            7,
            "关键词覆盖率达到 80% 以上，semantic 维度提升到 80+。",
        ))
    if dimensions.get("authority", 0) < 75:
        recs.append(_recommendation(
            "rec_authority_evidence", "authority", "medium", "high", "medium",
            "补充可信证据和第三方引用",
            "页面缺少客户、案例、数据来源或外部权威链接，商业可信度和 AI 采信概率不足。",
            [f"外部链接：{external_count}", f"内部链接：{internal_count}"],
            [
                "增加客户案例、行业解决方案、白皮书、产品文档或安全合规说明入口。",
                "对关键数据补充来源说明或可验证链接，避免孤立数字。",
                "在页面中明确公司主体、服务资质、隐私安全和联系方式。",
            ],
            6,
            "至少新增 3 类可信证据，authority 维度提升到 75+。",
        ))
    if page.image_count and page.images_missing_alt / page.image_count > 0.3:
        recs.append(_recommendation(
            "rec_image_alt", "technical", "low", "medium", "small",
            "补齐关键图片 alt 文本",
            "大量图片缺少 alt，会让模型和无障碍工具丢失截图、流程图、产品界面的语义。",
            [f"图片 {page.image_count} 张，缺失 alt {page.images_missing_alt} 张"],
            [
                "为产品界面、流程图、数据图表补充描述性 alt，不写“图片1”这类无效文本。",
                "alt 中包含产品模块、界面状态和展示的数据类型。",
                "装饰性图片可保留空 alt，但核心信息图片必须可读。",
            ],
            3,
            "关键图片 alt 完整率达到 90% 以上。",
        ))
    return sorted(recs, key=lambda item: {"high": 0, "medium": 1, "low": 2}.get(item.get("priority"), 3))[:10]


DIMENSION_LABELS = {
    **DIMENSION_LABELS,
    "aiCitability": "AI 可引用性",
    "brandAuthority": "品牌权威性",
    "eeat": "内容 E-E-A-T",
    "technicalGeo": "技术 GEO",
    "schemaStructuredData": "架构与结构化数据",
    "platformOptimization": "平台优化",
}


def _contains_any(text: str, tokens: list[str]) -> bool:
    lower = text.lower()
    return any(token.lower() in lower for token in tokens)


def _count_any(text: str, tokens: list[str]) -> int:
    lower = text.lower()
    return sum(1 for token in tokens if token.lower() in lower)


def _number_signal_count(text: str) -> int:
    return len(re.findall(r"(\d+(\.\d+)?%|\d+(\.\d+)?\s*(元|万|亿|天|周|月|年|个|家|次|%|ms|s))", text))


def _json_ld_nodes(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [node for item in value for node in _json_ld_nodes(item)]
    if not isinstance(value, dict):
        return []
    nodes: list[dict[str, Any]] = []
    if value.get("@type"):
        nodes.append(value)
    graph = value.get("@graph")
    if isinstance(graph, list):
        nodes.extend(node for item in graph for node in _json_ld_nodes(item))
    return nodes


def _schema_type_names(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _schema_nodes_by_type(page: ExtractedPage) -> dict[str, list[dict[str, Any]]]:
    nodes_by_type: dict[str, list[dict[str, Any]]] = {}
    for node in page.schema_nodes:
        for schema_type in _schema_type_names(node.get("@type")):
            nodes_by_type.setdefault(schema_type.lower(), []).append(node)
    return nodes_by_type


def _schema_requirements(schema_type: str) -> list[str]:
    requirements = {
        "Organization": ["name", "url", "logo", "sameAs"],
        "WebSite": ["name", "url", "publisher"],
        "SoftwareApplication": ["name", "applicationCategory", "operatingSystem", "offers"],
        "FAQPage": ["mainEntity"],
        "HowTo": ["name", "step"],
        "LocalBusiness": ["name", "address", "telephone", "openingHours", "geo"],
        "GeoCoordinates": ["latitude", "longitude"],
        "Product": ["name", "description", "brand", "offers"],
        "Offer": ["price", "priceCurrency", "availability"],
        "AggregateRating": ["ratingValue", "reviewCount"],
        "BreadcrumbList": ["itemListElement"],
        "Article": ["headline", "author", "datePublished", "dateModified"],
        "Person": ["name", "jobTitle", "affiliation"],
        "Service": ["name", "provider", "areaServed", "serviceType"],
        "Review": ["reviewRating", "author", "itemReviewed"],
    }
    return requirements.get(schema_type, ["name", "url"])


def _schema_same_as_urls(page: ExtractedPage) -> list[str]:
    urls: list[str] = []
    for node in page.schema_nodes:
        same_as = node.get("sameAs")
        if isinstance(same_as, str):
            urls.append(same_as)
        elif isinstance(same_as, list):
            urls.extend(item for item in same_as if isinstance(item, str))
    for link in page.links:
        href = link.get("href", "")
        if _contains_any(href, ["zhihu", "baike.baidu", "weibo", "bilibili", "xiaohongshu", "douyin", "mp.weixin"]):
            urls.append(href)
    return sorted(set(urls))


def _schema_example(schema_type: str, page: ExtractedPage) -> dict[str, Any]:
    name = page.title or "Brand Name"
    examples: dict[str, dict[str, Any]] = {
        "Organization": {
            "@context": "https://schema.org",
            "@type": "Organization",
            "name": name,
            "url": page.canonical or "https://example.com",
            "logo": "https://example.com/logo.png",
            "sameAs": ["https://zhihu.com/org/example", "https://baike.baidu.com/item/example"],
        },
        "WebSite": {
            "@context": "https://schema.org",
            "@type": "WebSite",
            "name": name,
            "url": page.canonical or "https://example.com",
            "publisher": {"@type": "Organization", "name": name},
        },
        "SoftwareApplication": {
            "@context": "https://schema.org",
            "@type": "SoftwareApplication",
            "name": name,
            "applicationCategory": "BusinessApplication",
            "operatingSystem": "Web",
            "offers": {"@type": "Offer", "price": "0", "priceCurrency": "CNY"},
        },
        "FAQPage": {
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": [{"@type": "Question", "name": "What does this product do?", "acceptedAnswer": {"@type": "Answer", "text": "Describe the product in one factual paragraph."}}],
        },
    }
    return examples.get(schema_type, {"@context": "https://schema.org", "@type": schema_type, "name": name})


INTEGRATED_MODEL_LABELS = {
    "deepseek": "DeepSeek",
    "doubao": "豆包",
    "hunyuan": "腾讯元宝",
    "qwen": "通义千问",
    "kimi": "Kimi",
}


THIRD_PARTY_PLATFORM_RULES = [
    {
        "id": "baike",
        "label": "百度百科/百家号",
        "tokens": ["baike.baidu", "baijiahao.baidu", "百度百科", "百家号"],
        "models": ["deepseek", "doubao", "hunyuan", "qwen", "kimi"],
        "weight": 22,
    },
    {
        "id": "zhihu",
        "label": "知乎",
        "tokens": ["zhihu.com", "知乎"],
        "models": ["deepseek", "doubao", "hunyuan", "qwen", "kimi"],
        "weight": 18,
    },
    {
        "id": "wechat",
        "label": "微信公众号",
        "tokens": ["mp.weixin.qq.com", "微信公众号", "微信公众"],
        "models": ["deepseek", "doubao", "hunyuan", "qwen", "kimi"],
        "weight": 16,
    },
    {
        "id": "bilibili",
        "label": "Bilibili",
        "tokens": ["bilibili.com", "B站", "哔哩哔哩"],
        "models": ["deepseek", "doubao", "qwen", "kimi"],
        "weight": 14,
    },
    {
        "id": "weibo",
        "label": "微博",
        "tokens": ["weibo.com", "微博"],
        "models": ["doubao", "hunyuan", "qwen"],
        "weight": 10,
    },
    {
        "id": "xiaohongshu",
        "label": "小红书",
        "tokens": ["xiaohongshu.com", "小红书"],
        "models": ["doubao", "qwen", "kimi"],
        "weight": 10,
    },
    {
        "id": "douyin",
        "label": "抖音",
        "tokens": ["douyin.com", "抖音"],
        "models": ["doubao", "qwen"],
        "weight": 10,
    },
]


def _integrated_product_website_models() -> list[str]:
    configured = configured_product_website_citation_platforms()
    return configured or ["deepseek", "doubao", "hunyuan", "qwen", "kimi"]


def _platform_presence(page: ExtractedPage, schema_quality: dict | None = None) -> dict:
    integrated_models = _integrated_product_website_models()
    text = " ".join([
        page.body_text,
        page.nav_text,
        page.footer_text,
        " ".join(link.get("href", "") + " " + link.get("text", "") for link in page.links),
        " ".join(((schema_quality or {}).get("sameAs") or {}).get("urls") or []),
    ])
    platforms = []
    for rule in THIRD_PARTY_PLATFORM_RULES:
        found = _contains_any(text, rule["tokens"])
        applicable_models = [model for model in rule["models"] if model in integrated_models]
        platforms.append({
            "id": rule["id"],
            "label": rule["label"],
            "found": found,
            "weight": rule["weight"],
            "models": applicable_models,
            "evidence": [token for token in rule["tokens"] if token.lower() in text.lower()][:5],
        })
    total_weight = sum(item["weight"] for item in platforms if item["models"])
    found_weight = sum(item["weight"] for item in platforms if item["models"] and item["found"])
    score = round(found_weight / total_weight * 100) if total_weight else 0
    model_advice = []
    for model in integrated_models:
        relevant = [item for item in platforms if model in item["models"]]
        found = [item for item in relevant if item["found"]]
        missing = [item for item in relevant if not item["found"]]
        model_score = round(sum(item["weight"] for item in found) / sum(item["weight"] for item in relevant) * 100) if relevant else 0
        model_advice.append({
            "model": model,
            "label": INTEGRATED_MODEL_LABELS.get(model, model),
            "score": model_score,
            "coveredPlatforms": [item["label"] for item in found],
            "missingPlatforms": [item["label"] for item in missing[:3]],
            "advice": _model_platform_advice(model, missing),
        })
    return {
        "models": [{"id": model, "label": INTEGRATED_MODEL_LABELS.get(model, model)} for model in integrated_models],
        "platforms": platforms,
        "score": score,
        "modelAdvice": model_advice,
    }


def _model_platform_advice(model: str, missing: list[dict]) -> str:
    labels = "、".join(item["label"] for item in missing[:2])
    if not labels:
        return "当前官网已暴露主要第三方平台入口，继续保持 sameAs 与外链一致。"
    if model == "deepseek":
        return f"优先补齐可搜索、可引用的 {labels} 页面，并在官网 sameAs 中互相指向。"
    if model == "doubao":
        return f"优先补齐 {labels} 与短视频/内容平台入口，强化字节生态和通用搜索可见性。"
    if model == "hunyuan":
        return f"优先补齐 {labels} 与微信公众号入口，强化腾讯生态内的品牌实体信号。"
    if model == "qwen":
        return f"优先补齐 {labels}，并让平台内容与官网 Schema/sameAs 保持实体名称一致。"
    if model == "kimi":
        return f"优先补齐 {labels} 的长文本解释、案例或评测内容，提升可引用证据密度。"
    return f"优先补齐 {labels} 平台入口，并从官网进行可验证链接。"


def _eeat_signals(
    page: ExtractedPage,
    project: dict,
    keyword_coverage: dict,
    number_count: int,
    external_count: int,
) -> dict:
    text = page.body_text
    link_text = " ".join(link.get("href", "") + " " + link.get("text", "") for link in page.links)
    schema_text = " ".join(page.schema_types)

    experience_tokens = [
        "客户案例", "实践", "我们发现", "基于我们", "试点", "落地", "复盘", "案例研究",
        "customer case", "pilot", "case study", "implementation", "we found", "based on our",
    ]
    expertise_tokens = [
        "专家", "团队", "作者", "方法论", "研究", "报告", "调研", "来源", "引用", "白皮书",
        "expert", "team", "author", "methodology", "research", "report", "source", "citation", "white paper",
    ]
    authority_tokens = [
        "认证", "媒体", "获奖", "资质", "合作伙伴", "客户", "案例", "白皮书", "报告",
        "certified", "media", "award", "partner", "customer", "case", "white paper", "report",
    ]
    trust_tokens = [
        "隐私", "安全", "合规", "服务 SLA", "SLA", "联系方式", "联系电话", "邮箱", "ICP", "备案", "更新于",
        "privacy", "security", "compliance", "contact", "email", "updated", "terms", "status page",
    ]
    same_as_tokens = ["sameas", "same as", "知乎", "百科", "微博", "Bilibili", "小红书", "抖音", "zhihu", "baike", "weibo"]

    experience_hits = _count_any(text, experience_tokens)
    expertise_hits = _count_any(text + " " + schema_text, expertise_tokens + ["Person", "Organization"])
    authority_hits = _count_any(text + " " + link_text, authority_tokens + same_as_tokens)
    trust_hits = _count_any(text + " " + page.footer_text, trust_tokens)

    keyword_bonus = 10 if keyword_coverage.get("coverageRate", 0) and keyword_coverage["coverageRate"] >= 60 else 0
    experience = min(100, 20 + experience_hits * 12 + min(20, number_count * 4) + keyword_bonus)
    expertise = min(100, 20 + expertise_hits * 10 + min(15, len(page.schema_types) * 4) + min(15, page.table_count * 8))
    authoritativeness = min(100, 20 + authority_hits * 9 + min(20, external_count * 4) + (10 if "sameas" in (text + " " + link_text).lower() else 0))
    trustworthiness = min(
        100,
        20
        + trust_hits * 8
        + (8 if page.canonical else 0)
        + (8 if page.lang else 0)
        + (8 if page.description else 0)
        + (8 if not (page.robots_meta and "noindex" in page.robots_meta.lower()) else 0),
    )

    evidence = {
        "experience": [
            f"实践/案例信号：{experience_hits}",
            f"数字证据：{number_count}",
            f"关键词覆盖：{keyword_coverage.get('matched')}/{keyword_coverage.get('total')}",
        ],
        "expertise": [
            f"专家/作者/方法论信号：{expertise_hits}",
            f"结构化数据类型：{', '.join(page.schema_types) or '暂无'}",
            f"表格：{page.table_count}",
        ],
        "authoritativeness": [
            f"权威/客户/媒体信号：{authority_hits}",
            f"外部链接：{external_count}",
            f"sameAs/平台信号：{'有' if _contains_any(text + ' ' + link_text, same_as_tokens) else '缺失'}",
        ],
        "trustworthiness": [
            f"隐私/安全/合规/联系信号：{trust_hits}",
            f"canonical：{'有' if page.canonical else '缺失'}",
            f"robots meta：{page.robots_meta or '未检测到'}",
        ],
    }
    gaps = {
        "experience": [] if experience >= 75 else ["补充客户实践、试点结果、复盘或第一方研究背景。"],
        "expertise": [] if expertise >= 75 else ["补充作者/团队资质、方法论、来源引用和研究报告。"],
        "authoritativeness": [] if authoritativeness >= 75 else ["补充客户、媒体、认证、白皮书和可信第三方平台入口。"],
        "trustworthiness": [] if trustworthiness >= 75 else ["补充隐私、安全、合规、联系方式、备案和更新时间。"],
    }
    overall = round(experience * 0.25 + expertise * 0.30 + authoritativeness * 0.20 + trustworthiness * 0.25)
    return {
        "subScores": {
            "experience": experience,
            "expertise": expertise,
            "authoritativeness": authoritativeness,
            "trustworthiness": trustworthiness,
        },
        "evidence": evidence,
        "gaps": gaps,
        "overall": overall,
    }


def _detect_business_type(page: ExtractedPage) -> dict:
    text = " ".join([page.body_text, page.nav_text, page.footer_text]).lower()
    hrefs = " ".join(link.get("href", "") for link in page.links).lower()
    signals = {
        "saas": _count_any(text + hrefs, ["pricing", "price", "定价", "免费试用", "注册", "demo", "api", "集成", "integration", "控制台", "console"]),
        "localBusiness": _count_any(text + hrefs, ["地址", "电话", "门店", "附近", "地图", "营业时间", "localbusiness"]),
        "ecommerce": _count_any(text + hrefs, ["购物车", "加入购物车", "sku", "价格", "商品", "订单", "product", "offer"]),
        "publisher": _count_any(text + hrefs, ["博客", "文章", "作者", "资讯", "rss", "article", "blog", "news"]),
        "serviceAgency": _count_any(text + hrefs, ["案例", "客户", "作品", "团队", "服务", "解决方案", "case", "portfolio"]),
    }
    best = max(signals, key=signals.get)
    detected = "hybrid" if sorted(signals.values(), reverse=True)[:2] == [signals[best], signals[best]] and signals[best] > 0 else best
    if "softwareapplication" in " ".join(page.schema_types).lower() and signals["saas"] > 0:
        detected = "saas"
    if signals[best] == 0:
        detected = "unknown"
    labels = {
        "saas": "SaaS",
        "localBusiness": "本地商家",
        "ecommerce": "电子商务",
        "publisher": "发布者",
        "serviceAgency": "代理商/服务",
        "hybrid": "混合",
        "unknown": "未识别",
    }
    return {"type": detected, "label": labels[detected], "signals": signals}


def _schema_quality(page: ExtractedPage, business_type: str) -> dict:
    body = page.body_text.lower()
    schema_text = " ".join(page.schema_types).lower() + " " + body
    nodes_by_type = _schema_nodes_by_type(page)
    recommended = ["Organization", "WebSite"]
    if business_type == "saas":
        recommended += ["SoftwareApplication", "FAQPage", "HowTo"]
    elif business_type == "localBusiness":
        recommended += ["LocalBusiness", "GeoCoordinates"]
    elif business_type == "ecommerce":
        recommended += ["Product", "Offer", "AggregateRating", "BreadcrumbList"]
    elif business_type == "publisher":
        recommended += ["Article", "Person"]
    elif business_type == "serviceAgency":
        recommended += ["Service", "Person", "Review"]
    found = [item for item in recommended if item.lower() in schema_text]
    property_completeness = []
    for schema_type in recommended:
        nodes = nodes_by_type.get(schema_type.lower(), [])
        requirements = _schema_requirements(schema_type)
        present = sorted({
            prop
            for node in nodes
            for prop in requirements
            if node.get(prop) not in (None, "", [], {})
        })
        missing_props = [prop for prop in requirements if prop not in present]
        property_completeness.append({
            "type": schema_type,
            "found": bool(nodes),
            "required": requirements,
            "present": present,
            "missing": missing_props,
            "score": round(len(present) / len(requirements) * 100) if requirements else 100,
        })
    typed_entries = [item for item in property_completeness if item["found"]]
    property_score = round(sum(item["score"] for item in typed_entries) / len(typed_entries)) if typed_entries else 0
    same_as_urls = _schema_same_as_urls(page)
    domestic_same_as = [url for url in same_as_urls if _contains_any(url, ["zhihu", "baike.baidu", "weibo", "bilibili", "xiaohongshu", "douyin", "mp.weixin"])]
    same_as_score = min(100, len(same_as_urls) * 18 + len(domestic_same_as) * 12)
    missing_examples = [
        {"type": schema_type, "jsonLd": _schema_example(schema_type, page)}
        for schema_type in recommended
        if schema_type not in found
    ][:3]
    incomplete_examples = [
        {"type": item["type"], "missing": item["missing"], "jsonLd": _schema_example(item["type"], page)}
        for item in property_completeness
        if item["found"] and item["missing"]
    ][:3]
    return {
        "recommended": recommended,
        "found": found,
        "missing": [item for item in recommended if item not in found],
        "propertyCompleteness": property_completeness,
        "propertyScore": property_score,
        "sameAs": {
            "urls": same_as_urls,
            "domesticUrls": domestic_same_as,
            "score": same_as_score,
            "status": "strong" if same_as_score >= 70 else "watch" if same_as_score >= 35 else "weak",
        },
        "examples": missing_examples + incomplete_examples,
    }


def _geo_subscores(page: ExtractedPage, project: dict, final_url: str | None = None) -> dict:
    text = page.body_text
    lower = text.lower()
    word_count = _word_count(text)
    paragraph_count = max(len(page.paragraphs), 1)
    h1_count = sum(1 for heading in page.headings if heading.get("level") == 1)
    h2_count = sum(1 for heading in page.headings if heading.get("level") == 2)
    keyword_coverage = _keyword_coverage(page, project)
    business = _detect_business_type(page)
    schema_quality = _schema_quality(page, business["type"])
    platform_presence = _platform_presence(page, schema_quality)

    definition_patterns = ["是指", "是一种", "是一个", "核心含义", "means", "is a", "refers to"]
    question_headings = sum(1 for heading in page.headings if "?" in heading.get("text", "") or "？" in heading.get("text", "") or _contains_any(heading.get("text", ""), ["什么", "如何", "为什么", "怎么"]))
    short_paragraphs = sum(1 for paragraph in page.paragraphs if 60 <= _word_count(paragraph) <= 250)
    named_subject_paragraphs = sum(1 for paragraph in page.paragraphs if _contains_any(paragraph, [project.get("product_name") or "", project.get("name") or ""]) and _word_count(paragraph) >= 30)
    number_count = _number_signal_count(text)
    answer_quality = min(100, 30 + _count_any(text[:1200], definition_patterns) * 15 + question_headings * 8 + (20 if keyword_coverage.get("matched") else 0))
    self_contained = min(100, 35 + round(named_subject_paragraphs / paragraph_count * 40) + (15 if keyword_coverage.get("coverageRate", 0) and keyword_coverage["coverageRate"] >= 60 else 0))
    structure_readability = min(100, 25 + (20 if h1_count == 1 else 0) + min(25, h2_count * 5) + min(15, page.list_count * 5) + min(15, page.table_count * 8) + min(10, round(short_paragraphs / paragraph_count * 10)))
    statistics_density = min(100, 20 + min(60, round(number_count / max(word_count, 1) * 500 * 15)) + (20 if _contains_any(text, ["报告", "研究", "调研", "数据显示", "根据", "report", "research", "survey", "data shows", "based on"]) else 0))
    uniqueness = min(100, 25 + _count_any(text, ["我们发现", "调研", "客户案例", "白皮书", "方法论", "专有", "原创", "基于我们", "we found", "research", "customer case", "white paper", "methodology", "proprietary", "original", "based on our"]) * 10)
    ai_citability = round(answer_quality * 0.30 + self_contained * 0.25 + structure_readability * 0.20 + statistics_density * 0.15 + uniqueness * 0.10)

    authority_tokens = ["客户", "案例", "认证", "报告", "白皮书", "媒体", "获奖", "资质", "合作伙伴", "case", "customer", "certified"]
    cn_platform_tokens = ["zhihu", "知乎", "baike", "百科", "bilibili", "微博", "weibo", "小红书", "xiaohongshu", "抖音", "douyin", "微信公众号", "mp.weixin"]
    brand_authority = min(100, 25 + _count_any(text, authority_tokens) * 8 + min(20, _link_counts(page)[1] * 4) + _count_any(" ".join(link.get("href", "") + link.get("text", "") for link in page.links), cn_platform_tokens) * 6)
    brand_authority = min(100, brand_authority + _count_any(text, ["white paper", "report", "media", "award", "partner", "security", "compliance", "customer evidence"]) * 4)

    internal_count, external_count = _link_counts(page)
    eeat_signals = _eeat_signals(page, project, keyword_coverage, number_count, external_count)
    eeat = eeat_signals["overall"]

    crawler_diag = {}
    technical_geo = min(100,
        (12 if page.title else 0)
        + (12 if page.description else 0)
        + (10 if page.canonical else 0)
        + (10 if page.lang else 0)
        + (10 if page.viewport else 0)
        + (10 if page.open_graph else 0)
        + (6 if page.twitter_card else 0)
        + (15 if word_count >= 300 else 0)
        + (15 if not (page.robots_meta and "noindex" in page.robots_meta.lower()) else 0)
    )

    type_coverage = round(len(schema_quality["found"]) / len(schema_quality["recommended"]) * 100) if schema_quality.get("recommended") else 0
    property_score = schema_quality.get("propertyScore", 0)
    same_as_score = (schema_quality.get("sameAs") or {}).get("score", 0)
    schema_structured = min(100, round(
        (15 if page.schema_types else 0)
        + type_coverage * 0.40
        + property_score * 0.30
        + same_as_score * 0.15
        + (10 if page.open_graph else 0)
    ))
    platform_optimization = min(100, round(
        platform_presence.get("score", 0) * 0.75
        + min(25, _count_any(text + " " + " ".join(link.get("href", "") for link in page.links), cn_platform_tokens) * 5)
    ))

    dimensions = {
        "aiCitability": ai_citability,
        "brandAuthority": brand_authority,
        "eeat": eeat,
        "technicalGeo": technical_geo,
        "schemaStructuredData": schema_structured,
        "platformOptimization": platform_optimization,
    }
    overall = round(
        dimensions["aiCitability"] * 0.25
        + dimensions["brandAuthority"] * 0.20
        + dimensions["eeat"] * 0.20
        + dimensions["technicalGeo"] * 0.15
        + dimensions["schemaStructuredData"] * 0.10
        + dimensions["platformOptimization"] * 0.10,
        1,
    )
    return {
        "dimensions": dimensions,
        "overall": overall,
        "grade": _grade(overall),
        "businessType": business,
        "schemaQuality": schema_quality,
        "platformPresence": platform_presence,
        "citabilitySignals": {
            "answerQuality": answer_quality,
            "selfContained": self_contained,
            "structureReadability": structure_readability,
            "statisticsDensity": statistics_density,
            "uniqueness": uniqueness,
            "questionHeadings": question_headings,
            "numberSignals": number_count,
        },
        "eeatSignals": eeat_signals,
    }


def _score_page(page: ExtractedPage, project: dict) -> dict:
    return _geo_subscores(page, project)


def _dimension_diagnostics(page: ExtractedPage, score: dict, project: dict) -> dict:
    dimensions = score.get("dimensions") or {}
    business = score.get("businessType") or _detect_business_type(page)
    schema_quality = score.get("schemaQuality") or _schema_quality(page, business.get("type", "unknown"))
    platform_presence = score.get("platformPresence") or _platform_presence(page, schema_quality)
    citability = score.get("citabilitySignals") or {}
    eeat_signals = score.get("eeatSignals") or {}
    eeat_subscores = eeat_signals.get("subScores") or {}
    eeat_evidence = eeat_signals.get("evidence") or {}
    eeat_gaps = eeat_signals.get("gaps") or {}
    word_count = _word_count(page.body_text)
    internal_count, external_count = _link_counts(page)
    keyword_coverage = _keyword_coverage(page, project)
    diagnostics = {
        "aiCitability": {
            "summary": f"按答案块质量、自包含性、结构可读性、统计密度和独特性评估，可引用性子项：答案 {citability.get('answerQuality', '--')}，自包含 {citability.get('selfContained', '--')}，统计信号 {citability.get('numberSignals', 0)} 个。",
            "evidence": [f"疑问式标题：{citability.get('questionHeadings', 0)}", f"列表：{page.list_count}", f"表格：{page.table_count}", f"正文规模：{word_count}"],
            "issues": [],
            "opportunities": ["为每个核心 H2 增加 1-2 句答案优先段落，使用定义、对比、数据和来源说明，让内容可直接被 AI 摘取。"],
        },
        "brandAuthority": {
            "summary": f"品牌权威性基于客户/案例/资质/外部链接/第三方平台信号评估，当前外部链接 {external_count} 个。",
            "evidence": [f"外部链接：{external_count}", f"内部链接：{internal_count}", f"页脚业务信息长度：{_word_count(page.footer_text)}"],
            "issues": [],
            "opportunities": ["补充客户案例、权威媒体、白皮书、资质认证和可验证第三方来源，增强品牌实体识别。"],
        },
        "eeat": {
            "summary": (
                "按经验、专业性、权威性和可信度四个子项评估内容质量："
                f"经验 {eeat_subscores.get('experience', '--')}，"
                f"专业性 {eeat_subscores.get('expertise', '--')}，"
                f"权威性 {eeat_subscores.get('authoritativeness', '--')}，"
                f"可信度 {eeat_subscores.get('trustworthiness', '--')}。"
            ),
            "evidence": [
                item
                for group in eeat_evidence.values()
                for item in (group or [])
            ][:10] or [f"正文词数：{word_count}", f"关键词覆盖：{keyword_coverage.get('matched')}/{keyword_coverage.get('total')}"],
            "issues": [],
            "opportunities": [
                item
                for group in eeat_gaps.values()
                for item in (group or [])
            ] or ["保持作者/团队凭据、数据来源、更新时间、隐私安全与合规说明可验证。"],
        },
        "technicalGeo": {
            "summary": "检查 AI 爬虫可读的基础技术信号，包括 meta、canonical、viewport、OG、可索引正文与 noindex 风险。",
            "evidence": [
                f"title：{'有' if page.title else '缺失'}",
                f"description：{'有' if page.description else '缺失'}",
                f"canonical：{'有' if page.canonical else '缺失'}",
                f"robots meta：{page.robots_meta or '未检测到'}",
            ],
            "issues": [],
            "opportunities": ["后续可扩展 robots.txt、sitemap.xml、llms.txt 和 SSR 检查，形成完整技术 GEO 审计。"],
        },
        "schemaStructuredData": {
            "summary": (
                f"业务类型识别为 {business.get('label')}，推荐 Schema：{', '.join(schema_quality.get('recommended') or [])}。"
                f"属性完整度 {schema_quality.get('propertyScore', 0)}/100，"
                f"sameAs 实体关系 {(schema_quality.get('sameAs') or {}).get('score', 0)}/100。"
            ),
            "evidence": [
                f"已检测：{', '.join(schema_quality.get('found') or page.schema_types) or '暂无'}",
                f"缺失类型：{', '.join(schema_quality.get('missing') or []) or '暂无'}",
                f"sameAs URL：{len((schema_quality.get('sameAs') or {}).get('urls') or [])}",
            ] + [
                f"{item.get('type')} 缺失属性：{', '.join(item.get('missing') or []) or '暂无'}"
                for item in (schema_quality.get("propertyCompleteness") or [])
                if item.get("found")
            ][:4],
            "issues": [],
            "opportunities": [
                "按业务类型补齐 Organization、SoftwareApplication/Product/FAQPage 等 JSON-LD 的关键属性，而不只是声明 @type。",
                "用 sameAs 连接官网认证平台、百度百科、知乎、Bilibili、微博、公众号等可验证实体页。",
            ],
        },
        "platformOptimization": {
            "summary": (
                "检查面向当前智见已对接国内模型的平台存在信号："
                f"{'、'.join(model.get('label', '') for model in platform_presence.get('models', []))}。"
                f"平台存在性 {platform_presence.get('score', 0)}/100。"
            ),
            "evidence": [
                f"已发现平台：{', '.join(item.get('label', '') for item in platform_presence.get('platforms', []) if item.get('found')) or '暂无'}",
                f"缺失平台：{', '.join(item.get('label', '') for item in platform_presence.get('platforms', []) if not item.get('found')) or '暂无'}",
                f"外链样本数：{external_count}",
            ],
            "issues": [],
            "opportunities": [
                item.get("advice", "")
                for item in (platform_presence.get("modelAdvice") or [])
                if item.get("missingPlatforms")
            ][:5] or ["当前已接入模型对应的平台入口覆盖较好，继续保持官网、Schema sameAs 与平台内容互链。"],
        },
    }
    for key, item in diagnostics.items():
        value = dimensions.get(key)
        status = _score_status(value)
        if status == "weak":
            item["issues"].append("该 GEO 类别低于 60 分，属于优先优化项。")
        elif status == "watch":
            item["issues"].append("该 GEO 类别具备基础信号，但距离稳定被 AI 引用仍有差距。")
        item["score"] = value
        item["status"] = status
        item["label"] = DIMENSION_LABELS.get(key, key)
    return diagnostics


def _recommendations(page: ExtractedPage, score: dict, project: dict) -> list[dict]:
    recs: list[dict] = []
    dimensions = score.get("dimensions") or {}
    schema_quality = score.get("schemaQuality") or {}
    citability = score.get("citabilitySignals") or {}
    keyword_coverage = _keyword_coverage(page, project)
    technical_audit = score.get("technicalAudit") or {}
    if technical_audit:
        robots = technical_audit.get("robots") or {}
        llms = technical_audit.get("llms") or {}
        blocked_domestic = robots.get("blockedCritical") or []
        integrated_labels = "、".join(INTEGRATED_MODEL_LABELS.get(model, model) for model in _integrated_product_website_models())
        if blocked_domestic:
            recs.append(_recommendation(
                "geo_rec_ai_crawlers", "technicalGeo", "high", "high", "small",
                "放开国内关键 AI 爬虫的 robots.txt 访问",
                f"robots.txt 当前阻止了部分国内关键 AI/搜索爬虫，可能直接降低当前智见已对接模型（{integrated_labels}）的发现和引用概率。",
                [f"被阻止：{', '.join(item.get('name', '') for item in blocked_domestic)}", f"爬虫访问评分：{(technical_audit.get('score') or {}).get('crawlerAccess')}"],
                [
                    "在 robots.txt 中为 Baiduspider、Bytespider、DeepSeekBot、TencentCloudBot、Moonshot/Kimi、Sogou、360、AlibabaCloud 等与已接入模型检索链路相关的爬虫配置 Allow: /。",
                    "避免在通配 User-agent: * 中使用 Disallow: / 这类全站阻断规则，除非有明确的内容授权策略。",
                    "保留 Sitemap 指令，帮助国内 AI 爬虫发现产品页、文档页、案例页和 FAQ 页面。",
                ],
                9,
                "国内关键 AI 爬虫访问恢复为允许，爬虫访问评分提升到 90+。",
            ))
        if not llms.get("found"):
            recs.append(_recommendation(
                "geo_rec_llms_txt", "technicalGeo", "medium", "high", "medium",
                "发布面向 AI 理解的 llms.txt",
                "根目录未检测到 llms.txt，AI 系统缺少一份可快速理解网站结构、核心页面和关键事实的机器可读指南。",
                [f"llms.txt 状态码：{llms.get('statusCode') or '未获取'}"],
                [
                    "在 /llms.txt 增加 H1 网站名称、200 字以内业务描述、核心页面列表、关键事实和联系信息。",
                    "页面条目使用绝对 URL，并为每个页面写 10-30 个词的事实型描述，避免营销口号。",
                    "如果网站页面多，补充 /llms-full.txt，覆盖更多文档、案例、资源和产品页面。",
                ],
                6,
                "llms.txt 可访问且格式通过，llms 就绪度提升到 75+。",
            ))
    if dimensions.get("aiCitability", 0) < 75:
        recs.append(_recommendation(
            "geo_rec_answer_blocks", "aiCitability", "high", "high", "medium",
            "按答案优先结构重写核心内容块",
            "页面缺少可直接被 AI 摘取的定义、对比、数据和结论型段落。",
            [f"AI 可引用性：{dimensions.get('aiCitability')}", f"统计信号：{citability.get('numberSignals', 0)}", f"疑问式标题：{citability.get('questionHeadings', 0)}"],
            [
                "每个核心 H2 下第一段改成“X 是……，适合……，能解决……”的 80-160 字答案块。",
                "增加“什么是/如何选择/与竞品区别/适用场景”类问答标题。",
                "在答案块中加入具体数字、时间范围、客户规模或明确来源。",
            ],
            10,
            "AI 可引用性提升到 75+，至少 60% 的核心段落可脱离上下文独立理解。",
        ))
    if dimensions.get("brandAuthority", 0) < 75:
        recs.append(_recommendation(
            "geo_rec_brand_authority", "brandAuthority", "high", "high", "medium",
            "补强品牌权威与第三方实体信号",
            "当前品牌权威信号不足，国内大模型难以把品牌识别为可信实体。",
            [f"品牌权威性：{dimensions.get('brandAuthority')}"],
            [
                "新增客户案例、媒体报道、资质认证、白皮书或行业报告入口。",
                "在页脚和关于页明确公司主体、联系方式、备案、隐私和安全说明。",
                "将知乎、百度百科、Bilibili、微博、微信公众号等官方或权威入口加入 sameAs/外链策略。",
            ],
            9,
            "至少新增 3 类可验证权威信号，品牌权威性提升到 75+。",
        ))
    if dimensions.get("eeat", 0) < 75:
        eeat_signals = score.get("eeatSignals") or {}
        eeat_subscores = eeat_signals.get("subScores") or {}
        recs.append(_recommendation(
            "geo_rec_eeat", "eeat", "medium", "high", "medium",
            "补充 E-E-A-T 证据层",
            "页面更像产品介绍，缺少经验、专业性、权威性和可信度的可验证证据。",
            [
                f"E-E-A-T：{dimensions.get('eeat')}",
                f"经验/专业/权威/可信：{eeat_subscores.get('experience', '--')}/{eeat_subscores.get('expertise', '--')}/{eeat_subscores.get('authoritativeness', '--')}/{eeat_subscores.get('trustworthiness', '--')}",
                f"关键词覆盖：{keyword_coverage.get('matched')}/{keyword_coverage.get('total')}",
            ],
            [
                "为关键观点补充来源、方法论或客户实践背景。",
                "增加团队/作者/专家凭据和更新时间。",
                "补充数据安全、合规、隐私、服务 SLA 等可信度内容。",
            ],
            8,
            "E-E-A-T 评分提升到 75+，核心商业主张都有证据支撑。",
        ))
    if dimensions.get("technicalGeo", 0) < 75:
        recs.append(_recommendation(
            "geo_rec_technical", "technicalGeo", "high", "medium", "small",
            "补齐技术 GEO 基础信号",
            "技术可读性不足会降低 AI 爬虫抓取、索引和摘要生成稳定性。",
            [f"技术 GEO：{dimensions.get('technicalGeo')}", f"robots meta：{page.robots_meta or '未检测到'}"],
            [
                "补齐 title、description、canonical、lang、viewport、Open Graph/Twitter Card。",
                "确保页面有可索引 HTML 正文，不依赖纯客户端渲染。",
                "后续增加 robots.txt、sitemap.xml、llms.txt 自动检查。",
            ],
            7,
            "技术 GEO 达到 80+，没有 noindex 风险，基础 meta 完整。",
        ))
    if dimensions.get("schemaStructuredData", 0) < 80:
        property_gaps = [
            f"{item.get('type')} 缺失 {', '.join(item.get('missing') or [])}"
            for item in (schema_quality.get("propertyCompleteness") or [])
            if item.get("found") and item.get("missing")
        ][:3]
        same_as = schema_quality.get("sameAs") or {}
        recs.append(_recommendation(
            "geo_rec_schema", "schemaStructuredData", "medium", "medium", "medium",
            "按业务类型补齐 Schema 与 sameAs",
            "结构化数据不完整或关键属性不足，AI 难以稳定识别组织、产品、问答和第三方实体关系。",
            [
                f"架构评分：{dimensions.get('schemaStructuredData')}",
                f"缺失 Schema：{', '.join(schema_quality.get('missing') or []) or '暂无'}",
                f"属性完整度：{schema_quality.get('propertyScore', 0)}/100",
                f"sameAs：{same_as.get('score', 0)}/100",
            ] + property_gaps,
            [
                "至少添加 Organization 与 WebSite JSON-LD，并补齐 name、url、logo、publisher、sameAs 等关键属性。",
                "SaaS 产品页补 SoftwareApplication、FAQPage；电商补 Product、Offer；服务商补 Service，并填完整 offers/mainEntity/provider 等属性。",
                "用 sameAs 指向官网认证平台、百度百科、知乎、Bilibili、微博或公众号。",
            ],
            6,
            "推荐 Schema 缺失项清零，结构化数据评分提升到 80+。",
            [json.dumps(example.get("jsonLd"), ensure_ascii=False) for example in (schema_quality.get("examples") or [])[:2]],
        ))
    if dimensions.get("platformOptimization", 0) < 70:
        platform_presence = score.get("platformPresence") or {}
        integrated_labels = "、".join(model.get("label", "") for model in platform_presence.get("models", []))
        model_gaps = [
            f"{item.get('label')}：缺 {', '.join(item.get('missingPlatforms') or [])}"
            for item in (platform_presence.get("modelAdvice") or [])
            if item.get("missingPlatforms")
        ][:5]
        missing_platforms = [
            item.get("label", "")
            for item in (platform_presence.get("platforms") or [])
            if not item.get("found") and item.get("models")
        ][:5]
        recs.append(_recommendation(
            "geo_rec_cn_platforms", "platformOptimization", "medium", "high", "medium",
            "建立国内 AI 平台可识别的外部内容矩阵",
            f"官网缺少当前智见已对接模型（{integrated_labels}）可验证的平台信号，会影响这些模型的实体识别和引用概率。",
            [
                f"平台优化：{dimensions.get('platformOptimization')}",
                f"平台存在性：{platform_presence.get('score', 0)}/100",
                f"缺失平台：{', '.join(missing_platforms) or '暂无'}",
            ] + model_gaps,
            [
                "优先完善百度百科/百家号、知乎机构号或高质量问答、微信公众号、Bilibili 等当前已接入模型容易引用的公开内容入口。",
                "针对豆包、通义千问等通用检索链路，同步建设微博、小红书、抖音等可验证品牌入口；如果暂无运营资源，先从官网 sameAs 链接到已有官方号。",
                "将官网核心内容拆成适合平台分发的问答、教程、案例和对比内容。",
            ],
            7,
            "至少 3 个与当前已接入模型相关的平台出现可验证品牌内容，平台优化评分达到 70+。",
        ))
    return sorted(recs, key=lambda item: {"high": 0, "medium": 1, "low": 2}.get(item.get("priority"), 3))[:10]


def build_result_snapshot(
    html: str,
    final_url: str,
    input_snapshot: dict,
    crawler_diagnostics: dict | None = None,
) -> dict:
    page = extract_page(html)
    project = input_snapshot.get("project") or {}
    score = _score_page(page, project)
    technical_audit = (crawler_diagnostics or {}).get("technicalAudit")
    score = _apply_technical_audit_to_score(score, technical_audit if isinstance(technical_audit, dict) else None)
    recommendations = _recommendations(page, score, project)
    parsed = urlparse(final_url)
    keyword_coverage = _keyword_coverage(page, project)
    content_detail = _content_detail(page, final_url, project, crawler_diagnostics)
    dimension_diagnostics = _dimension_diagnostics(page, score, project)
    geo_audit = {
        "methodology": "geo-audit-cn",
        "weights": {
            "aiCitability": 0.25,
            "brandAuthority": 0.20,
            "eeat": 0.20,
            "technicalGeo": 0.15,
            "schemaStructuredData": 0.10,
            "platformOptimization": 0.10,
        },
        "businessType": score.get("businessType"),
        "schemaQuality": score.get("schemaQuality"),
        "platformPresence": score.get("platformPresence"),
        "citabilitySignals": score.get("citabilitySignals"),
        "eeatSignals": score.get("eeatSignals"),
        "technicalAudit": score.get("technicalAudit"),
    }
    return {
        "url": final_url,
        "summary": {
            "title": page.title,
            "description": page.description,
            "canonical": page.canonical,
            "lang": page.lang,
            "wordCount": _word_count(page.body_text),
            "headingsCount": len(page.headings),
            "schemaCount": len(page.schema_types),
        },
        "page": {
            "finalUrl": final_url,
            "title": page.title,
            "metaDescription": page.description,
            "description": page.description,
            "canonical": page.canonical,
            "lang": page.lang,
            "charset": page.charset,
            "viewport": page.viewport,
            "openGraph": page.open_graph,
            "headings": page.headings[:50],
            "h1": [heading.get("text", "") for heading in page.headings if heading.get("level") == 1],
            "h2": [heading.get("text", "") for heading in page.headings if heading.get("level") == 2],
            "wordCount": _word_count(page.body_text),
            "paragraphCount": len(page.paragraphs),
            "imageCount": page.image_count,
            "imagesMissingAlt": page.images_missing_alt,
            "schema": {"jsonLdTypes": page.schema_types, "rawCount": len(page.schema_types)},
            "schemaTypes": page.schema_types,
            "links": {
                "internal": len([link for link in page.links if not link.get("href", "").startswith("http")]),
                "external": len([link for link in page.links if link.get("href", "").startswith("http")]),
                "ctaCandidates": page.links[:10],
            },
        },
        "score": score,
        "product": {
            "score": score["dimensions"].get("brandAuthority", score["overall"]),
            "keywordCoverage": keyword_coverage,
        },
        "geoAudit": geo_audit,
        "technicalAudit": score.get("technicalAudit"),
        "contentDetail": content_detail,
        "dimensionDiagnostics": dimension_diagnostics,
        "recommendations": recommendations,
        "diagnostics": {
            "provider": (crawler_diagnostics or {}).get("provider", "native_fetch"),
            "domain": parsed.hostname,
            "crawler": crawler_diagnostics or {},
        },
    }


async def _log_event(db: AsyncSession, analysis_id: int, event_type: str, stage: str, payload: dict | None = None) -> None:
    db.add(ProductWebsiteEventLog(
        analysis_id=analysis_id,
        event_type=event_type,
        stage=stage,
        payload=payload or {},
    ))


async def run_product_website_analysis(analysis_id: int) -> None:
    async with async_session() as db:
        analysis = await db.get(ProductWebsiteAnalysis, analysis_id)
        if not analysis:
            return

        analysis.attempt_count += 1
        analysis.status = "fetching"
        analysis.stage = "fetching"
        analysis.started_at = analysis.started_at or datetime.now(timezone.utc)
        await _log_event(db, analysis.id, "stage_started", "fetching")
        await db.commit()

        start = time.perf_counter()
        input_snapshot = analysis.input_snapshot or {}
        options = input_snapshot.get("options") if isinstance(input_snapshot.get("options"), dict) else {}
        crawler_provider = options.get("crawler_provider")
        crawler = get_product_website_crawler(crawler_provider if isinstance(crawler_provider, str) else None)
        html = ""
        status_code = None
        final_url = analysis.target_url
        method = getattr(crawler, "method", "native_fetch")
        crawler_metadata: dict = {}
        error = None
        try:
            crawl_result = await crawler.fetch(analysis.target_url)
            html = crawl_result.html
            status_code = crawl_result.status_code
            final_url = crawl_result.final_url
            method = crawl_result.method
            crawler_metadata = crawl_result.metadata
        except Exception as exc:  # pragma: no cover - exact network errors vary
            error = str(exc)

        duration_ms = int((time.perf_counter() - start) * 1000)
        db.add(ProductWebsiteCrawlLog(
            analysis_id=analysis.id,
            target_url=analysis.target_url,
            final_url=final_url,
            method=method,
            status_code=status_code,
            content_length=len(html),
            duration_ms=duration_ms,
            error=error,
        ))

        if error or len(html) < 80:
            analysis.status = "failed"
            analysis.stage = "failed"
            analysis.error_code = "CRAWL_FAILED"
            analysis.error_message = error or "HTML content is too short"
            analysis.completed_at = datetime.now(timezone.utc)
            await _log_event(db, analysis.id, "analysis_failed", "fetching", {"error": analysis.error_message})
            await db.commit()
            return

        analysis.status = "extracting"
        analysis.stage = "extracting"
        await _log_event(db, analysis.id, "stage_started", "extracting")
        await db.commit()

        try:
            technical_audit = None
            try:
                technical_audit = await analyze_product_website_technical_access(final_url)
            except Exception as exc:  # pragma: no cover - auxiliary network failures vary
                technical_audit = {"error": str(exc)}
                await _log_event(db, analysis.id, "stage_failed", "technical_access", {"error": str(exc)})
            snapshot = build_result_snapshot(
                html,
                final_url,
                input_snapshot,
                {
                    "provider": method,
                    "durationMs": duration_ms,
                    "statusCode": status_code,
                    "metadata": crawler_metadata,
                    "technicalAudit": technical_audit,
                },
            )
            enable_ai_citation = options.get("enable_ai_citation")
            if enable_ai_citation is None:
                enable_ai_citation = settings.product_website_ai_citation_enabled
            if enable_ai_citation:
                analysis.status = "citation_checking"
                analysis.stage = "citation_checking"
                await _log_event(db, analysis.id, "stage_started", "citation_checking")
                await db.commit()
                try:
                    citation_check = await run_product_website_citation_check(
                        input_snapshot,
                        final_url,
                    )
                    snapshot["aiCitations"] = {
                        "enabled": citation_check.enabled,
                        "prompts": citation_check.prompts,
                        "platforms": citation_check.platforms,
                    }
                except Exception as exc:  # pragma: no cover - provider failures vary
                    snapshot["aiCitations"] = {
                        "enabled": True,
                        "prompts": [],
                        "platforms": [],
                        "error": str(exc),
                    }
                    await _log_event(db, analysis.id, "stage_failed", "citation_checking", {"error": str(exc)})
            analysis.final_url = final_url
            analysis.normalized_domain = urlparse(final_url).hostname
            analysis.result_snapshot = snapshot
            analysis.score_overall = snapshot["score"]["overall"]
            analysis.score_grade = snapshot["score"]["grade"]
            analysis.status = "completed"
            analysis.stage = "completed"
            analysis.completed_at = datetime.now(timezone.utc)
            db.add(ProductWebsiteStageRun(
                analysis_id=analysis.id,
                stage_name="analysis",
                status="completed",
                attempt_no=analysis.attempt_count,
                started_at=analysis.started_at,
                finished_at=analysis.completed_at,
                duration_ms=int((analysis.completed_at - analysis.started_at).total_seconds() * 1000) if analysis.started_at else None,
                output_snapshot={"score": snapshot["score"]},
            ))
            await _log_event(db, analysis.id, "analysis_completed", "completed", {"score": analysis.score_overall})
            await db.commit()
        except Exception as exc:
            analysis.status = "failed"
            analysis.stage = "failed"
            analysis.error_code = "ANALYSIS_FAILED"
            analysis.error_message = str(exc)
            analysis.completed_at = datetime.now(timezone.utc)
            await _log_event(db, analysis.id, "analysis_failed", "extracting", {"error": analysis.error_message})
            await db.commit()
