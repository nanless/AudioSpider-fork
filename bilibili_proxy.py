"""Source-scoped proxy configuration for Bilibili requests.

The supported endpoint is intentionally limited to a loopback HTTP proxy so a
temporary SSH reverse tunnel cannot silently become a general remote proxy.
"""

from __future__ import annotations

import os
import re


BILIBILI_PROXY_ENV = "AUDIOSPIDER_BILIBILI_PROXY"
_LOOPBACK_HTTP_PROXY = re.compile(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})")


def get_bilibili_proxy() -> str | None:
    """Return the explicit Bilibili loopback proxy, or ``None`` when disabled."""

    value = os.environ.get(BILIBILI_PROXY_ENV, "")
    if not value:
        return None
    match = _LOOPBACK_HTTP_PROXY.fullmatch(value)
    if match is None:
        raise ValueError(
            f"{BILIBILI_PROXY_ENV} must be exactly "
            "http://127.0.0.1:<port> without credentials, path, query, or fragment"
        )
    port = int(match.group(1))
    if port > 65_535:
        raise ValueError(f"{BILIBILI_PROXY_ENV} port must be between 1 and 65535")
    return value


def proxy_request_kwargs(proxy: str | None) -> dict[str, str]:
    """Build aiohttp request kwargs without adding an ambient proxy fallback."""

    return {"proxy": proxy} if proxy else {}
