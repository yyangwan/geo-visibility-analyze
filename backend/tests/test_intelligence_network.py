import socket
from types import SimpleNamespace

import httpx
import pytest

from app.intelligence import sources


@pytest.mark.asyncio
async def test_actual_connection_uses_pinned_ip_and_original_tls_hostname(monkeypatch):
    observed = {}

    async def handle(self, request):
        observed.update(
            host=request.url.host,
            header=request.headers["Host"],
            sni=request.extensions["sni_hostname"],
        )
        return httpx.Response(200)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    async with sources.PinnedTransport("example.com", "93.184.216.34") as transport:
        await transport.handle_async_request(
            httpx.Request("GET", "https://example.com/article")
        )
    assert observed == {
        "host": "93.184.216.34",
        "header": "example.com",
        "sni": "example.com",
    }


@pytest.mark.asyncio
async def test_mixed_public_private_dns_is_rejected(monkeypatch):
    import asyncio

    loop = asyncio.get_running_loop()

    async def resolve(*_, **__):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
            for ip in ("93.184.216.34", "127.0.0.1")
        ]

    monkeypatch.setattr(loop, "getaddrinfo", resolve)
    with pytest.raises(sources.SourceBlocked):
        await sources.resolve_public("example.com", 443)


@pytest.mark.asyncio
async def test_redirect_to_metadata_is_validated_before_connect(monkeypatch):
    connected = []

    async def resolve(host, _):
        if host == "169.254.169.254":
            raise sources.SourceBlocked("non_public_destination")
        return ["93.184.216.34"]

    def transport(host, _):
        connected.append(host)
        return httpx.MockTransport(
            lambda request: httpx.Response(
                302, headers={"location": "http://169.254.169.254/latest"}
            )
        )

    monkeypatch.setattr(sources, "resolve_public", resolve)
    monkeypatch.setattr(sources, "PinnedTransport", transport)
    with pytest.raises(sources.SourceBlocked):
        await sources.capture_source("https://example.com/article")
    assert connected == ["example.com"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "acl,status",
    [("public-read", None), ("private", "Enabled"), ("private", "Suspended")],
)
async def test_storage_refuses_public_or_versioned_bucket(acl, status):
    from app.intelligence.blob import OssBlobStore

    bucket = SimpleNamespace(
        get_bucket_acl=lambda: SimpleNamespace(acl=acl),
        get_bucket_versioning=lambda: SimpleNamespace(status=status),
    )
    with pytest.raises(ValueError):
        await OssBlobStore(bucket).verify_configuration()
