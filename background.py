#!/usr/bin/env python3
"""Normalize rich source metadata and safely persist auxiliary background assets."""

import hashlib
import html
import asyncio
import json
import os
from pathlib import Path
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import aiofiles
import aiohttp
from bs4 import BeautifulSoup

from anti_crawler import build_headers
from config import (
    MAX_BACKGROUND_ASSET_BYTES,
    MAX_BACKGROUND_ASSETS,
    MAX_BACKGROUND_TOTAL_BYTES,
    BACKGROUND_ASSET_TIMEOUT,
)
from network_safety import safe_get


SCHEMA_VERSION = 1
ALLOWED_CONTENT_TYPES = {
    "application/json",
    "application/ld+json",
    "application/x-subrip",
    "application/xml",
}
EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "text/plain": ".txt",
    "text/html": ".html",
    "text/vtt": ".vtt",
    "application/json": ".json",
    "application/ld+json": ".json",
    "application/x-subrip": ".srt",
    "application/xml": ".xml",
}
KIND_NAMES = {
    "cover": "cover",
    "transcript": "transcript",
    "chapters": "chapters",
    "source_text": "source-text",
}


def encode_metadata(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def decode_metadata(value) -> dict:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def plain_text(value: str) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(html.unescape(value), "lxml")
    for tag in soup.find_all("br"):
        tag.replace_with("\n")
    for tag in soup.find_all(("p", "div", "li")):
        tag.insert_after("\n")
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def sanitize_html(value: str) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(value, "lxml")
    for tag in soup.find_all((
        "script", "style", "iframe", "object", "embed", "form", "meta", "link",
    )):
        tag.decompose()
    for tag in soup.find_all():
        for attribute in list(tag.attrs):
            lowered = attribute.lower()
            if lowered.startswith("on") or lowered in {"srcdoc", "style"}:
                del tag.attrs[attribute]
                continue
            if lowered in {"href", "src", "action", "poster", "xlink:href"}:
                raw = tag.attrs.get(attribute, "")
                candidate = raw[0] if isinstance(raw, list) and raw else raw
                scheme = urlsplit(str(candidate).strip()).scheme.lower()
                if scheme in {"javascript", "data", "vbscript", "file"}:
                    del tag.attrs[attribute]
    container = soup.body or soup
    return "".join(str(child) for child in container.contents).strip()


def redact_url(value: str) -> str:
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    try:
        port = parsed.port
    except ValueError:
        return ""
    if port:
        host += f":{port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def _redact_error(value: str) -> str:
    """Remove query strings from URLs embedded in persisted error messages."""
    pattern = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
    return pattern.sub(lambda match: redact_url(match.group(0)), value)


def redact_metadata_urls(value, key: str = ""):
    if isinstance(value, dict):
        return {name: redact_metadata_urls(child, name) for name, child in value.items()}
    if isinstance(value, list):
        return [redact_metadata_urls(child, key) for child in value]
    if isinstance(value, str) and "url" in key.lower():
        return redact_url(value)
    return value


def metadata_envelope(source: str, *, common: dict | None = None,
                      source_data: dict | None = None,
                      assets: dict | None = None,
                      text_source: str = "platform",
                      transcript_status: str | None = None) -> dict:
    asset_data = assets or {}
    return {
        "schema_version": SCHEMA_VERSION,
        "text_source": text_source,
        "transcript_status": transcript_status or (
            "provided" if asset_data.get("transcripts") else "not_provided"
        ),
        "common": common or {},
        "source_data": {source: source_data or {}},
        "assets": asset_data,
        "provenance": {
            "source": source,
            "collected_at": datetime.now(timezone.utc).isoformat(),
        },
    }


def _asset_extension(content_type: str, url: str) -> str:
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type in EXTENSIONS:
        return EXTENSIONS[media_type]
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif",
                  ".txt", ".html", ".htm", ".vtt", ".srt", ".json", ".xml"}:
        return ".jpg" if suffix == ".jpeg" else ".html" if suffix == ".htm" else suffix
    return ".bin"


def _allowed_type(content_type: str, kind: str, url: str) -> bool:
    media_type = content_type.split(";", 1)[0].strip().lower()
    suffix = Path(urlsplit(url).path).suffix.lower()
    if kind == "cover":
        return media_type in {"image/jpeg", "image/png", "image/webp", "image/gif"}
    if not media_type:
        return suffix in {".txt", ".html", ".htm", ".vtt", ".srt", ".json", ".xml"}
    if media_type == "application/octet-stream":
        return suffix in {".txt", ".html", ".htm", ".vtt", ".srt", ".json", ".xml"}
    if media_type.startswith("text/") or media_type in ALLOWED_CONTENT_TYPES:
        return True
    return False


