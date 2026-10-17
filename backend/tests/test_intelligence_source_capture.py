"""Capture HTTP boundaries without replacing parsing or streaming safeguards."""

import httpx
import pytest

from app.intelligence import sources


class BodyStream(httpx.AsyncByteStream):
    def __init__(self, *chunks):
        self.chunks = chunks
        self.read_chunks = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.read_chunks += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.fixture
def capture_http(monkeypatch):
    def install(handler):
        resolved, requests = [], []

        async def resolve(host, port):
            resolved.append((host, port))
            return ["93.184.216.34"]

        def transport(host, address):
            assert address == "93.184.216.34"

            def respond(request):
                assert request.url.host == host
                requests.append(request)
                return handler(request)

            return httpx.MockTransport(respond)

        monkeypatch.setattr(sources, "resolve_public", resolve)
        monkeypatch.setattr(sources, "PinnedTransport", transport)
        return resolved, requests

    return install


@pytest.mark.asyncio
@pytest.mark.parametrize("mime", ["text/html", "application/xhtml+xml"])
async def test_html_capture_preserves_raw_unicode_and_extracts_visible_text(
    capture_http, mime
):
    raw = (
        "<header>hidden header</header><script>hidden script</script>"
        "<style>hidden style</style><nav>hidden nav</nav>"
        "<article><h1>正文 &amp; 标题</h1><p>第一段  内容</p>"
        "<p>最后一段</p></article><footer>hidden footer</footer>"
    ).encode()
    stream = BodyStream(raw[:31], raw[31:])
    resolved, requests = capture_http(
        lambda _: httpx.Response(
            200, headers={"Content-Type": f"{mime}; charset=utf-8"}, stream=stream
        )
    )

    captured = await sources.capture_source("https://example.com/article#section")

    assert captured.raw == raw
    assert captured.text == "正文 & 标题\n第一段 内容\n最后一段"
    assert captured.final_url == "https://example.com/article"
    assert captured.status == 200 and captured.completeness == "unknown"
    assert resolved == [("example.com", 443)]
    assert requests[0].headers["Accept-Encoding"] == "identity"
    assert stream.closed


@pytest.mark.asyncio
async def test_plain_text_at_exact_byte_limit_keeps_literal_markup(capture_http):
    raw = "  正文 <literal> &amp;\n末尾  ".encode()
    stream = BodyStream(raw[:4], raw[4:])
    capture_http(
        lambda _: httpx.Response(
            200, headers={"content-type": "text/plain; charset=utf-8"}, stream=stream
        )
    )

    captured = await sources.capture_source("http://example.com", byte_limit=len(raw))

    assert captured.raw == raw
    assert captured.text == "正文 <literal> &amp;\n末尾"
    assert captured.final_url == "http://example.com/"
    assert stream.read_chunks == 2 and stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers,code",
    [
        ({"content-type": "application/pdf"}, "unsupported_format"),
        ({}, "unsupported_format"),
        (
            {"content-type": "text/html", "content-encoding": "gzip"},
            "unsupported_encoding",
        ),
    ],
)
async def test_unsupported_representation_rejected_before_reading_body(
    capture_http, headers, code
):
    stream = BodyStream(b"untrusted body")
    capture_http(lambda _: httpx.Response(200, headers=headers, stream=stream))

    with pytest.raises(sources.SourceUnavailable) as error:
        await sources.capture_source("https://example.com/article")

    assert error.value.code == code and error.value.status == 200
    assert stream.read_chunks == 0 and stream.closed


@pytest.mark.asyncio
async def test_oversized_stream_stops_before_consuming_remaining_chunks(capture_http):
    stream = BodyStream(b"1234", b"56", b"must not be read")
    capture_http(
        lambda _: httpx.Response(
            200, headers={"content-type": "text/plain"}, stream=stream
        )
    )

    with pytest.raises(sources.SourceUnavailable) as error:
        await sources.capture_source("https://example.com/article", byte_limit=5)

    assert error.value.code == "source_too_large" and error.value.status == 200
    assert stream.read_chunks == 2 and stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mime,body",
    [("text/plain", b" \n\t"), ("text/html", b"<script>not article text</script>")],
)
async def test_empty_visible_content_is_unavailable(capture_http, mime, body):
    stream = BodyStream(body)
    capture_http(
        lambda _: httpx.Response(200, headers={"content-type": mime}, stream=stream)
    )

    with pytest.raises(sources.SourceUnavailable) as error:
        await sources.capture_source("https://example.com/article")

    assert error.value.code == "empty_content" and error.value.status == 200
    assert stream.closed


@pytest.mark.asyncio
async def test_relative_redirect_resolves_new_host_before_final_capture(capture_http):
    streams = []

    def respond(request):
        body = BodyStream(b"final article")
        streams.append(body)
        if request.url.host == "example.com":
            return httpx.Response(
                302, headers={"location": "//other.example/final#fragment"}, stream=body
            )
        return httpx.Response(200, headers={"content-type": "text/plain"}, stream=body)

    resolved, requests = capture_http(respond)
    captured = await sources.capture_source("https://example.com/start")

    assert resolved == [("example.com", 443), ("other.example", 443)]
    assert len(requests) == 2
    assert captured.final_url == "https://other.example/final"
    assert captured.text == "final article"
    assert streams[0].read_chunks == 0
    assert all(stream.closed for stream in streams)


@pytest.mark.asyncio
async def test_redirect_exhaustion_is_bounded_and_closes_every_response(capture_http):
    streams = []

    def redirect(_):
        stream = BodyStream(b"unused redirect body")
        streams.append(stream)
        return httpx.Response(302, headers={"location": "/again"}, stream=stream)

    resolved, requests = capture_http(redirect)
    with pytest.raises(sources.SourceUnavailable) as error:
        await sources.capture_source("https://example.com/start")

    assert error.value.code == "redirect_limit"
    assert len(requests) == len(resolved) == 6
    assert all(stream.closed and stream.read_chunks == 0 for stream in streams)


@pytest.mark.asyncio
async def test_redirect_without_location_is_unavailable(capture_http):
    capture_http(lambda _: httpx.Response(302, stream=BodyStream()))

    with pytest.raises(sources.SourceUnavailable) as error:
        await sources.capture_source("https://example.com/start")

    assert error.value.code == "invalid_redirect" and error.value.status == 302


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code",
    [(401, "blocked"), (403, "blocked"), (404, "not_found"), (429, "http_error"), (503, "http_error")],
)
async def test_http_failures_preserve_status_without_reading_body(capture_http, status, code):
    stream = BodyStream(b"private server failure details")
    capture_http(lambda _: httpx.Response(status, stream=stream))

    with pytest.raises(sources.SourceUnavailable) as error:
        await sources.capture_source("https://example.com/article")

    assert error.value.code == code and error.value.status == status
    assert "private server" not in str(error.value)
    assert stream.read_chunks == 0 and stream.closed
