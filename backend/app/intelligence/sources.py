"""Public-IP-pinned HTTP capture. The renderer is deliberately a separate boundary."""

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx


class SourceBlocked(ValueError):
    pass


class SourceUnavailable(RuntimeError):
    def __init__(self, code: str, status: int | None = None):
        super().__init__(code)
        self.code, self.status = code, status


def normalized_url(value: str) -> str:
    if len(value) > 4096 or any(c in value for c in ("\\", "\r", "\n", "\x00")):
        raise SourceBlocked("invalid_url")
    try:
        url = urlsplit(value)
        host = (url.hostname or "").encode("idna").decode("ascii").lower()
        port = url.port
    except (ValueError, UnicodeError):
        raise SourceBlocked("invalid_url") from None
    if (
        url.scheme not in ("http", "https")
        or not host
        or url.username is not None
        or url.password is not None
    ):
        raise SourceBlocked("invalid_url")
    if port not in (None, 80 if url.scheme == "http" else 443):
        raise SourceBlocked("unsupported_port")
    authority = f"[{host}]" if ":" in host else host
    return urlunsplit((url.scheme, authority, url.path or "/", url.query, ""))


def public_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast and not address.is_unspecified


async def resolve_public(host: str, port: int) -> list[str]:
    # Reject mixed public/private DNS answers rather than choosing a convenient one.
    answers = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    )
    addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
    if not addresses or not all(public_ip(address) for address in addresses):
        raise SourceBlocked("non_public_destination")
    return addresses


class PinnedTransport(httpx.AsyncHTTPTransport):
    def __init__(self, host: str, address: str):
        super().__init__(verify=True, retries=0)
        self.host, self.address = host, address

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        original = request.url
        request.url = original.copy_with(host=self.address)
        request.headers["Host"] = f"[{self.host}]" if ":" in self.host else self.host
        request.extensions["sni_hostname"] = self.host
        return await super().handle_async_request(request)


@dataclass(frozen=True)
class CapturedSource:
    final_url: str
    status: int
    raw: bytes
    text: str
    completeness: str
    extractor_version: str = "html-visible-v1"


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.suppressed: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg", "nav", "footer", "header"):
            self.suppressed.append(tag)
        if not self.suppressed and tag in (
            "p",
            "div",
            "h1",
            "h2",
            "h3",
            "li",
            "br",
            "section",
            "article",
        ):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.suppressed and tag == self.suppressed[-1]:
            self.suppressed.pop()
        elif not self.suppressed and tag in ("p", "div", "li", "section", "article"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.suppressed:
            self.parts.append(data)

    def text(self):
        return "\n".join(
            line
            for line in (
                re.sub(r"[ \t]+", " ", line).strip()
                for line in "".join(self.parts).splitlines()
            )
            if line
        )


async def capture_source(url: str, byte_limit: int = 4 * 1024 * 1024) -> CapturedSource:
    async with asyncio.timeout(30):
        current = normalized_url(url)
        for _ in range(6):
            parsed = urlsplit(current)
            addresses = await resolve_public(
                parsed.hostname, 443 if parsed.scheme == "https" else 80
            )
            transport = PinnedTransport(parsed.hostname, addresses[0])
            # No proxy, cookies, credentials, automatic redirect, or environment trust.
            async with httpx.AsyncClient(
                transport=transport,
                trust_env=False,
                follow_redirects=False,
                timeout=10,
                headers={
                    "Accept-Encoding": "identity",
                    "User-Agent": "GenilinkCitationBot/1.0",
                },
            ) as client:
                async with client.stream("GET", current) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise SourceUnavailable(
                                "invalid_redirect", response.status_code
                            )
                        current = normalized_url(urljoin(current, location))
                        continue
                    if response.status_code != 200:
                        code = (
                            "blocked"
                            if response.status_code in (401, 403, 402)
                            else "not_found"
                            if response.status_code == 404
                            else "http_error"
                        )
                        raise SourceUnavailable(code, response.status_code)
                    mime = (
                        response.headers.get("content-type", "")
                        .split(";")[0]
                        .strip()
                        .lower()
                    )
                    if mime not in ("text/html", "application/xhtml+xml", "text/plain"):
                        raise SourceUnavailable("unsupported_format", 200)
                    if response.headers.get(
                        "content-encoding", "identity"
                    ).lower() not in ("", "identity"):
                        raise SourceUnavailable("unsupported_encoding", 200)
                    data = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(data) + len(chunk) > byte_limit:
                            raise SourceUnavailable("source_too_large", 200)
                        data.extend(chunk)
                    raw = bytes(data)
                    decoded = raw.decode(response.encoding or "utf-8", errors="replace")
                    parser = VisibleText()
                    parser.feed(decoded)
                    text = decoded.strip() if mime == "text/plain" else parser.text()
                    if not text:
                        raise SourceUnavailable("empty_content", 200)
                    # A successful HTTP request does not prove full article coverage.
                    return CapturedSource(current, 200, raw, text, "unknown")
        raise SourceUnavailable("redirect_limit")