def _json_transcript_text(value) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("body", "segments", "cues", "items"):
        rows = value.get(key)
        if not isinstance(rows, list):
            continue
        lines = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            text = row.get("content") or row.get("text") or row.get("body")
            if isinstance(text, str) and text.strip():
                lines.append(text.strip())
        if lines:
            return "\n".join(lines)
    return ""


def _write_derived_text(output_path: str, kind: str) -> str:
    if kind not in {"transcript", "source_text"}:
        return ""
    suffix = Path(output_path).suffix.lower()
    if suffix in {".txt", ".vtt", ".srt"}:
        return ""
    try:
        raw = Path(output_path).read_text(encoding="utf-8", errors="replace")
        if suffix == ".json":
            text = _json_transcript_text(json.loads(raw))
        elif suffix in {".html", ".xml"}:
            text = plain_text(raw)
        else:
            text = ""
        if not text:
            return ""
        text_path = str(Path(output_path).with_suffix(".txt"))
        Path(text_path).write_text(text.rstrip() + "\n", encoding="utf-8")
        return text_path
    except (OSError, ValueError, TypeError):
        return ""


def _sanitize_downloaded_html(path: str) -> tuple[int, str]:
    """Neutralize active HTML before saving it beside an audio file."""
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    cleaned = sanitize_html(raw).encode("utf-8")
    Path(path).write_bytes(cleaned)
    return len(cleaned), hashlib.sha256(cleaned).hexdigest()


def _asset_specs(item: dict) -> list[dict]:
    metadata = decode_metadata(item.get("metadata_json", ""))
    assets = metadata.get("assets", {})
    specs = []
    cover_url = item.get("cover_url", "")
    if cover_url:
        specs.append({"kind": "cover", "url": cover_url, "type": "image/*"})
    for key, kind in (
        ("transcripts", "transcript"),
        ("chapters", "chapters"),
        ("source_texts", "source_text"),
    ):
        for raw in assets.get(key, []) or []:
            spec = dict(raw) if isinstance(raw, dict) else {"url": str(raw)}
            spec["kind"] = kind
            if spec.get("url"):
                specs.append(spec)
    unique = []
    seen = set()
    for spec in specs:
        spec.setdefault("referer", item.get("webpage_url", ""))
        key = (spec["kind"], spec.get("url", ""))
        if key not in seen:
            seen.add(key)
            unique.append(spec)
    return unique[:MAX_BACKGROUND_ASSETS]


async def _download_one(session: aiohttp.ClientSession, audio_path: str,
                        spec: dict, index: int, *, allow_private_network: bool,
                        remaining_bytes: int) -> dict:
    url = spec["url"]
    kind = spec["kind"]
    maximum = min(MAX_BACKGROUND_ASSET_BYTES, remaining_bytes)
    result = {
        "kind": kind,
        "source_url": redact_url(url),
        "type": spec.get("type", ""),
        "language": spec.get("language", ""),
        "rel": spec.get("rel", ""),
        "text_source": spec.get("text_source", "platform"),
    }
    if maximum <= 0:
        return {**result, "status": "skipped", "error": "per-item byte limit reached"}
    try:
        async with safe_get(
            session, url, allow_private=allow_private_network,
            timeout=aiohttp.ClientTimeout(total=60),
            headers=build_headers(spec.get("referer", "")),
        ) as response:
            if response.status != 200:
                raise ValueError(f"HTTP {response.status}")
            content_type = response.headers.get("Content-Type", "").lower()
            if not _allowed_type(content_type, kind, url):
                raise ValueError(f"unsupported content type: {content_type or '<empty>'}")
            if response.content_length is not None and response.content_length > maximum:
                raise ValueError("background asset exceeds byte limit")
            extension = _asset_extension(content_type, url)
            stem = os.path.splitext(audio_path)[0]
            ordinal = "" if kind == "cover" else f".{index}"
            output_path = f"{stem}.{KIND_NAMES[kind]}{ordinal}{extension}"
            part_path = output_path + ".part"
            total = 0
            digest = hashlib.sha256()
            async with aiofiles.open(part_path, "wb") as output:
                async for chunk in response.content.iter_chunked(65536):
                    total += len(chunk)
                    if total > maximum:
                        raise ValueError("background asset exceeds byte limit")
                    digest.update(chunk)
                    await output.write(chunk)
            if extension == ".html":
                total, digest_hex = _sanitize_downloaded_html(part_path)
            else:
                digest_hex = digest.hexdigest()
            os.replace(part_path, output_path)
            derived_text = _write_derived_text(output_path, kind)
            saved = {
                **result,
                "status": "saved",
                "path": output_path,
                "bytes": total,
                "sha256": digest_hex,
                "content_type": content_type.split(";", 1)[0],
            }
            if derived_text:
                saved["plain_text_path"] = derived_text
            return saved
    except Exception as exc:
        part_path = locals().get("part_path", "")
        if part_path and os.path.exists(part_path):
            os.remove(part_path)
        return {
            **result,
            "status": "failed",
            "error": _redact_error(str(exc))[:300],
        }


