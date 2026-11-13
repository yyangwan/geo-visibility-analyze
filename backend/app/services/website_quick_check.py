from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from app.intelligence.sources import SourceBlocked, SourceUnavailable, capture_source
from app.services.product_website_analysis_service import extract_page

RULE_VERSION = "website-quick-check-v1"
CHECK_LABELS = {
    "access": "首页访问",
    "title": "网页标题",
    "content": "公开正文",
    "structured_data": "结构化数据",
}


def finding(key: str, status: str, evidence: str, action: str | None = None) -> dict:
    return {
        "key": key,
        "status": status,
        "evidence": evidence,
        **({"action": action} if action else {}),
    }


def result(checks: list[dict], host: str | None = None) -> dict:
    priority = next((item for item in checks if item.get("action")), None)
    return {
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "ruleVersion": RULE_VERSION,
        "checks": checks,
        **({"finalHost": host} if host else {}),
        **(
            {"priorityFinding": {**priority, "title": CHECK_LABELS[priority["key"]]}}
            if priority
            else {}
        ),
    }


def unavailable(evidence: str, *, issue: bool = False) -> dict:
    action = "在浏览器中确认官网首页地址与公开访问设置，再重试快检。"
    return result([
        finding("access", "issue" if issue else "unknown", evidence, action),
        *(finding(key, "unknown", "本次未取得可核对的首页内容。")
          for key in ("title", "content", "structured_data")),
    ])


async def check_website(url: str) -> dict:
    try:
        captured = await capture_source(
            url,
            byte_limit=2 * 1024 * 1024,
            timeout_seconds=10,
            max_redirects=3,
            html_only=True,
            user_agent="GeniLinkWebsiteCheck/1.0",
        )
    except SourceBlocked:
        return unavailable("该地址无法作为公开首页检查。")
    except SourceUnavailable as error:
        messages = {
            "blocked": "网站限制了本次访问。",
            "not_found": "提交的首页返回 404。",
            "unsupported_format": "首页没有返回可读取的 HTML。",
            "source_too_large": "首页内容超出快检可读取的大小。",
            "unsupported_encoding": "本次未能读取网站返回的压缩内容。",
        }
        return unavailable(
            messages.get(error.code, "本次未能获取首页。"),
            issue=error.code == "not_found",
        )
    except (TimeoutError, OSError, httpx.HTTPError):
        return unavailable("本次连接首页失败或超时。")

    page = extract_page(captured.raw.decode(captured.encoding, errors="replace"))
    excerpt = " ".join(captured.text.split())[:180]
    title = " ".join((page.title or "").split())[:180]
    types = "、".join(value for value in page.schema_types if value != "JSON-LD")[:180]
    has_text = len(excerpt) >= 30
    return result([
        finding("access", "pass", "本次成功获取公开首页 HTML。"),
        finding("content", "pass", excerpt) if has_text else finding(
            "content", "unknown", "公开 HTML 中可核对的正文较少。",
            "确认官网是否依赖脚本渲染，并让首页直接提供产品和适用对象的简短说明。",
        ),
        finding("title", "pass", title) if title else finding(
            "title", "issue", "本次获取的 HTML 中未找到网页标题。",
            "为首页填写一个能体现品牌和主要产品的网页标题。",
        ),
        finding("structured_data", "pass", types) if types else finding(
            "structured_data", "issue" if has_text else "unknown",
            "本次获取的 HTML 中未识别到带类型的 JSON-LD。",
            "可为首页添加与实际页面信息一致的 Organization 或 WebSite 结构化数据。"
            if has_text else None,
        ),
    ], urlsplit(captured.final_url).hostname)
