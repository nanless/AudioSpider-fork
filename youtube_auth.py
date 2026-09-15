"""Explicit, in-memory YouTube cookie gate.

The payload is read from stdin by ``main.py`` only after the operator enables
the matching CLI gate. It is never accepted from argv, a cookie file, or a
browser profile on the server.
"""

from __future__ import annotations

import base64
import json
import re
import time
from http.cookiejar import Cookie
from typing import Any, BinaryIO
from urllib.parse import quote, quote_plus


MAX_PAYLOAD_BYTES = 64 * 1024
MAX_COOKIE_COUNT = 64
MAX_COOKIE_VALUE_BYTES = 4096
COOKIE_NAME = re.compile(r"^[A-Za-z0-9_!#$%&'*+.^`|~-]{1,128}$")
ALLOWED_DOMAINS = {".youtube.com", "youtube.com", "www.youtube.com"}
ALLOWED_NAMES = {
    "APISID",
    "HSID",
    "LOGIN_INFO",
    "SAPISID",
    "SID",
    "SIDCC",
    "SSID",
    "VISITOR_INFO1_LIVE",
    "VISITOR_PRIVACY_METADATA",
    "YSC",
    "__Secure-1PAPISID",
    "__Secure-1PSID",
    "__Secure-1PSIDCC",
    "__Secure-1PSIDTS",
    "__Secure-3PAPISID",
    "__Secure-3PSID",
    "__Secure-3PSIDCC",
    "__Secure-3PSIDTS",
}
SAPISID_NAMES = {"SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID"}


def read_cookie_payload(stream: BinaryIO) -> list[dict[str, Any]]:
    """Read and validate one bounded JSON cookie array from a binary stream."""

    raw = stream.read(MAX_PAYLOAD_BYTES + 1)
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise ValueError("YouTube cookie payload exceeds the byte limit")
    if not raw:
        raise ValueError("YouTube cookie payload is empty")
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("YouTube cookie payload must be UTF-8 JSON") from exc
    return validate_cookie_payload(decoded)


def validate_cookie_payload(payload: str) -> list[dict[str, Any]]:
    """Return normalized cookies without ever including values in errors."""

    if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError("YouTube cookie payload exceeds the byte limit")
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("YouTube cookie payload is invalid JSON") from exc
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_COOKIE_COUNT:
        raise ValueError("YouTube cookie payload must contain 1..64 entries")

    normalized: list[dict[str, Any]] = []
    now = int(time.time())
    seen: set[tuple[str, str, str]] = set()
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise ValueError(f"YouTube cookie entry {index} must be an object")
        name = entry.get("name")
        cookie_value = entry.get("value")
        domain = entry.get("domain")
        path = entry.get("path", "/")
        secure = entry.get("secure", True)
        expires = entry.get("expires")
        if not isinstance(name, str) or not COOKIE_NAME.fullmatch(name):
            raise ValueError(f"YouTube cookie entry {index} has an invalid name")
        if name not in ALLOWED_NAMES:
            raise ValueError(f"YouTube cookie entry {index} is not allow-listed")
        if not isinstance(cookie_value, str):
            raise ValueError(f"YouTube cookie entry {index} has an invalid value")
        if len(cookie_value.encode("utf-8")) > MAX_COOKIE_VALUE_BYTES or any(
            ord(character) < 0x20 or ord(character) == 0x7F
            for character in cookie_value
        ):
            raise ValueError(f"YouTube cookie entry {index} has an unsafe value")
        if domain not in ALLOWED_DOMAINS:
            raise ValueError(f"YouTube cookie entry {index} has a forbidden domain")
        if not isinstance(path, str) or not path.startswith("/") or len(path) > 256:
            raise ValueError(f"YouTube cookie entry {index} has an invalid path")
        if secure is not True:
            raise ValueError(f"YouTube cookie entry {index} must be Secure")
        if expires in (None, 0, 0.0):
            normalized_expires = None
        elif isinstance(expires, (int, float)) and not isinstance(expires, bool):
            normalized_expires = int(expires)
            if normalized_expires <= now:
                raise ValueError(f"YouTube cookie entry {index} is expired")
        else:
            raise ValueError(f"YouTube cookie entry {index} has an invalid expiry")
        identity = (name, domain, path)
        if identity in seen:
            raise ValueError(f"YouTube cookie entry {index} is duplicated")
        seen.add(identity)
        normalized.append({
            "name": name,
            "value": cookie_value,
            "domain": domain,
            "path": path,
            "secure": True,
            "expires": normalized_expires,
        })
    names = {entry["name"] for entry in normalized}
    if "LOGIN_INFO" not in names or not names.intersection(SAPISID_NAMES):
        raise ValueError(
            "YouTube cookie payload lacks the minimum authenticated-session entries"
        )
    return normalized


def cookie_secret_variants(cookies: list[dict[str, Any]] | None) -> list[str]:
    """Return common reversible encodings, longest first, without logging them."""

    variants: set[str] = set()
    for entry in cookies or []:
        secret = str(entry.get("value") or "")
        if not secret:
            continue
        variants.update({
            secret,
            quote(secret, safe=""),
            quote_plus(secret, safe=""),
            json.dumps(secret, ensure_ascii=False)[1:-1],
            base64.b64encode(secret.encode("utf-8")).decode("ascii"),
        })
    # Longest-first is important when two cookie values overlap.  Replacing a
    # short prefix first could otherwise leave a credential suffix behind.
    return sorted((variant for variant in variants if variant), key=len, reverse=True)


def redact_cookie_values(value: str, cookies: list[dict[str, Any]] | None) -> str:
    """Remove raw or reversibly encoded secrets before crossing a boundary."""

    redacted = str(value)
    for secret in cookie_secret_variants(cookies):
        redacted = redacted.replace(secret, "[youtube-cookie]")
    return redacted


def install_cookiejar(cookiejar: Any, cookies: list[dict[str, Any]]) -> None:
    """Install validated entries into a yt-dlp-compatible in-memory CookieJar."""

    for entry in cookies:
        domain = entry["domain"]
        expires = entry.get("expires")
        cookiejar.set_cookie(Cookie(
            version=0,
            name=entry["name"],
            value=entry["value"],
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=domain.startswith("."),
            domain_initial_dot=domain.startswith("."),
            path=entry.get("path") or "/",
            path_specified=True,
            secure=True,
            expires=expires,
            discard=expires is None,
            comment=None,
            comment_url=None,
            rest={"HttpOnly": None},
            rfc2109=False,
        ))