async def persist_background(session: aiohttp.ClientSession, audio_path: str,
                             item: dict, *, mode: str = "all",
                             allow_private_network: bool = False) -> dict:
    """Write descriptions and optionally download bounded public assets."""
    if mode == "none":
        return {"mode": mode, "description_files": [], "assets": []}
    stem = os.path.splitext(audio_path)[0]
    metadata = decode_metadata(item.get("metadata_json", ""))
    common = metadata.get("common", {})
    description = item.get("description", "") or common.get("description", "")
    description_html = sanitize_html(common.get("description_html", ""))
    description_files = []
    text = plain_text(description_html or description)
    if text:
        path = stem + ".description.txt"
        async with aiofiles.open(path, "w", encoding="utf-8") as output:
            await output.write(text + "\n")
        description_files.append(path)
    if description_html:
        path = stem + ".description.html"
        async with aiofiles.open(path, "w", encoding="utf-8") as output:
            await output.write(description_html)
        description_files.append(path)

    results = []
    if mode == "all":
        remaining = MAX_BACKGROUND_TOTAL_BYTES
        counters = {}
        for spec in _asset_specs(item):
            kind = spec["kind"]
            counters[kind] = counters.get(kind, 0) + 1
            try:
                result = await asyncio.wait_for(
                    _download_one(
                        session, audio_path, spec, counters[kind],
                        allow_private_network=allow_private_network,
                        remaining_bytes=remaining,
                    ),
                    timeout=BACKGROUND_ASSET_TIMEOUT,
                )
            except asyncio.TimeoutError:
                result = {
                    "kind": kind,
                    "source_url": redact_url(spec.get("url", "")),
                    "type": spec.get("type", ""),
                    "language": spec.get("language", ""),
                    "rel": spec.get("rel", ""),
                    "text_source": spec.get("text_source", "platform"),
                    "status": "failed",
                    "error": f"background asset timeout ({BACKGROUND_ASSET_TIMEOUT}s)",
                }
            results.append(result)
            remaining -= result.get("bytes", 0)
    return {"mode": mode, "description_files": description_files, "assets": results}


def save_sidecar(audio_path: str, item: dict, content_hash: str = "",
                 background: dict | None = None) -> str:
    metadata_path = os.path.splitext(audio_path)[0] + ".json"
    original_url = item.get("url", "")
    metadata = {
        "schema_version": 2,
        "title": item.get("title", ""),
        "source": item.get("source", ""),
        "source_id": item.get("source_id", ""),
        "original_url": redact_url(original_url),
        "original_url_query_redacted": bool(urlsplit(original_url).query),
        "webpage_url": redact_url(item.get("webpage_url", "")),
        "description": item.get("description", ""),
        "author": item.get("author", ""),
        "cover_url": redact_url(item.get("cover_url", "")),
        "file_format": os.path.splitext(audio_path)[1].lstrip(".").lower(),
        "file_size": os.path.getsize(audio_path) if os.path.exists(audio_path) else 0,
        "duration": item.get("duration", 0),
        "language": item.get("language", ""),
        "category": item.get("category", ""),
        "speaker": item.get("speaker", ""),
        "published_at": item.get("published_at", ""),
        "content_hash": content_hash or item.get("content_hash", ""),
        "content_hash_algorithm": "sha256",
        "background_metadata": redact_metadata_urls(
            decode_metadata(item.get("metadata_json", "")),
        ),
        "background_files": background or {},
    }
    metadata["acquired_at"] = datetime.now(timezone.utc).isoformat()
    with open(metadata_path, "w", encoding="utf-8") as output:
        json.dump(metadata, output, ensure_ascii=False, indent=2)
    return metadata_path
