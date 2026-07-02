"""Product website visibility analysis API."""

import asyncio
import io
import json
from html import escape

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.access import get_product_website_analysis_for_project, require_project_scope
from app.api.auth import get_current_user
from app.api.schemas import (
    ProductWebsiteAnalysisOut,
    ProductWebsiteAnalyzeCreated,
    ProductWebsiteAnalyzeRequest,
)
from app.database import get_db
from app.models.models import ProductWebsiteAnalysis, ProductWebsiteEventLog
from app.services.product_website_analysis_service import run_product_website_analysis

router = APIRouter()


def _sse_event(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _product_website_report_html(analysis: ProductWebsiteAnalysis) -> str:
    result = analysis.result_snapshot or {}
    score = result.get("score") or {}
    page = result.get("page") or {}
    recommendations = result.get("recommendations") or []
    dimensions = score.get("dimensions") or {}
    diagnostics = result.get("diagnostics") or {}
    ai_citations = result.get("aiCitations") or {}

    dimension_rows = "".join(
        f"<tr><td>{escape(str(key))}</td><td>{escape(str(value))}</td></tr>"
        for key, value in dimensions.items()
    ) or "<tr><td colspan='2'>暂无维度评分</td></tr>"
    recommendation_rows = "".join(
        "<li>"
        f"<strong>{escape(str(item.get('title', '优化建议')))}</strong>"
        f"<p>{escape(str(item.get('detail', '')))}</p>"
        "</li>"
        for item in recommendations[:10]
        if isinstance(item, dict)
    ) or "<li>暂无优化建议</li>"
    ai_citation_empty_text = (
        "本次分析请求了真实 AI 引用检查，但后端全局开关未启用，因此未调用真实 AI 平台。"
        if ai_citations and not ai_citations.get("enabled")
        else "已启用真实 AI 引用检查，但本次没有返回平台结果。请检查平台配置或稍后重新分析。"
    )
    ai_citation_empty_text = (
        "本次分析请求了真实 AI 引用检查，但后端全局开关未启用，因此未调用真实 AI 平台。"
        if ai_citations and not ai_citations.get("enabled")
        else "已启用真实 AI 引用检查，但本次没有返回平台结果。请检查平台配置或稍后重新分析。"
    )
    ai_citation_empty_text = (
        "本次分析请求了真实 AI 引用检查，但后端全局开关未启用，因此未调用真实 AI 平台。"
        if ai_citations and not ai_citations.get("enabled")
        else "已启用真实 AI 引用检查，但本次没有返回平台结果。请检查平台配置或稍后重新分析。"
    )
    ai_citation_rows = "".join(
        "<tr>"
        f"<td>{escape(str(item.get('platform', '--')))}</td>"
        f"<td>{escape(str(item.get('status', '--')))}</td>"
        f"<td>{'是' if item.get('mentionsProduct') else '否'}</td>"
        f"<td>{escape(str(item.get('ownDomainCitationCount', 0)))}/{escape(str(item.get('citationCount', 0)))}</td>"
        "</tr>"
        for item in ai_citations.get("platforms", [])
        if isinstance(item, dict)
    ) or f"<tr><td colspan='4'>{escape(ai_citation_empty_text)}</td></tr>"
    schema_types = ", ".join(page.get("schemaTypes") or []) if isinstance(page.get("schemaTypes"), list) else ""

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>产品网站可见性分析报告 #{analysis.id}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #111827; margin: 32px; }}
    h1 {{ font-size: 26px; margin-bottom: 4px; }}
    h2 {{ font-size: 18px; margin-top: 28px; border-bottom: 1px solid #e5e7eb; padding-bottom: 8px; }}
    .muted {{ color: #6b7280; }}
    .score {{ font-size: 48px; font-weight: 700; margin: 16px 0; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
    td, th {{ border: 1px solid #e5e7eb; padding: 8px; text-align: left; }}
    li {{ margin-bottom: 10px; }}
    p {{ line-height: 1.6; }}
  </style>
</head>
<body>
  <h1>产品网站可见性分析报告</h1>
  <div class="muted">分析 ID：{analysis.id} · 目标 URL：{escape(analysis.target_url)}</div>
  <div class="score">{escape(str(analysis.score_overall or score.get("overall") or "--"))}</div>
  <div class="muted">等级：{escape(str(analysis.score_grade or score.get("grade") or "--"))} · 状态：{escape(analysis.status)}</div>

  <h2>页面摘要</h2>
  <table>
    <tr><th>标题</th><td>{escape(str(page.get("title") or "--"))}</td></tr>
    <tr><th>Meta 描述</th><td>{escape(str(page.get("metaDescription") or "--"))}</td></tr>
    <tr><th>正文词数</th><td>{escape(str(page.get("wordCount") or "--"))}</td></tr>
    <tr><th>结构化数据</th><td>{escape(schema_types or "--")}</td></tr>
  </table>

  <h2>评分维度</h2>
  <table>
    <tr><th>维度</th><th>得分</th></tr>
    {dimension_rows}
  </table>

  <h2>抓取诊断</h2>
  <table>
    <tr><th>抓取方式</th><td>{escape(str(diagnostics.get("provider") or "--"))}</td></tr>
    <tr><th>状态码</th><td>{escape(str((diagnostics.get("crawler") or {}).get("statusCode") or "--"))}</td></tr>
    <tr><th>耗时</th><td>{escape(str((diagnostics.get("crawler") or {}).get("durationMs") or "--"))} ms</td></tr>
  </table>

  <h2>真实 AI 引用</h2>
  <table>
    <tr><th>平台</th><th>状态</th><th>提及产品</th><th>自有域名引用</th></tr>
    {ai_citation_rows}
  </table>

  <h2>优化建议</h2>
  <ol>{recommendation_rows}</ol>
</body>
</html>"""


def _html_list(items, empty: str = "--") -> str:
    if not items:
        return f"<li>{escape(empty)}</li>"
    return "".join(f"<li>{escape(str(item))}</li>" for item in items)


def _html_badge(value) -> str:
    return f"<span class='badge'>{escape(str(value or '--'))}</span>"


def _html_dict(value: dict | None, empty: str = "--") -> str:
    if not value:
        return escape(empty)
    return escape(", ".join(f"{key}: {item}" for key, item in value.items()))


def _product_website_report_html(analysis: ProductWebsiteAnalysis) -> str:
    result = analysis.result_snapshot or {}
    score = result.get("score") or {}
    page = result.get("page") or {}
    geo_audit = result.get("geoAudit") or {}
    recommendations = result.get("recommendations") or []
    dimensions = score.get("dimensions") or {}
    diagnostics = result.get("diagnostics") or {}
    dimension_diagnostics = result.get("dimensionDiagnostics") or {}
    content_detail = result.get("contentDetail") or {}
    metadata = content_detail.get("metadata") or {}
    headings = content_detail.get("headings") or {}
    paragraphs = content_detail.get("paragraphs") or []
    links = content_detail.get("links") or {}
    images = content_detail.get("images") or {}
    schema = content_detail.get("schema") or page.get("schema") or {}
    keyword_coverage = content_detail.get("keywordCoverage") or (result.get("product") or {}).get("keywordCoverage") or {}
    ai_citations = result.get("aiCitations") or {}
    technical_audit = result.get("technicalAudit") or geo_audit.get("technicalAudit") or {}
    robots_audit = technical_audit.get("robots") or {}
    llms_audit = technical_audit.get("llms") or {}
    llms_full_audit = technical_audit.get("llmsFull") or {}
    eeat_signals = geo_audit.get("eeatSignals") or {}
    eeat_subscores = eeat_signals.get("subScores") or {}
    schema_quality = geo_audit.get("schemaQuality") or {}
    platform_presence = geo_audit.get("platformPresence") or {}

    dimension_rows = "".join(
        "<tr>"
        f"<td>{escape(str((dimension_diagnostics.get(key) or {}).get('label') or key))}</td>"
        f"<td>{escape(str(value))}</td>"
        f"<td>{escape(str((dimension_diagnostics.get(key) or {}).get('summary') or '--'))}</td>"
        f"<td><ul>{_html_list((dimension_diagnostics.get(key) or {}).get('issues'), '暂无明显问题')}</ul></td>"
        f"<td><ul>{_html_list((dimension_diagnostics.get(key) or {}).get('opportunities'), '暂无')}</ul></td>"
        "</tr>"
        for key, value in dimensions.items()
    ) or "<tr><td colspan='5'>暂无维度评分</td></tr>"

    recommendation_rows = "".join(
        "<section class='recommendation'>"
        f"<h3>{escape(str(item.get('title', '优化建议')))}</h3>"
        f"<div>{_html_badge(item.get('dimensionLabel') or item.get('dimension'))}{_html_badge(item.get('priority'))}{_html_badge('预期 +' + str(item.get('expectedLift')) if item.get('expectedLift') is not None else None)}</div>"
        f"<p><strong>问题：</strong>{escape(str(item.get('problem') or item.get('detail') or '--'))}</p>"
        f"<p><strong>证据：</strong></p><ul>{_html_list(item.get('evidence'), '暂无证据')}</ul>"
        f"<p><strong>行动项：</strong></p><ol>{_html_list(item.get('actions'), '暂无行动项')}</ol>"
        f"<p><strong>验收指标：</strong>{escape(str(item.get('successMetric') or '--'))}</p>"
        + (f"<p><strong>参考表达：</strong></p><ul>{_html_list(item.get('examples'))}</ul>" if item.get("examples") else "")
        + "</section>"
        for item in recommendations[:10]
        if isinstance(item, dict)
    ) or "<p>暂无优化建议</p>"

    paragraph_rows = "".join(
        "<tr>"
        f"<td>{index + 1}</td>"
        f"<td>{escape(str(item.get('wordCount') or '--'))}</td>"
        f"<td>{escape(str(item.get('text') or '--'))}</td>"
        "</tr>"
        for index, item in enumerate(paragraphs[:8])
        if isinstance(item, dict)
    ) or "<tr><td colspan='3'>暂无正文样本</td></tr>"

    ai_citation_empty_text = (
        "本次分析请求了真实 AI 引用检查，但后端全局开关未启用，因此未调用真实 AI 平台。"
        if ai_citations and not ai_citations.get("enabled")
        else "已启用真实 AI 引用检查，但本次没有返回平台结果。请检查平台配置或稍后重新分析。"
    )
    ai_citation_rows = "".join(
        "<tr>"
        f"<td>{escape(str(item.get('platform', '--')))}</td>"
        f"<td>{escape(str(item.get('status', '--')))}</td>"
        f"<td>{'是' if item.get('mentionsProduct') else '否'}</td>"
        f"<td>{escape(str(item.get('ownDomainCitationCount', 0)))}/{escape(str(item.get('citationCount', 0)))}</td>"
        "</tr>"
        for item in ai_citations.get("platforms", [])
        if isinstance(item, dict)
    ) or f"<tr><td colspan='4'>{escape(ai_citation_empty_text)}</td></tr>"

    technical_child_rows = "".join([
        "<tr>"
        "<th>robots.txt</th>"
        f"<td>{'已发现' if robots_audit.get('found') else '未发现'} · 国内爬虫 {escape(str(robots_audit.get('domesticScore', '--')))}/100 · 国际爬虫 {escape(str(robots_audit.get('internationalScore', '--')))}/100</td>"
        f"<td><ul>{_html_list([item.get('name') or item.get('id') for item in robots_audit.get('blockedCritical', [])], '暂无关键阻断')}</ul></td>"
        "</tr>",
        "<tr>"
        "<th>llms.txt</th>"
        f"<td>{'已发现' if llms_audit.get('found') else '未发现'} · 就绪度 {escape(str((llms_audit.get('scores') or {}).get('overall', '--')))}/100 · 条目 {escape(str(llms_audit.get('itemCount', '--')))}</td>"
        f"<td><ul>{_html_list(llms_audit.get('missingChecks'), '暂无缺失检查项')}</ul></td>"
        "</tr>",
        "<tr>"
        "<th>llms-full.txt</th>"
        f"<td>{'已发现' if llms_full_audit.get('found') else '未发现'} · 就绪度 {escape(str((llms_full_audit.get('scores') or {}).get('overall', '--')))}/100</td>"
        f"<td><ul>{_html_list(llms_full_audit.get('missingChecks'), '暂无缺失检查项')}</ul></td>"
        "</tr>",
    ])

    eeat_rows = "".join(
        "<tr>"
        f"<th>{escape(label)}</th>"
        f"<td>{escape(str(eeat_subscores.get(key, '--')))}</td>"
        f"<td><ul>{_html_list((eeat_signals.get('evidence') or {}).get(key), '暂无证据')}</ul></td>"
        f"<td><ul>{_html_list((eeat_signals.get('gaps') or {}).get(key), '暂无缺口')}</ul></td>"
        "</tr>"
        for key, label in [
            ("experience", "经验"),
            ("expertise", "专业性"),
            ("authoritativeness", "权威性"),
            ("trustworthiness", "可信度"),
        ]
    )

    schema_property_rows = "".join(
        "<tr>"
        f"<th>{escape(str(item.get('type') or '--'))}</th>"
        f"<td>{'已检测' if item.get('found') else '缺失'}</td>"
        f"<td>{escape(str(item.get('score', '--')))}</td>"
        f"<td><ul>{_html_list(item.get('missing'), '暂无缺失属性')}</ul></td>"
        "</tr>"
        for item in (schema_quality.get("propertyCompleteness") or [])[:8]
        if isinstance(item, dict)
    ) or "<tr><td colspan='4'>暂无 Schema 属性完整度数据</td></tr>"

    platform_rows = "".join(
        "<tr>"
        f"<th>{escape(str(item.get('label') or item.get('id') or '--'))}</th>"
        f"<td>{'已发现' if item.get('found') else '缺失'}</td>"
        f"<td>{escape('、'.join(item.get('models') or []) or '--')}</td>"
        f"<td><ul>{_html_list(item.get('evidence'), '暂无证据')}</ul></td>"
        "</tr>"
        for item in (platform_presence.get("platforms") or [])
        if isinstance(item, dict)
    ) or "<tr><td colspan='4'>暂无平台覆盖数据</td></tr>"

    model_advice_rows = "".join(
        "<tr>"
        f"<th>{escape(str(item.get('label') or item.get('model') or '--'))}</th>"
        f"<td>{escape(str(item.get('score', '--')))}</td>"
        f"<td>{escape('、'.join(item.get('missingPlatforms') or []) or '暂无')}</td>"
        f"<td>{escape(str(item.get('advice') or '--'))}</td>"
        "</tr>"
        for item in (platform_presence.get("modelAdvice") or [])
        if isinstance(item, dict)
    ) or "<tr><td colspan='4'>暂无模型建议</td></tr>"

    schema_types = schema.get("jsonLdTypes") or page.get("schemaTypes") or []
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>产品网站可见性分析报告 #{analysis.id}</title>
  <style>
    @page {{
      size: A4;
      margin: 18mm 16mm;
      @bottom-right {{
        content: "第 " counter(page) " 页 / 共 " counter(pages) " 页";
        color: #94a3b8;
        font-size: 9px;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: #f6f8fb;
      color: #111827;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans SC", "Microsoft YaHei", sans-serif;
      font-size: 12px;
      line-height: 1.65;
    }}
    .report {{
      background: #ffffff;
      border: 1px solid #e5e7eb;
      border-radius: 10px;
      overflow: hidden;
    }}
    .cover {{
      padding: 26px 28px 24px;
      color: #ffffff;
      background: #172033;
    }}
    .eyebrow {{
      color: #93c5fd;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.08em;
      margin-bottom: 10px;
      text-transform: uppercase;
    }}
    h1 {{
      margin: 0;
      font-size: 28px;
      line-height: 1.2;
      letter-spacing: 0;
    }}
    .subtitle {{
      margin-top: 10px;
      color: #cbd5e1;
      font-size: 12px;
      word-break: break-all;
    }}
    .hero-grid {{
      display: table;
      width: 100%;
      margin-top: 22px;
      table-layout: fixed;
    }}
    .hero-cell {{
      display: table-cell;
      padding-right: 14px;
      vertical-align: bottom;
    }}
    .score {{
      font-size: 56px;
      font-weight: 800;
      line-height: 1;
      color: #ffffff;
    }}
    .grade {{
      display: inline-block;
      margin-left: 10px;
      padding: 4px 9px;
      border: 1px solid rgba(255,255,255,0.25);
      border-radius: 999px;
      color: #dbeafe;
      font-size: 12px;
      font-weight: 700;
    }}
    .meta-line {{
      margin-top: 8px;
      color: #cbd5e1;
      font-size: 11px;
    }}
    .metrics {{
      display: table;
      width: 100%;
      padding: 18px 20px;
      table-layout: fixed;
      border-bottom: 1px solid #e5e7eb;
      background: #f8fafc;
    }}
    .metric {{
      display: table-cell;
      padding: 0 10px;
      border-right: 1px solid #e5e7eb;
      vertical-align: top;
    }}
    .metric:last-child {{ border-right: 0; }}
    .metric-label {{
      color: #64748b;
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.05em;
      text-transform: uppercase;
    }}
    .metric-value {{
      margin-top: 5px;
      color: #0f172a;
      font-size: 18px;
      font-weight: 800;
      word-break: break-word;
    }}
    .metric-note {{
      margin-top: 3px;
      color: #64748b;
      font-size: 10px;
    }}
    .section {{
      padding: 20px 24px;
      border-bottom: 1px solid #eef2f7;
      page-break-inside: avoid;
    }}
    .section.breakable {{ page-break-inside: auto; }}
    .section-title {{
      display: table;
      width: 100%;
      margin-bottom: 12px;
    }}
    h2 {{
      display: table-cell;
      margin: 0;
      color: #0f172a;
      font-size: 16px;
      line-height: 1.3;
      font-weight: 800;
    }}
    .section-kicker {{
      display: table-cell;
      color: #94a3b8;
      font-size: 10px;
      text-align: right;
      vertical-align: middle;
    }}
    table {{
      width: 100%;
      border-collapse: separate;
      border-spacing: 0;
      overflow: hidden;
      border: 1px solid #e5e7eb;
      border-radius: 8px;
      background: #ffffff;
    }}
    th {{
      width: 118px;
      background: #f8fafc;
      color: #475569;
      font-size: 11px;
      font-weight: 700;
      text-align: left;
    }}
    td, th {{
      padding: 9px 10px;
      border-right: 1px solid #e5e7eb;
      border-bottom: 1px solid #e5e7eb;
      vertical-align: top;
    }}
    tr:last-child td, tr:last-child th {{ border-bottom: 0; }}
    td:last-child, th:last-child {{ border-right: 0; }}
    tr:nth-child(even) td {{ background: #fbfdff; }}
    ul, ol {{
      margin: 0;
      padding-left: 16px;
    }}
    li {{ margin-bottom: 4px; }}
    p {{ margin: 0 0 8px; }}
    .badge {{
      display: inline-block;
      margin: 0 6px 6px 0;
      padding: 3px 8px;
      border: 1px solid #dbe3ef;
      border-radius: 999px;
      background: #f8fafc;
      color: #334155;
      font-size: 10px;
      font-weight: 700;
    }}
    .recommendation {{
      margin: 12px 0;
      padding: 14px 16px;
      border: 1px solid #dbe3ef;
      border-left: 4px solid #2563eb;
      border-radius: 8px;
      background: #ffffff;
      page-break-inside: avoid;
    }}
    .recommendation h3 {{
      margin: 0 0 8px;
      color: #0f172a;
      font-size: 14px;
      line-height: 1.4;
    }}
    .recommendation strong {{ color: #334155; }}
    .recommendation p {{ color: #334155; }}
    .recommendation ul, .recommendation ol {{ color: #475569; }}
    .footer-note {{
      padding: 12px 24px 18px;
      color: #94a3b8;
      font-size: 10px;
      background: #f8fafc;
    }}
    .word-break {{ word-break: break-all; }}
    .subsection-title {{
      margin: 14px 0 8px;
      color: #334155;
      font-size: 12px;
      font-weight: 800;
    }}
  </style>
</head>
<body>
  <main class="report">
    <section class="cover">
      <div class="eyebrow">Product Website Visibility Report</div>
      <h1>产品网站可见性分析报告</h1>
      <div class="subtitle">目标 URL：{escape(analysis.target_url)}</div>
      <div class="hero-grid">
        <div class="hero-cell">
          <div>
            <span class="score">{escape(str(analysis.score_overall or score.get("overall") or "--"))}</span>
            <span class="grade">{escape(str(analysis.score_grade or score.get("grade") or "--"))}</span>
          </div>
          <div class="meta-line">分析 ID：{analysis.id} · 状态：{escape(analysis.status)}</div>
        </div>
      </div>
    </section>

    <section class="metrics">
      <div class="metric">
        <div class="metric-label">正文规模</div>
        <div class="metric-value">{escape(str(page.get("wordCount") or "--"))}</div>
        <div class="metric-note">词 / 字符估算</div>
      </div>
      <div class="metric">
        <div class="metric-label">关键词覆盖</div>
        <div class="metric-value">{escape(str(keyword_coverage.get("matched", 0)))} / {escape(str(keyword_coverage.get("total", 0)))}</div>
        <div class="metric-note">产品关键词匹配</div>
      </div>
      <div class="metric">
        <div class="metric-label">结构化数据</div>
        <div class="metric-value">{escape(str(len(schema_types) if schema_types else 0))}</div>
        <div class="metric-note">{escape(", ".join(schema_types) if schema_types else "未检测到 Schema")}</div>
      </div>
      <div class="metric">
        <div class="metric-label">图片 Alt 缺失</div>
        <div class="metric-value">{escape(str(images.get("missingAlt", page.get("imagesMissingAlt", 0)) or 0))}</div>
        <div class="metric-note">共 {escape(str(images.get("total", page.get("imageCount", 0)) or 0))} 张图片</div>
      </div>
    </section>

    <section class="section">
      <div class="section-title"><h2>页面摘要</h2><div class="section-kicker">机器可读基础信息</div></div>
      <table>
        <tr><th>最终 URL</th><td class="word-break">{escape(str(metadata.get("finalUrl") or page.get("finalUrl") or analysis.target_url))}</td></tr>
        <tr><th>标题</th><td>{escape(str(metadata.get("title") or page.get("title") or "--"))}</td></tr>
        <tr><th>Meta 描述</th><td>{escape(str(metadata.get("description") or page.get("metaDescription") or page.get("description") or "--"))}</td></tr>
        <tr><th>Canonical</th><td class="word-break">{escape(str(metadata.get("canonical") or page.get("canonical") or "--"))}</td></tr>
        <tr><th>语言 / Viewport</th><td>{escape(str(metadata.get("lang") or page.get("lang") or "--"))} / {escape(str(metadata.get("viewport") or page.get("viewport") or "--"))}</td></tr>
        <tr><th>正文 / 段落</th><td>{escape(str(page.get("wordCount") or "--"))} / {escape(str(page.get("paragraphCount") or "--"))}</td></tr>
        <tr><th>关键词覆盖</th><td>{escape(str(keyword_coverage.get("matched", 0)))} / {escape(str(keyword_coverage.get("total", 0)))}，缺失：{escape(", ".join(keyword_coverage.get("missing") or []) or "暂无")}</td></tr>
      </table>
    </section>

    <section class="section breakable">
      <div class="section-title"><h2>详细内容证据</h2><div class="section-kicker">页面结构与正文样本</div></div>
      <table>
        <tr><th>H1</th><td><ul>{_html_list(headings.get("h1"), "暂无 H1")}</ul></td></tr>
        <tr><th>H2</th><td><ul>{_html_list(headings.get("h2"), "暂无 H2")}</ul></td></tr>
        <tr><th>链接</th><td>内部 {escape(str(links.get("internalCount", "--")))}，外部 {escape(str(links.get("externalCount", "--")))}，CTA 候选 {escape(str(len(links.get("ctaCandidates") or [])))}</td></tr>
        <tr><th>图片</th><td>共 {escape(str(images.get("total", 0)))} 张，缺失 alt {escape(str(images.get("missingAlt", 0)))} 张，缺失率 {escape(str(images.get("missingAltRate", 0)))}%</td></tr>
      </table>
      <br>
      <table>
        <tr><th>#</th><th>词数</th><th>正文样本</th></tr>
        {paragraph_rows}
      </table>
    </section>

    <section class="section breakable">
      <div class="section-title"><h2>GEO 子流程审计</h2><div class="section-kicker">robots / llms / E-E-A-T / Schema / 平台覆盖</div></div>

      <div class="subsection-title">技术 GEO：爬虫与 llms.txt</div>
      <table>
        <tr><th>子流程</th><th>状态与评分</th><th>缺口</th></tr>
        {technical_child_rows}
      </table>

      <div class="subsection-title">内容 E-E-A-T 证据</div>
      <table>
        <tr><th>子项</th><th>得分</th><th>证据</th><th>缺口</th></tr>
        {eeat_rows}
      </table>

      <div class="subsection-title">Schema 属性完整度与 sameAs</div>
      <table>
        <tr><th>指标</th><td colspan="3">属性完整度 {escape(str(schema_quality.get("propertyScore", "--")))}/100 · sameAs {(schema_quality.get("sameAs") or {}).get("score", "--")}/100 · 国内平台 URL {len((schema_quality.get("sameAs") or {}).get("domesticUrls") or [])}</td></tr>
        <tr><th>类型</th><th>状态</th><th>属性分</th><th>缺失属性</th></tr>
        {schema_property_rows}
      </table>

      <div class="subsection-title">当前智见已接入模型的平台覆盖</div>
      <table>
        <tr><th>模型范围</th><td colspan="3">{escape("、".join(model.get("label", "") for model in platform_presence.get("models", [])) or "--")}</td></tr>
        <tr><th>平台覆盖分</th><td colspan="3">{escape(str(platform_presence.get("score", "--")))}/100</td></tr>
        <tr><th>平台</th><th>状态</th><th>关联模型</th><th>证据</th></tr>
        {platform_rows}
      </table>
      <br>
      <table>
        <tr><th>模型</th><th>得分</th><th>缺失平台</th><th>建议</th></tr>
        {model_advice_rows}
      </table>
    </section>

    <section class="section breakable">
      <div class="section-title"><h2>维度诊断</h2><div class="section-kicker">评分依据与提升机会</div></div>
      <table>
        <tr><th>维度</th><th>得分</th><th>诊断摘要</th><th>发现问题</th><th>提升机会</th></tr>
        {dimension_rows}
      </table>
    </section>

    <section class="section">
      <div class="section-title"><h2>抓取诊断</h2><div class="section-kicker">数据采集质量</div></div>
      <table>
        <tr><th>抓取方式</th><td>{escape(str(diagnostics.get("provider") or "--"))}</td></tr>
        <tr><th>状态码</th><td>{escape(str((diagnostics.get("crawler") or {}).get("statusCode") or "--"))}</td></tr>
        <tr><th>耗时</th><td>{escape(str((diagnostics.get("crawler") or {}).get("durationMs") or "--"))} ms</td></tr>
      </table>
    </section>

    <section class="section">
      <div class="section-title"><h2>真实 AI 引用</h2><div class="section-kicker">平台实际回答引用表现</div></div>
      <table>
        <tr><th>平台</th><th>状态</th><th>提及产品</th><th>自有域名引用</th></tr>
        {ai_citation_rows}
      </table>
    </section>

    <section class="section breakable">
      <div class="section-title"><h2>优化建议</h2><div class="section-kicker">可执行动作与验收指标</div></div>
      {recommendation_rows}
    </section>

    <div class="footer-note">本报告由智链产品网站可见性分析生成。评分和建议基于当前抓取内容、项目配置与真实 AI 引用检查结果。</div>
  </main>
</body>
</html>"""


@router.post("/analyze", response_model=ProductWebsiteAnalyzeCreated)
async def create_product_website_analysis(
    data: ProductWebsiteAnalyzeRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_project_scope(current_user, data.project_id)
    analysis = ProductWebsiteAnalysis(
        workspace_id=data.workspace_id,
        project_id=data.project_id,
        target_url=data.target_url,
        status="queued",
        stage="queued",
        input_snapshot={
            "project": data.project.model_dump(),
            "brands": [brand.model_dump() for brand in data.brands],
            "options": data.options.model_dump(exclude_none=True),
        },
    )
    db.add(analysis)
    await db.commit()
    await db.refresh(analysis)

    asyncio.create_task(run_product_website_analysis(analysis.id))
    return ProductWebsiteAnalyzeCreated(
        id=analysis.id,
        analysisId=analysis.id,
        status=analysis.status,
        stage=analysis.stage,
    )


@router.get("/projects/{project_id}/trends")
async def product_website_trends(
    project_id: str,
    range: str = Query("30d"),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_project_scope(current_user, project_id)
    result = await db.execute(
        select(ProductWebsiteAnalysis)
        .where(ProductWebsiteAnalysis.project_id == project_id)
        .order_by(ProductWebsiteAnalysis.created_at.desc())
        .limit(90)
    )
    analyses = list(reversed(result.scalars().all()))
    points = [
        {
            "analysisId": row.id,
            "date": row.completed_at.isoformat() if row.completed_at else row.created_at.isoformat(),
            "overall": row.score_overall,
            "grade": row.score_grade,
            "dimensions": (row.result_snapshot or {}).get("score", {}).get("dimensions", {}),
            "status": row.status,
        }
        for row in analyses
    ]
    completed = [point for point in points if isinstance(point.get("overall"), (int, float))]
    current = completed[-1]["overall"] if completed else None
    previous = completed[-2]["overall"] if len(completed) >= 2 else None
    return {
        "projectId": project_id,
        "range": range,
        "points": points,
        "summary": {
            "currentScore": current,
            "delta": round(current - previous, 1) if current is not None and previous is not None else None,
        },
    }


@router.get("/{analysis_id}", response_model=ProductWebsiteAnalysisOut)
async def get_product_website_analysis(
    analysis_id: int,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await get_product_website_analysis_for_project(db, current_user, analysis_id)


@router.post("/{analysis_id}/retry", response_model=ProductWebsiteAnalyzeCreated)
async def retry_product_website_analysis(
    analysis_id: int,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    analysis = await get_product_website_analysis_for_project(db, current_user, analysis_id)
    analysis.status = "queued"
    analysis.stage = "queued"
    analysis.error_code = None
    analysis.error_message = None
    analysis.completed_at = None
    await db.commit()

    asyncio.create_task(run_product_website_analysis(analysis.id))
    return ProductWebsiteAnalyzeCreated(
        id=analysis.id,
        analysisId=analysis.id,
        status=analysis.status,
        stage=analysis.stage,
    )


@router.get("/{analysis_id}/events")
async def product_website_events(
    analysis_id: int,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    analysis = await get_product_website_analysis_for_project(db, current_user, analysis_id)

    async def _stream():
        yield _sse_event("analysis_snapshot", {
            "id": analysis.id,
            "status": analysis.status,
            "stage": analysis.stage,
            "result": analysis.result_snapshot,
        })
        result = await db.execute(
            select(ProductWebsiteEventLog)
            .where(ProductWebsiteEventLog.analysis_id == analysis_id)
            .order_by(ProductWebsiteEventLog.created_at.asc())
        )
        for event in result.scalars().all():
            yield _sse_event(event.event_type, {
                "stage": event.stage,
                "payload": event.payload or {},
                "created_at": event.created_at.isoformat() if event.created_at else None,
            })
        if analysis.status in {"completed", "partial", "failed"}:
            yield _sse_event("analysis_done", {"status": analysis.status})

    return StreamingResponse(_stream(), media_type="text/event-stream")


@router.get("/{analysis_id}/pdf")
async def export_product_website_pdf(
    analysis_id: int,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    analysis = await get_product_website_analysis_for_project(db, current_user, analysis_id)
    html = _product_website_report_html(analysis)

    try:
        from weasyprint import HTML

        pdf_bytes = HTML(string=html).write_pdf()
    except Exception:
        return StreamingResponse(
            io.BytesIO(html.encode("utf-8")),
            media_type="text/html",
            headers={"Content-Disposition": f"attachment; filename=product-website-analysis-{analysis_id}.html"},
        )

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=product-website-analysis-{analysis_id}.pdf"},
    )
