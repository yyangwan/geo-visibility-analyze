"""Discover citation media and archive small, verified previews."""

import asyncio
import logging
from io import BytesIO
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image, UnidentifiedImageError

from app.intelligence.blob import digest
from app.intelligence.sources import (
    SourceBlocked,
    SourceUnavailable,
    VisibleText,
    capture_source,
    normalized_url,
)

MAX_MEDIA = 24
MAX_PREVIEWS = 8
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
logger = logging.getLogger(__name__)


class MediaParser(VisibleText):
    def __init__(self, base):
        super().__init__()
        self.base = base
        self.stack = []
        self.rows = []
        self.metadata = {}

    def link(self, value):
        if not value or len(value) > 4096:
            return None
        try:
            return normalized_url(urljoin(self.base, value))
        except (SourceBlocked, ValueError):
            return None

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        attrs = dict(attrs)
        if tag == "meta":
            field = attrs.get("property")
            if field in {
                "og:image",
                "og:video",
                "og:video:url",
                "og:type",
            } and attrs.get("content"):
                self.metadata.setdefault(field, attrs["content"])
        if not any(
            node in {"header", "nav", "footer", "aside", "script", "style"}
            for node in self.stack
        ):
            in_body = "article" in self.stack or "main" in self.stack
            if tag == "img":
                candidate = (
                    attrs.get("data-src")
                    or attrs.get("data-original")
                    or attrs.get("src")
                )
                url = self.link(candidate)
                if not url and (
                    srcset := attrs.get("data-srcset") or attrs.get("srcset")
                ):
                    url = self.link(srcset.split(",", 1)[0].strip().split(" ", 1)[0])
                if url:
                    self.rows.append(
                        {
                            "kind": "image",
                            "url": url,
                            "preview_url": url,
                            "label": (attrs.get("alt") or "")[:500],
                            "in_body": in_body,
                            "part_index": len(self.parts) if not self.suppressed else None,
                        }
                    )
            elif tag == "video":
                self.rows.append(
                    {
                        "kind": "video",
                        "url": self.link(attrs.get("src")),
                        "preview_url": self.link(attrs.get("poster")),
                        "label": (attrs.get("title") or "")[:500],
                        "in_body": in_body,
                        "part_index": len(self.parts) if not self.suppressed else None,
                    }
                )
            elif (
                tag == "source"
                and self.stack
                and self.stack[-1] == "video"
                and self.rows
            ):
                if self.rows[-1]["kind"] == "video" and not self.rows[-1]["url"]:
                    self.rows[-1]["url"] = self.link(attrs.get("src"))
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        super().handle_endtag(tag)
        if tag in self.stack:
            while self.stack.pop() != tag:
                pass


def discover_media(raw, encoding, final_url, original_url=None, *, body_text=None):
    parser = MediaParser(final_url)
    try:
        parser.feed(raw.decode(encoding, errors="replace"))
    except (ValueError, LookupError, AssertionError):
        pass
    if not any(row["kind"] == "video" for row in parser.rows) and (
        (parser.metadata.get("og:type") or "").startswith("video")
        or parser.metadata.get("og:video")
        or parser.metadata.get("og:video:url")
    ):
        parser.rows.append(
            {
                "kind": "video",
                "url": parser.link(
                    parser.metadata.get("og:video:url")
                    or parser.metadata.get("og:video")
                ),
                "preview_url": parser.link(parser.metadata.get("og:image")),
                "label": "",
                "in_body": True,
            }
        )
    address = urlsplit(original_url or final_url)
    if (
        address.hostname in {"www.iesdouyin.com", "www.douyin.com"}
        and "/video/" in address.path
        and not any(row["kind"] == "video" for row in parser.rows)
    ):
        parser.rows.append(
            {
                "kind": "video",
                "url": None,
                "preview_url": parser.link(parser.metadata.get("og:image")),
                "label": "",
                "in_body": True,
            }
        )
    rows = [row for row in parser.rows if row["in_body"]] or parser.rows
    verified_order = body_text is not None and body_text == parser.text()
    for row in rows[:MAX_MEDIA]:
        part_index = row.pop("part_index", None)
        if verified_order and part_index is not None:
            prefix = parser.text(part_index)
            if body_text.startswith(prefix):
                row["text_offset"] = len(prefix)
    return [
        {
            "ordinal": number,
            **{key: val for key, val in row.items() if key != "in_body"},
            "page_url": original_url or final_url,
            "preview_status": "link_only",
        }
        for number, row in enumerate(rows[:MAX_MEDIA], 1)
    ]


def source_page_type(original_url, visible_text):
    address = urlsplit(original_url)
    if (
        address.hostname in {"www.iesdouyin.com", "www.douyin.com"}
        and "/video/" in address.path
    ):
        return "video"
    if address.hostname == "s.1688.com":
        return "search_listing"
    return "text_page" if visible_text.strip() else "empty_or_dynamic"


def small_preview(raw):
    with Image.open(BytesIO(raw), formats=("JPEG", "PNG", "WEBP", "GIF")) as source:
        if source.width * source.height > MAX_IMAGE_PIXELS:
            raise ValueError("Preview dimensions exceed budget")
        source.thumbnail((1280, 960))
        output = BytesIO()
        source.convert("RGB").save(output, "WEBP", quality=78)
        return output.getvalue()


def preview_key(project_id, sha256):
    return f"projects/{project_id}/citation-media/{sha256}.webp"


async def save_media_previews(blobs, project_id, rows):
    available = {}
    failures = {}
    try:
        async with asyncio.timeout(10):
            for url in dict.fromkeys(
                row["preview_url"] for row in rows if row["preview_url"]
            ):
                if len(available) >= MAX_PREVIEWS:
                    break
                available[url] = None
                try:
                    page = await capture_source(
                        url,
                        MAX_IMAGE_BYTES,
                        timeout_seconds=4,
                        max_redirects=3,
                        image_only=True,
                    )
                    preview = await asyncio.to_thread(small_preview, page.raw)
                except SourceBlocked:
                    failures[url] = "preview_blocked"
                    continue
                except SourceUnavailable as error:
                    failures[url] = error.code
                    continue
                except (httpx.HTTPError, TimeoutError):
                    failures[url] = "preview_transport_failed"
                    continue
                except (
                    OSError,
                    UnidentifiedImageError,
                    ValueError,
                    Image.DecompressionBombError,
                ):
                    failures[url] = "preview_decode_failed"
                    continue
                checksum = digest(preview)
                try:
                    await blobs.put_immutable(
                        preview_key(project_id, checksum), preview
                    )
                except Exception as error:
                    logger.warning(
                        "citation_preview_storage_failed class=%s", type(error).__name__
                    )
                    failures[url] = "preview_storage_failed"
                    continue
                available[url] = checksum
    except TimeoutError:
        pass
    for row in rows:
        url = row["preview_url"]
        if checksum := available.get(url):
            row.update(preview_status="saved", preview_sha256=checksum)
        elif url in available:
            row.update(
                preview_status="preview_unavailable",
                preview_reason=failures.get(url, "preview_budget"),
            )
        elif url:
            row["preview_reason"] = "preview_budget"
    return rows
