# Copyright (c) 2026 AudioSpider Contributors.

"""Network boundary helpers for untrusted feed and media URLs."""

import asyncio
import ipaddress
import socket
from contextlib import asynccontextmanager
from urllib.parse import urljoin, urlsplit

import aiohttp


class UnsafeURLError(ValueError):
    """Raised when a URL is not safe for server-side fetching."""


class TooManyRedirectsError(UnsafeURLError):
    """Raised when a request exceeds the configured redirect budget."""


def _validate_url_shape(url: str) -> tuple[str, int]:
    if not isinstance(url, str) or not url.strip():
        raise UnsafeURLError("URL is empty")
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeURLError(f"unsupported URL scheme: {parsed.scheme or '<empty>'}")
    if not parsed.hostname:
        raise UnsafeURLError("URL has no hostname")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeURLError("credentials in URLs are not allowed")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeURLError(f"invalid URL port: {exc}") from exc
    return parsed.hostname, port


def _is_public_address(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


async def validate_public_http_url(url: str, *, allow_private: bool = False) -> str:
    """Validate scheme and every DNS answer before a server-side request."""

    host, port = _validate_url_shape(url)
    if allow_private:
        return url

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise UnsafeURLError(f"non-public destination is blocked: {literal}")
        return url

    try:
        answers = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            port,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise UnsafeURLError(f"hostname resolution failed: {host}: {exc}") from exc

    addresses = {answer[4][0] for answer in answers}
    if not addresses:
        raise UnsafeURLError(f"hostname has no addresses: {host}")
    blocked = sorted(address for address in addresses if not _is_public_address(address))
    if blocked:
        raise UnsafeURLError(
            f"hostname resolves to a non-public destination: {host}: {', '.join(blocked)}"
        )
    return url


@asynccontextmanager
async def safe_get(
    session: aiohttp.ClientSession,
    url: str,
    *,
    allow_private: bool = False,
    max_redirects: int = 5,
    **kwargs,
):
    """GET a URL while validating the initial and every redirected target."""

    current = url
    response = None
    for hop in range(max_redirects + 1):
        await validate_public_http_url(current, allow_private=allow_private)
        response = await session.get(current, allow_redirects=False, **kwargs)
        if response.status not in {301, 302, 303, 307, 308}:
            try:
                yield response
            finally:
                response.release()
            return

        location = response.headers.get("Location", "")
        response.release()
        if not location:
            raise UnsafeURLError("redirect response has no Location header")
        if hop >= max_redirects:
            raise TooManyRedirectsError(f"too many redirects for {url}")
        current = urljoin(current, location)

    raise TooManyRedirectsError(f"too many redirects for {url}")
