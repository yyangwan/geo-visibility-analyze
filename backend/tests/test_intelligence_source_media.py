from io import BytesIO

import pytest
from PIL import Image

from app.intelligence import source_media
from app.intelligence.blob import LocalBlobStore
from app.intelligence.sources import CapturedSource, VisibleText


def test_discovers_body_images_and_video_cover_without_inventing_direct_link():
    media = source_media.discover_media(
        b'<nav><img src="logo.png"></nav><article><img data-src="photo.png" alt="Product">'
        b'<video poster="poster.png"></video><img src="javascript:alert(1)"></article>',
        "utf-8",
        "https://example.com/a/notes",
    )
    assert [(item["kind"], item["url"], item["preview_url"]) for item in media] == [
        ("image", "https://example.com/a/photo.png", "https://example.com/a/photo.png"),
        ("video", None, "https://example.com/a/poster.png"),
    ]


def test_video_share_shell_retains_original_playback_page():
    original = "https://www.iesdouyin.com/share/video/123/"
    media = source_media.discover_media(
        b"<html><script></script></html>", "utf-8", "https://www.douyin.com/", original
    )
    assert media[0]["kind"] == "video"
    assert media[0]["page_url"] == original
    assert media[0]["url"] is None


def test_inline_positions_use_the_same_text_as_the_saved_body():
    raw = ('<article><p>🫘 前文</p><img src="photo.png"><p>中间</p>'
           '<video poster="poster.png"></video><p>后文</p></article>').encode()
    url = "https://example.com/notes"
    body = "🫘 前文\n中间\n后文"
    media = source_media.discover_media(raw, "utf-8", url, body_text=body)
    assert [entry["text_offset"] for entry in media] == [len("🫘 前文"), len("🫘 前文\n中间")]
    other_representation = source_media.discover_media(raw, "utf-8", url, body_text="其他来源")
    assert all("text_offset" not in entry for entry in other_representation)


def test_only_article_body_of_public_structured_articles_is_extracted():
    parser = VisibleText()
    parser.feed(
        '<script type="application/ld+json">[{"@type":"VideoObject","articleBody":"No"},'
        '{"@graph":[{"@type":"NewsArticle","articleBody":"Recovered article text"}]}]'
        "</script>"
    )
    assert parser.text() == ""
    assert parser.article_body() == "Recovered article text"


@pytest.mark.asyncio
async def test_preview_store_writes_only_one_derived_object_for_repeat_image(
    tmp_path, monkeypatch
):
    original = BytesIO()
    Image.new("RGB", (1600, 1200)).save(original, "PNG")
    calls = []

    async def fetch(url, limit, **options):
        assert options["image_only"]
        calls.append(url)
        return CapturedSource(url, 200, original.getvalue(), "", "unknown")

    monkeypatch.setattr(source_media, "capture_source", fetch)
    blobs = LocalBlobStore(tmp_path)
    items = source_media.discover_media(
        b'<article><img src="/image.png"><img src="/image.png"></article>',
        "utf-8",
        "https://example.com/article",
    )
    await source_media.save_media_previews(blobs, "project", items)
    assert calls == ["https://example.com/image.png"]
    assert [item["ordinal"] for item in items] == [1, 2]
    checksum = items[0]["preview_sha256"]
    assert items[1]["preview_sha256"] == checksum
    preview = await blobs.get(source_media.preview_key("project", checksum), checksum)
    with Image.open(BytesIO(preview)) as image:
        assert image.format == "WEBP"
        assert image.width <= 1280 and image.height <= 960
    assert len([path for path in tmp_path.rglob("*") if path.is_file()]) == 1
