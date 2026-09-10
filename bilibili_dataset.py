#!/usr/bin/env python3
"""Build auditable Bilibili video, audio and platform-caption bundles."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import ipaddress
import json
import math
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlsplit

from bilibili_subtitles import (
    classify_subtitle_track,
    classify_subtitle_inventory,
    parse_subtitle_document,
    render_subtitle_text,
    render_subtitle_vtt,
    safe_caption_component,
    sanitize_subtitle_track,
    subtitle_track_diagnostic,
    redact_url,
    sanitize_subtitle_document,
)
from bilibili_proxy import get_bilibili_proxy, proxy_request_kwargs
from youtube_dataset import redact_urls_in_text, sha256_file, write_json_atomic, write_text_atomic
from network_safety import validate_public_http_url


SCHEMA_VERSION = 1
ENCODING_PROFILE = "bilibili-mp4-wav16k-v1"
VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
PLAYER_URL = "https://api.bilibili.com/x/player/v2"
PLAYURL_URL = "https://api.bilibili.com/x/player/playurl"
BVID_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")
MAX_MANIFEST_BYTES = 10 * 1024 * 1024
MAX_API_BYTES = 20 * 1024 * 1024
MAX_SUBTITLE_BYTES = 50 * 1024 * 1024
MAX_MEDIA_BYTES = 8 * 1024 * 1024 * 1024
MAX_BUNDLE_BYTES = 10 * 1024 * 1024 * 1024
MIN_FREE_DISK_BYTES = 20 * 1024 * 1024 * 1024
MAX_PARTS = 1000
RIGHTS_STATUSES = {
    "needs_review", "unknown", "licensed", "permission_granted",
    "public_domain", "creative_commons",
}
CLEARED_RIGHTS = {
    "licensed", "permission_granted", "public_domain", "creative_commons",
}
AI_GENERATION_STATUSES = {"declared", "not_declared", "suspected", "unknown"}
CAPTION_INVENTORY_RETRY_SECONDS = 1.0
CAPTION_TIMELINE_TOLERANCE_SECONDS = 2.0
CAPTION_TIMELINE_RULE_VERSION = "bilibili-caption-timeline-v1"
MAX_CAPTION_TIMELINE_SECONDS = 7 * 24 * 3600


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def bilibili_video_id(value: str) -> str:
    """Return a validated BV id from an id or canonical Bilibili video URL."""

    if not isinstance(value, str):
        raise ValueError("Bilibili id/URL must be text")
    value = value.strip()
    if BVID_RE.fullmatch(value):
        return value
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise ValueError("only credential-free HTTPS Bilibili URLs are accepted")
    if (parsed.hostname or "").lower().rstrip(".") not in {
        "bilibili.com", "www.bilibili.com", "m.bilibili.com",
    }:
        raise ValueError("URL is not a supported Bilibili host")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0] != "video" or not BVID_RE.fullmatch(parts[1]):
        raise ValueError("URL is not a canonical Bilibili BV video URL")
    return parts[1]


def _url_part(value: str) -> int | None:
    if not isinstance(value, str) or BVID_RE.fullmatch(value.strip()):
        return None
    parsed = urlsplit(value.strip())
    values = parse_qs(parsed.query).get("p")
    if not values:
        return None
    if len(values) != 1 or not values[0].isdigit():
        raise ValueError("Bilibili URL p query must be one positive integer")
    return _bounded_int(int(values[0]), "Bilibili URL part", 1, MAX_PARTS)


def _short_strings(values: Any, label: str, *, maximum: int = 16) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list) or not 0 <= len(values) <= maximum:
        raise ValueError(f"{label} must be an array with at most {maximum} entries")
    result = []
    for value in values:
        if not isinstance(value, str) or not value.strip() or len(value) > 80:
            raise ValueError(f"{label} contains an invalid value")
        normalized = value.strip().replace("_", "-")
        if normalized not in result:
            result.append(normalized)
    return result


def _bounded_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer in {minimum}-{maximum}")
    return value


def validate_manifest(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate and normalize a declarative source manifest."""

    if not isinstance(document, dict) or not isinstance(document.get("items"), list):
        raise ValueError("manifest must contain an items array")
    if not 1 <= len(document["items"]) <= 1000:
        raise ValueError("manifest items must contain between 1 and 1000 entries")
    normalized = []
    seen: set[tuple[str, tuple[int, ...]]] = set()
    for index, raw in enumerate(document["items"], 1):
        if not isinstance(raw, dict):
            raise ValueError(f"item {index} must be an object")
        item = dict(raw)
        raw_target = str(item.get("bvid") or item.get("url") or "")
        bvid = bilibili_video_id(raw_target)
        query_part = _url_part(raw_target)
        parts_raw = item.get("parts") or ([query_part] if query_part is not None else [])
        if not isinstance(parts_raw, list) or len(parts_raw) > MAX_PARTS:
            raise ValueError(f"item {index} parts must be a bounded array")
        parts = []
        for part in parts_raw:
            part = _bounded_int(part, f"item {index} part", 1, MAX_PARTS)
            if part not in parts:
                parts.append(part)
        if query_part is not None and parts != [query_part]:
            raise ValueError(f"item {index} URL p query conflicts with explicit parts")
        duplicate_key = (bvid, tuple(sorted(parts)))
        if duplicate_key in seen:
            raise ValueError(f"item {index} duplicates {bvid} with the same parts")
        seen.add(duplicate_key)
        max_parts = _bounded_int(item.get("max_parts", 20), "max_parts", 1, MAX_PARTS)
        max_height = _bounded_int(item.get("max_height", 720), "max_height", 144, 1080)
        max_duration = float(item.get("max_duration_seconds", 4 * 3600))
        if not math.isfinite(max_duration) or not 0 < max_duration <= 4 * 3600:
            raise ValueError(f"item {index} max_duration_seconds must be in (0, 14400]")
        languages = _short_strings(item.get("languages"), f"item {index} languages")
        content_language = str(item.get("content_language") or "und")[:80].replace("_", "-")
        if not languages:
            languages = default_caption_languages(content_language)
        if any(
            not caption_language_matches_content(content_language, language)
            for language in languages
        ):
            raise ValueError(
                f"item {index} caption languages must match content_language {content_language!r}"
            )
        require_caption = item.get("require_caption", False)
        if not isinstance(require_caption, bool):
            raise ValueError(f"item {index} require_caption must be boolean")

        rights = item.get("rights") or {"status": "needs_review"}
        if not isinstance(rights, dict) or rights.get("status") not in RIGHTS_STATUSES:
            raise ValueError(f"item {index} has invalid rights")
        rights = {"status": rights["status"]}
        for key in ("evidence_url", "evidence_text", "note"):
            if key in (item.get("rights") or {}):
                value = item["rights"][key]
                if not isinstance(value, str) or len(value) > 4096:
                    raise ValueError(f"item {index} rights {key} is invalid")
                if key == "evidence_url":
                    parsed = urlsplit(value)
                    if parsed.scheme != "https" or parsed.username or parsed.password or not parsed.hostname:
                        raise ValueError(f"item {index} rights evidence_url must be credential-free HTTPS")
                    rights[key] = redact_url(value)
                else:
                    rights[key] = redact_urls_in_text(value)

        ai_generation = item.get("ai_generation") or {"status": "unknown", "evidence": []}
        if not isinstance(ai_generation, dict) or ai_generation.get("status") not in AI_GENERATION_STATUSES:
            raise ValueError(f"item {index} has invalid ai_generation")
        evidence = _short_strings(
            ai_generation.get("evidence"), f"item {index} ai_generation evidence", maximum=32
        )

        speaker_count = item.get("speaker_count")
        if speaker_count is not None:
            speaker_count = _bounded_int(speaker_count, "speaker_count", 1, 100)
            speaker_status = item.get("speaker_count_status", "verified_manual")
            if speaker_status != "verified_manual":
                raise ValueError("non-null speaker_count must be manually verified")
        else:
            speaker_status = item.get("speaker_count_status", "needs_review")
            if speaker_status != "needs_review":
                raise ValueError("missing speaker_count must stay needs_review")

        normalized.append({
            "bvid": bvid,
            "url": f"https://www.bilibili.com/video/{bvid}",
            "parts": parts,
            "max_parts": max_parts,
            "max_height": max_height,
            "max_duration_seconds": max_duration,
            "languages": languages,
            "require_caption": require_caption,
            "content_language": content_language,
            "program": str(item.get("program") or "")[:500],
            "rights": rights,
            "ai_generation": {"status": ai_generation["status"], "evidence": evidence},
            "speaker_count": speaker_count,
            "speaker_count_status": speaker_status,
            "source_revision": str(item.get("source_revision") or "current")[:80],
        })
    return normalized


def load_manifest(path: Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("manifest is missing or exceeds 10 MiB")
    return validate_manifest(json.loads(path.read_text(encoding="utf-8")))


def build_job_key(item: dict[str, Any], part: int, cid: int) -> str:
    """Build the immutable artifact identity shared by collect and download."""

    fingerprint = hashlib.sha256(
        json.dumps(
            [
                item["bvid"], part, cid, item["max_height"], item["languages"],
                item["require_caption"], item["source_revision"], SCHEMA_VERSION,
                ENCODING_PROFILE,
            ],
            separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()[:12]
    return f"{item['bvid']}-p{part}-c{cid}-{fingerprint}"


def build_jobs(item: dict[str, Any], view: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn a validated video item and view response into bounded part jobs."""

    pages = view.get("pages")
    if not isinstance(pages, list):
        raise ValueError("Bilibili view response has no pages array")
    requested = set(item["parts"])
    jobs = []
    for position, page in enumerate(pages, 1):
        if not isinstance(page, dict):
            continue
        part = int(page.get("page") or position)
        if requested and part not in requested:
            continue
        cid = page.get("cid")
        duration = float(page.get("duration") or 0)
        if not isinstance(cid, int) or cid <= 0:
            raise ValueError(f"Bilibili part {part} has no valid cid")
        if not math.isfinite(duration) or duration <= 0 or duration > item["max_duration_seconds"]:
            raise ValueError(f"Bilibili part {part} has invalid/out-of-policy duration")
        jobs.append({
            **item,
            "aid": view.get("aid"),
            "cid": cid,
            "part": part,
            "part_title": str(page.get("part") or ""),
            "duration_seconds": duration,
            "job_key": build_job_key(item, part, cid),
        })
        if len(jobs) >= item["max_parts"]:
            break
    missing = requested - {job["part"] for job in jobs}
    if missing:
        raise ValueError(f"requested Bilibili parts do not exist: {sorted(missing)}")
    if not jobs:
        raise ValueError("no Bilibili parts matched the manifest")
    return jobs


def _language_family(value: str) -> str:
    value = value.lower().replace("_", "-")
    return "zh" if value.startswith("ai-zh") else value.removeprefix("ai-").split("-", 1)[0]


def caption_language_matches_content(content_language: str, caption_language: str) -> bool:
    content_family = _language_family(content_language)
    caption_family = _language_family(caption_language)
    chinese = {"zh", "yue", "cmn"}
    if content_family in chinese:
        return caption_family in chinese
    return content_family == caption_family and content_family not in {"", "und"}


def default_caption_languages(content_language: str) -> list[str]:
    family = _language_family(content_language)
    if family in {"zh", "yue", "cmn"}:
        return ["zh", "zh-Hans", "zh-Hant", "zh-CN", "zh-TW", "zh-HK", "ai-zh", "yue"]
    return [content_language] if family not in {"", "und"} else []


def caption_timeline_rejection(
    cues: Iterable[Any], media_duration_seconds: float,
) -> dict[str, Any] | None:
    """Describe one platform-caption track that cannot belong to this media."""

    if isinstance(media_duration_seconds, bool):
        raise ValueError("caption timeline comparison has invalid bounds")
    media_duration = float(media_duration_seconds)
    if not math.isfinite(media_duration) or media_duration <= 0:
        raise ValueError("caption timeline comparison has invalid bounds")
    cues = list(cues)
    if not cues:
        raise ValueError("caption timeline comparison has an empty track")
    try:
        raw_ends = [cue.end for cue in cues]
        if any(isinstance(value, bool) for value in raw_ends):
            raise ValueError
        maximum = max(float(value) for value in raw_ends)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("caption timeline comparison has an invalid cue") from exc
    if (
        not math.isfinite(maximum) or maximum <= 0
        or maximum > MAX_CAPTION_TIMELINE_SECONDS
    ):
        raise ValueError("caption timeline comparison has an invalid cue")
    media_duration = round(media_duration, 6)
    maximum = round(maximum, 6)
    overrun = round(maximum - media_duration, 6)
    if overrun <= CAPTION_TIMELINE_TOLERANCE_SECONDS:
        return None
    return {
        "reason": "caption_exceeds_media_duration",
        "media_duration_seconds": media_duration,
        "maximum_cue_end_seconds": maximum,
        "overrun_seconds": overrun,
        "tolerance_seconds": CAPTION_TIMELINE_TOLERANCE_SECONDS,
        "rule_version": CAPTION_TIMELINE_RULE_VERSION,
    }


def select_caption_tracks(
    tracks: Iterable[dict[str, Any]], preferred_languages: Iterable[str]
) -> list[dict[str, Any]]:
    """Keep every matching platform track in deterministic preference order."""

    preferred = [str(value).replace("_", "-") for value in preferred_languages]
    selected = []
    seen = set()
    for raw in tracks:
        if not isinstance(raw, dict):
            continue
        track = sanitize_subtitle_track(raw)
        if not track["url"]:
            continue
        language = track["language"]
        if preferred:
            exact = language in preferred
            family = _language_family(language)
            matches = [index for index, value in enumerate(preferred) if _language_family(value) == family]
            if exact:
                language_rank = preferred.index(language)
            elif matches:
                language_rank = 100 + matches[0]
            else:
                continue
        else:
            language_rank = 1000
        identity = (track["id_str"], language, track["url_redacted"])
        if identity in seen:
            continue
        seen.add(identity)
        kind_rank = {"manual": 0, "automatic": 1, "unknown": 2}[track["kind"]]
        track["selection_rank"] = language_rank * 10 + kind_rank
        selected.append(track)
    return sorted(selected, key=lambda row: (row["selection_rank"], row["language"], row["id_str"]))


def caption_selection_status(
    inventory: dict[str, Any], selected: list[dict[str, Any]],
    preferred_languages: Iterable[str],
) -> str:
    """Explain why a provided inventory produced no downloadable selected track."""

    if inventory.get("status") != "provided" or selected:
        return str(inventory.get("status") or "unknown")
    valid = [
        sanitize_subtitle_track(raw) for raw in (inventory.get("tracks") or [])
        if isinstance(raw, dict)
    ]
    if not any(track["url"] for track in valid):
        return "invalid_track_inventory"
    return "no_matching_language" if list(preferred_languages) else "invalid_track_inventory"


def _inventory_attempt_summary(
    inventory: dict[str, Any], selected: list[dict[str, Any]], attempt: int,
    preferred_languages: Iterable[str],
) -> dict[str, Any]:
    raw_tracks = inventory.get("tracks") or []
    selection_status = (
        "downloadable" if selected
        else caption_selection_status(inventory, selected, preferred_languages)
    )
    return {
        "attempt": attempt,
        "inventory_status": str(inventory.get("status") or "unknown"),
        "need_login_subtitle": inventory.get("need_login_subtitle"),
        "raw_track_count": len(raw_tracks),
        "selected_track_count": len(selected),
        "selection_status": selection_status,
        "tracks": [
            subtitle_track_diagnostic(track, index)
            for index, track in enumerate(raw_tracks)
        ],
    }


async def resolve_caption_inventory(
    client: Any, job: dict[str, Any], *, retry_seconds: float = CAPTION_INVENTORY_RETRY_SECONDS,
    strict_refresh: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]], str, list[dict[str, Any]]]:
    """Resolve captions with one bounded refresh after an unusable provided list."""

    first_inventory = await client.caption_inventory(job["bvid"], job["cid"])
    first_selected = select_caption_tracks(
        first_inventory.get("tracks") or [], job["languages"]
    )
    first_summary = _inventory_attempt_summary(
        first_inventory, first_selected, 1, job["languages"]
    )
    attempts = [first_summary]
    if first_selected:
        return first_inventory, first_selected, "downloaded", attempts
    first_status = first_summary["selection_status"]
    if first_inventory.get("status") != "provided":
        return first_inventory, [], first_status, attempts

    await asyncio.sleep(retry_seconds)
    try:
        second_inventory = await client.caption_inventory(job["bvid"], job["cid"])
    except Exception as exc:
        if strict_refresh:
            raise
        attempts.append({
            "attempt": 2,
            "inventory_status": "refresh_failed",
            "error_type": type(exc).__name__,
        })
        return first_inventory, [], first_status, attempts
    second_selected = select_caption_tracks(
        second_inventory.get("tracks") or [], job["languages"]
    )
    attempts.append(_inventory_attempt_summary(
        second_inventory, second_selected, 2, job["languages"]
    ))
    if second_selected:
        return second_inventory, second_selected, "downloaded", attempts
    return second_inventory, [], first_status, attempts


def _caption_basename(track: dict[str, Any], index: int) -> str:
    language = safe_caption_component(track.get("language") or "und")
    identity = safe_caption_component(track.get("id_str") or str(index + 1))
    return f"captions.{language}.{track['kind']}.{identity}.{index + 1}"


def clear_caption_stage_files(stage: Path) -> None:
    """Remove only pipeline-owned caption payloads from a retry staging root."""

    for path in Path(stage).glob("captions.*"):
        if path.parent == Path(stage) and (path.is_file() or path.is_symlink()):
            path.unlink()


async def prepare_caption_payloads(
    client: Any, tracks: list[dict[str, Any]], stage: Path,
    media_duration_seconds: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch, classify and stage valid or quarantined caption payloads per track."""

    clear_caption_stage_files(stage)
    accepted = []
    rejected = []
    for index, track in enumerate(tracks):
        document = sanitize_subtitle_document(await client.subtitle(track["url"]))
        cues = parse_subtitle_document(document)
        rejection = caption_timeline_rejection(cues, media_duration_seconds)
        base = _caption_basename(track, index)
        persisted = {
            key: value for key, value in track.items()
            if key not in {"url", "selection_rank"}
        }
        if rejection:
            paths = {"json": stage / f"{base}.rejected.json"}
            keys = {"json": f"caption_rejected_{index}_json"}
            write_json_atomic(paths["json"], document)
            persisted.update({
                "status": "rejected",
                "cue_count": len(cues),
                "files": keys,
                "timeline_rejection": rejection,
            })
            rejected.append({
                "index": index, "paths": paths, "track": persisted,
            })
            continue
        paths = {
            "json": stage / f"{base}.json",
            "vtt": stage / f"{base}.vtt",
            "txt": stage / f"{base}.txt",
        }
        keys = {
            extension: f"caption_{index}_{extension}"
            for extension in ("json", "vtt", "txt")
        }
        write_json_atomic(paths["json"], document)
        write_text_atomic(paths["vtt"], render_subtitle_vtt(cues))
        write_text_atomic(paths["txt"], render_subtitle_text(cues))
        persisted.update({
            "status": "downloaded", "cue_count": len(cues), "files": keys,
        })
        accepted.append({"index": index, "paths": paths, "track": persisted})
    return accepted, rejected


def _media_url(value: str) -> str:
    parsed = urlsplit(str(value or ""))
    if parsed.scheme != "https" or parsed.username or parsed.password or not parsed.hostname:
        return ""
    if parsed.port not in {None, 443}:
        return ""
    host = parsed.hostname.lower().rstrip(".")
    allowed = any(
        host == suffix or host.endswith("." + suffix)
        for suffix in ("bilivideo.com", "bilivideo.cn", "akamaized.net")
    )
    if not allowed:
        return ""
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        if not address.is_global:
            return ""
    return str(value)


def _stream_urls(stream: dict[str, Any]) -> list[str]:
    values = [stream.get("baseUrl") or stream.get("base_url")]
    backups = stream.get("backupUrl") or stream.get("backup_url") or []
    if isinstance(backups, list):
        values.extend(backups)
    result = []
    for value in values:
        normalized = _media_url(str(value or ""))
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def stream_descriptor(stream: dict[str, Any]) -> dict[str, Any]:
    """Return a signed-URL-free identity for safe resume and provenance."""

    return {key: stream.get(key) for key in (
        "id", "quality", "codecid", "mimeType", "mime_type", "codecs", "width", "height",
        "frameRate", "frame_rate", "bandwidth", "sar", "startWithSap", "start_with_sap",
    )}


def select_dash_streams(dash: dict[str, Any], max_height: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """Select highest permitted video (AVC tie-break) and highest-bandwidth audio."""

    if not isinstance(dash, dict):
        raise ValueError("playurl response has no DASH object")
    videos = [
        stream for stream in (dash.get("video") or [])
        if isinstance(stream, dict)
        and int(stream.get("height") or 0) <= max_height
        and _stream_urls(stream)
    ]
    audios = [
        stream for stream in (dash.get("audio") or [])
        if isinstance(stream, dict) and _stream_urls(stream)
    ]
    if not videos or not audios:
        raise ValueError("playurl has no usable bounded video/audio DASH pair")
    video = max(videos, key=lambda stream: (
        int(stream.get("height") or 0),
        int(stream.get("codecid") == 7),
        int(stream.get("bandwidth") or 0),
    ))
    audio = max(audios, key=lambda stream: int(stream.get("bandwidth") or 0))
    return video, audio


def output_directory(
    root: Path, job: dict[str, Any], *, destination: Path | None = None
) -> Path:
    root = Path(root).resolve()
    directory = (
        Path(destination)
        if destination is not None
        else root / "parents" / job["bvid"] / f"p{job['part']}" / job["job_key"]
    )
    directory = directory.resolve()
    if root != directory and root not in directory.parents:
        raise ValueError("bundle output escaped the configured root")
    return directory


def _run(command: list[str], *, timeout: int = 7200) -> None:
    try:
        subprocess.run(
            command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
            env=_subprocess_env(),
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", "replace")[-2000:]
        raise RuntimeError(f"{command[0]} failed: {stderr}") from exc


def _subprocess_env() -> dict[str, str]:
    """Do not expose crawler credentials to ffmpeg/ffprobe child processes."""

    environment = dict(os.environ)
    environment.pop("BILIBILI_COOKIE", None)
    environment.pop("AUDIOSPIDER_BILIBILI_PROXY", None)
    return environment


def _merge_dash(video_input: Path, audio_input: Path, destination: Path) -> str:
    temporary = destination.with_name(f".{destination.name}.tmp.mp4")
    temporary.unlink(missing_ok=True)
    try:
        try:
            _run([
                "ffmpeg", "-nostdin", "-y", "-i", str(video_input), "-i", str(audio_input),
                "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-movflags", "+faststart",
                str(temporary),
            ])
            mode = "remux_copy"
        except RuntimeError:
            temporary.unlink(missing_ok=True)
            _run([
                "ffmpeg", "-nostdin", "-y", "-i", str(video_input), "-i", str(audio_input),
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "23", "-c:a", "aac", "-movflags", "+faststart", str(temporary),
            ])
            mode = "transcode_h264_aac"
        os.replace(temporary, destination)
        return mode
    finally:
        temporary.unlink(missing_ok=True)


def _extract_wav(video: Path, destination: Path) -> None:
    temporary = destination.with_name(f".{destination.name}.tmp.wav")
    temporary.unlink(missing_ok=True)
    try:
        _run([
            "ffmpeg", "-nostdin", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", str(temporary),
        ])
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _file_record(path: Path, bundle: Path) -> dict[str, Any]:
    return {
        "path": str(path.relative_to(bundle)),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _rights_cleared(job: dict[str, Any]) -> bool:
    rights = job.get("rights") or {}
    return rights.get("status") in CLEARED_RIGHTS and bool(
        rights.get("evidence_url") or rights.get("evidence_text")
    )


class BilibiliClient:
    """Bounded anonymous client for public Bilibili metadata and captions."""

    def __init__(self, session: Any, *, auth_cookie: str = "", proxy: str | None = None):
        self.session = session
        self.auth_cookie = auth_cookie
        self.authenticated = bool(auth_cookie)
        self.proxy = proxy

    async def _json(self, url: str, *, params: dict[str, Any] | None = None,
                    maximum: int = MAX_API_BYTES) -> dict[str, Any]:
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.bilibili.com/",
        }
        if self.auth_cookie and (urlsplit(url).hostname or "").lower() == "api.bilibili.com":
            headers["Cookie"] = self.auth_cookie
        async with self.session.get(
            url, params=params, headers=headers, allow_redirects=False,
            **proxy_request_kwargs(self.proxy),
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"Bilibili HTTP {response.status} for {urlsplit(url).path}")
            content_length = int(response.headers.get("Content-Length") or 0)
            if content_length > maximum:
                raise ValueError("Bilibili response exceeds byte limit")
            payload = await response.read()
        if len(payload) > maximum:
            raise ValueError("Bilibili response exceeds byte limit")
        document = json.loads(payload.decode("utf-8"))
        if not isinstance(document, dict):
            raise ValueError("Bilibili response must be a JSON object")
        return document

    async def view(self, bvid: str) -> dict[str, Any]:
        document = await self._json(VIEW_URL, params={"bvid": bvid})
        if document.get("code") != 0 or not isinstance(document.get("data"), dict):
            raise RuntimeError(f"Bilibili view API failed: code={document.get('code')}")
        return document["data"]

    async def caption_inventory(self, bvid: str, cid: int) -> dict[str, Any]:
        document = await self._json(PLAYER_URL, params={"bvid": bvid, "cid": cid})
        if document.get("code") != 0:
            raise RuntimeError(f"Bilibili player API failed: code={document.get('code')}")
        return classify_subtitle_inventory(document.get("data"))

    async def tracks(self, bvid: str, cid: int) -> list[dict[str, Any]]:
        """Compatibility helper for callers that only need visible tracks."""
        return (await self.caption_inventory(bvid, cid))["tracks"]

    async def subtitle(self, url: str) -> dict[str, Any]:
        if not sanitize_subtitle_track({"subtitle_url": url})["url"]:
            raise ValueError("subtitle URL is outside the Bilibili subtitle allowlist")
        await validate_public_http_url(url)
        return await self._json(url, maximum=MAX_SUBTITLE_BYTES)

    async def dash(self, bvid: str, cid: int, max_height: int) -> dict[str, Any]:
        qn = 80 if max_height >= 1080 else 64 if max_height >= 720 else 32 if max_height >= 480 else 16
        document = await self._json(PLAYURL_URL, params={
            "bvid": bvid, "cid": cid, "fnval": 4048, "fourk": 0, "qn": qn,
        })
        if document.get("code") != 0:
            raise RuntimeError(f"Bilibili playurl API failed: code={document.get('code')}")
        dash = document.get("data", {}).get("dash")
        if not isinstance(dash, dict):
            raise ValueError("Bilibili playurl response contains no DASH data")
        return dash


def _stream_fingerprint(stream: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(stream_descriptor(stream), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _parse_content_range(value: str) -> tuple[int, int, int | None] | None:
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", value.strip())
    if not match:
        return None
    start, end = int(match.group(1)), int(match.group(2))
    total = None if match.group(3) == "*" else int(match.group(3))
    if end < start or (total is not None and (total <= 0 or end >= total)):
        return None
    return start, end, total


def _content_range_start(value: str) -> int | None:
    parsed = _parse_content_range(value)
    return parsed[0] if parsed else None


async def _download_stream(
    session: Any, stream: dict[str, Any], destination: Path, *, maximum: int,
    expected_kind: str, proxy: str | None = None,
) -> None:
    """Download one fixed representation with identity-bound HTTP Range resume."""

    urls = _stream_urls(stream)
    if not urls:
        raise ValueError("DASH stream has no allowed public CDN URL")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(destination.parent).free < MIN_FREE_DISK_BYTES:
        raise OSError("free disk space is below the 20 GiB safety reserve")
    partial = destination.with_suffix(destination.suffix + ".part")
    state_path = destination.with_suffix(destination.suffix + ".state.json")
    fingerprint = _stream_fingerprint(stream)
    previous = {}
    if state_path.is_file():
        try:
            previous = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
    if previous.get("fingerprint") != fingerprint or previous.get("expected_kind") != expected_kind:
        partial.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
    if destination.is_file():
        valid = (
            previous.get("complete") is True
            and previous.get("bytes") == destination.stat().st_size
            and previous.get("sha256") == sha256_file(destination)
        )
        if valid:
            summary = _media_summary(destination)
            valid = (
                summary["video_stream_count"] >= 1 if expected_kind == "video"
                else summary["audio_stream_count"] >= 1
            )
        if valid and 0 < destination.stat().st_size <= maximum:
            return
        destination.unlink(missing_ok=True)
    write_json_atomic(state_path, {
        "fingerprint": fingerprint, "descriptor": stream_descriptor(stream),
        "expected_kind": expected_kind, "complete": False,
    })

    last_error: Exception | None = None
    for url in urls:
        try:
            await validate_public_http_url(url)
            offset = partial.stat().st_size if partial.is_file() else 0
            if offset > maximum:
                raise ValueError("partial DASH stream exceeds byte limit")
            headers = {
                "Referer": "https://www.bilibili.com/",
                "User-Agent": "Mozilla/5.0",
            }
            if offset:
                headers["Range"] = f"bytes={offset}-"
            async with session.get(
                url, headers=headers, allow_redirects=False,
                **proxy_request_kwargs(proxy),
            ) as response:
                if response.status not in {200, 206}:
                    raise RuntimeError(f"DASH CDN returned HTTP {response.status}")
                range_info = _parse_content_range(response.headers.get("Content-Range", ""))
                if response.status == 206:
                    if range_info is None or range_info[0] != offset:
                        raise RuntimeError("DASH resume Content-Range does not match local bytes")
                    mode = "ab" if offset else "wb"
                elif response.status == 200:
                    mode = "wb"
                    offset = 0
                else:
                    mode = "wb"
                content_length = int(response.headers.get("Content-Length") or 0)
                if content_length and offset + content_length > maximum:
                    raise ValueError("DASH Content-Length exceeds byte limit")
                total = offset
                with partial.open(mode) as output:
                    async for chunk in response.content.iter_chunked(1024 * 1024):
                        total += len(chunk)
                        if total > maximum:
                            raise ValueError("DASH stream exceeds byte limit")
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                received = total - offset
                if range_info is not None:
                    expected = range_info[1] - range_info[0] + 1
                    if received != expected:
                        raise RuntimeError("DASH response body does not match Content-Range")
                    if range_info[2] is not None and total != range_info[2]:
                        raise RuntimeError("DASH ranged response did not reach the representation end")
                elif content_length and received != content_length:
                    raise RuntimeError("DASH response body does not match Content-Length")
            if not partial.is_file() or partial.stat().st_size <= 0:
                raise ValueError("DASH stream download is empty")
            os.replace(partial, destination)
            summary = _media_summary(destination)
            if expected_kind == "video" and summary["video_stream_count"] < 1:
                raise ValueError("downloaded video representation has no video stream")
            if expected_kind == "audio" and summary["audio_stream_count"] < 1:
                raise ValueError("downloaded audio representation has no audio stream")
            write_json_atomic(state_path, {
                "fingerprint": fingerprint, "descriptor": stream_descriptor(stream),
                "expected_kind": expected_kind, "complete": True,
                "bytes": destination.stat().st_size, "sha256": sha256_file(destination),
            })
            return
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"all DASH CDN candidates failed: {last_error}")


@contextmanager
def job_lock(output_root: Path, job_key: str):
    lock_dir = Path(output_root).resolve() / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{job_key}.lock"
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _metadata_view(view: dict[str, Any]) -> dict[str, Any]:
    owner = view.get("owner") if isinstance(view.get("owner"), dict) else {}
    stat = view.get("stat") if isinstance(view.get("stat"), dict) else {}
    rights = view.get("rights") if isinstance(view.get("rights"), dict) else {}
    return {
        "aid": view.get("aid"),
        "title": str(view.get("title") or ""),
        "description": redact_urls_in_text(view.get("desc")),
        "cover_url": redact_url(str(view.get("pic") or "")),
        "published_at": view.get("pubdate"),
        "copyright": view.get("copyright"),
        "category": str(view.get("tname") or ""),
        "owner": {"mid": owner.get("mid"), "name": str(owner.get("name") or "")},
        "stats": {key: stat.get(key) for key in (
            "view", "danmaku", "reply", "favorite", "coin", "share", "like"
        )},
        "platform_rights": {key: rights.get(key) for key in sorted(rights)},
    }


def _media_summary(path: Path) -> dict[str, Any]:
    try:
        result = subprocess.run([
            "ffprobe", "-v", "error", "-show_streams", "-show_format",
            "-of", "json", str(path),
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
           env=_subprocess_env())
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"ffprobe rejected {path.name}") from exc
    probe = json.loads(result.stdout.decode("utf-8"))
    streams = probe.get("streams") or []
    duration = float((probe.get("format") or {}).get("duration") or 0)
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    return {
        "duration_seconds": duration,
        "video_stream_count": sum(stream.get("codec_type") == "video" for stream in streams),
        "audio_stream_count": sum(stream.get("codec_type") == "audio" for stream in streams),
        "video_codec": video.get("codec_name") if video else None,
        "width": video.get("width") if video else None,
        "height": video.get("height") if video else None,
        "audio_codec": audio.get("codec_name") if audio else None,
        "sample_rate": int(audio.get("sample_rate") or 0) if audio else 0,
        "channels": audio.get("channels") if audio else None,
    }


async def download_job(
    client: BilibiliClient, job: dict[str, Any], view: dict[str, Any], output_root: Path,
    *, destination: Path | None = None,
) -> Path:
    """Download one public BV part and atomically promote its complete bundle."""

    integrated_destination = destination is not None
    destination = output_directory(output_root, job, destination=destination)
    with job_lock(output_root, job["job_key"]):
        if (destination / "metadata.json").is_file():
            validate_bundle(destination / "metadata.json")
            return destination
        stage = Path(output_root).resolve() / ".staging" / job["job_key"]
        stage.mkdir(parents=True, exist_ok=True)
        try:
            inventory, tracks, caption_status, inventory_attempts = (
                await resolve_caption_inventory(
                    client, job, strict_refresh=job["require_caption"]
                )
            )
            if any(
                not caption_language_matches_content(job["content_language"], track["language"])
                for track in tracks
            ):
                raise ValueError("selected caption language does not match video content language")
            if job["require_caption"] and not tracks:
                raise ValueError("no acceptable Bilibili platform caption is available")

            part_url = f"https://www.bilibili.com/video/{job['bvid']}?p={job['part']}"
            dash = await client.dash(job["bvid"], job["cid"], job["max_height"])
            video_stream, audio_stream = select_dash_streams(dash, job["max_height"])
            raw_video = stage / "dash-video.bin"
            raw_audio = stage / "dash-audio.bin"
            await _download_stream(
                client.session, video_stream, raw_video, maximum=MAX_MEDIA_BYTES,
                expected_kind="video", proxy=client.proxy,
            )
            await _download_stream(
                client.session, audio_stream, raw_audio, maximum=MAX_MEDIA_BYTES,
                expected_kind="audio", proxy=client.proxy,
            )
            video = stage / "source.mp4"
            merge_mode = _merge_dash(raw_video, raw_audio, video)
            audio = stage / "audio.wav"
            _extract_wav(video, audio)

            media_summary = _media_summary(video)
            audio_summary = _media_summary(audio)
            files = {
                "video": _file_record(video, stage),
                "audio": _file_record(audio, stage),
            }
            accepted, rejected = await prepare_caption_payloads(
                client, tracks, stage,
                media_summary["duration_seconds"],
            )
            if rejected and not accepted and job["require_caption"]:
                raise ValueError("no acceptable Bilibili platform caption is available")
            for item in [*accepted, *rejected]:
                for extension, path in item["paths"].items():
                    key = item["track"]["files"][extension]
                    files[key] = _file_record(path, stage)
            captions = [item["track"] for item in accepted]
            rejected_captions = [item["track"] for item in rejected]

            if sum(record["bytes"] for record in files.values()) > MAX_BUNDLE_BYTES:
                raise ValueError("bundle exceeds the total byte limit")
            caption = {
                "status": (
                    "invalid_timeline" if rejected and not captions
                    else "downloaded" if captions else caption_status
                ),
                "need_login_subtitle": inventory["need_login_subtitle"],
                "response_authenticated": client.authenticated,
                "requested_languages": job["languages"],
                "track_count": len(captions),
                "tracks": captions,
                "inventory_attempt_count": len(inventory_attempts),
                "inventory_attempts": inventory_attempts,
            }
            if rejected_captions:
                caption.update({
                    "payload_status": "partial" if captions else "rejected",
                    "rejected_track_count": len(rejected_captions),
                    "rejected_tracks": rejected_captions,
                })
            sidecar = {
                "schema_version": SCHEMA_VERSION,
                "asset_type": "bilibili_parent",
                "source": "bilibili",
                "source_id": f"{job['bvid']}_p{job['part']}",
                "storage_layout": (
                    "integrated_queue" if integrated_destination else "legacy_dataset"
                ),
                "bvid": job["bvid"],
                "aid": job.get("aid"),
                "cid": job["cid"],
                "part": job["part"],
                "part_title": job["part_title"],
                "declared_duration_seconds": job["duration_seconds"],
                "job_key": job["job_key"],
                "canonical_url": part_url,
                "content_language": job["content_language"],
                "program": job["program"],
                "speaker_count": job["speaker_count"],
                "speaker_count_status": job["speaker_count_status"],
                "rights": job["rights"],
                "rights_cleared": _rights_cleared(job),
                "ai_generation": job["ai_generation"],
                "caption": caption,
                "source_metadata": _metadata_view(view),
                "source_streams": {
                    "video": stream_descriptor(video_stream),
                    "audio": stream_descriptor(audio_stream),
                    "merge_mode": merge_mode,
                },
                "acquisition_policy": {
                    "max_height": job["max_height"],
                    "max_duration_seconds": job["max_duration_seconds"],
                    "require_caption": job["require_caption"],
                    "source_revision": job["source_revision"],
                },
                "media": media_summary,
                "audio": audio_summary,
                "files": files,
                "acquired_at": utc_now(),
                "toolchain": {
                    "encoding_profile": ENCODING_PROFILE,
                },
            }
            for transient in (
                raw_video, raw_audio,
                raw_video.with_suffix(raw_video.suffix + ".state.json"),
                raw_audio.with_suffix(raw_audio.suffix + ".state.json"),
            ):
                transient.unlink(missing_ok=True)
            (stage / "failure.json").unlink(missing_ok=True)
            write_json_atomic(stage / "metadata.json", sidecar)
            validate_bundle(stage / "metadata.json", allow_staging=True)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise FileExistsError(f"incomplete destination already exists: {destination}")
            os.replace(stage, destination)
            validate_bundle(destination / "metadata.json")
            return destination
        except Exception as exc:
            write_json_atomic(stage / "failure.json", {
                "status": "failed", "job_key": job["job_key"],
                "failed_at": utc_now(), "error": str(exc)[:2000],
            })
            raise


def _safe_file(bundle: Path, record: dict[str, Any], name: str) -> Path:
    relative = Path(str(record.get("path") or ""))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{name} path escapes its bundle")
    unresolved = bundle / relative
    if unresolved.is_symlink():
        raise ValueError(f"{name} file is missing, linked or outside its bundle")
    path = unresolved.resolve()
    if bundle not in path.parents or not path.is_file():
        raise ValueError(f"{name} file is missing, linked or outside its bundle")
    if path.stat().st_size != int(record.get("bytes", -1)):
        raise ValueError(f"{name} byte count mismatch")
    if sha256_file(path) != record.get("sha256"):
        raise ValueError(f"{name} SHA-256 mismatch")
    return path


def _validate_inventory_attempts(caption: dict[str, Any]) -> None:
    attempts = caption.get("inventory_attempts")
    count = caption.get("inventory_attempt_count")
    if attempts is None and count is None:
        if caption.get("status") == "invalid_timeline" or any(
            key in caption for key in (
                "payload_status", "rejected_track_count", "rejected_tracks",
            )
        ):
            raise ValueError("caption rejection evidence cannot use legacy compatibility")
        return  # schema-v1 bundles written before diagnostic attempts remain valid.
    if not isinstance(attempts, list) or type(count) is not int:
        raise ValueError("caption inventory attempt diagnostics are incomplete")
    if count != len(attempts) or not 1 <= count <= 2:
        raise ValueError("caption inventory attempt count is invalid")
    allowed_attempt_keys = {
        "attempt", "inventory_status", "need_login_subtitle", "raw_track_count",
        "selected_track_count", "selection_status", "tracks",
    }
    refresh_failed_keys = {"attempt", "inventory_status", "error_type"}
    allowed_track_keys = {
        "index", "track_value_type", "id_str", "language", "track_type",
        "ai_type", "ai_status", "is_lock", "url_present", "url_value_type",
        "url_form", "url_host", "url_port", "rejection_reason",
    }
    inventory_statuses = {
        "provided", "auth_required", "not_provided_publicly", "unknown",
    }
    selection_statuses = {
        "downloadable", "auth_required", "not_provided_publicly",
        "no_matching_language", "invalid_track_inventory", "unknown",
    }
    value_types = {"str", "dict", "list", "int", "float", "bool", "NoneType"}
    track_value_types = value_types - {"dict"}
    url_forms = {"missing", "scheme_relative", "https", "http", "other_scheme", "relative"}
    rejection_reasons = {
        "accepted", "missing_url", "url_not_string", "invalid_port",
        "unsupported_scheme", "credentials_not_allowed", "non_default_port",
        "missing_host", "host_not_allowed", "track_not_object",
    }
    for index, attempt in enumerate(attempts, 1):
        if not isinstance(attempt, dict):
            raise ValueError("caption inventory attempt shape is invalid")
        if attempt.get("attempt") != index:
            raise ValueError("caption inventory attempt order is invalid")
        if set(attempt) == refresh_failed_keys:
            if (
                index != 2
                or attempt.get("inventory_status") != "refresh_failed"
                or not isinstance(attempt.get("error_type"), str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", attempt["error_type"])
            ):
                raise ValueError("caption inventory refresh failure is invalid")
            continue
        if set(attempt) != allowed_attempt_keys:
            raise ValueError("caption inventory attempt shape is invalid")
        raw_count = attempt.get("raw_track_count")
        selected_count = attempt.get("selected_track_count")
        diagnostics = attempt.get("tracks")
        inventory_status = attempt.get("inventory_status")
        selection_status = attempt.get("selection_status")
        need_login = attempt.get("need_login_subtitle")
        if (
            not isinstance(inventory_status, str) or inventory_status not in inventory_statuses
            or not isinstance(selection_status, str) or selection_status not in selection_statuses
            or need_login is not True and need_login is not False and need_login is not None
            or type(raw_count) is not int or raw_count < 0
            or type(selected_count) is not int or selected_count < 0
            or selected_count > raw_count
            or not isinstance(diagnostics, list) or len(diagnostics) != raw_count
            or (attempt["selection_status"] == "downloadable") != (selected_count > 0)
            or (attempt["inventory_status"] == "provided") != (raw_count > 0)
        ):
            raise ValueError("caption inventory track counts are invalid")
        for track_index, diagnostic in enumerate(diagnostics):
            if not isinstance(diagnostic, dict) or not set(diagnostic).issubset(allowed_track_keys):
                raise ValueError("caption inventory track diagnostic shape is invalid")
            if diagnostic.get("index") != track_index:
                raise ValueError("caption inventory track diagnostic order is invalid")
            track_value_type = diagnostic.get("track_value_type")
            if not isinstance(track_value_type, str):
                raise ValueError("caption inventory diagnostic value type is invalid")
            if track_value_type == "dict":
                if set(diagnostic) != allowed_track_keys:
                    raise ValueError("caption inventory object diagnostic is incomplete")
                url_value_type = diagnostic.get("url_value_type")
                url_form = diagnostic.get("url_form")
                rejection_reason = diagnostic.get("rejection_reason")
                url_port = diagnostic.get("url_port")
                host = diagnostic.get("url_host")
                if (
                    type(diagnostic.get("url_present")) is not bool
                    or not isinstance(url_value_type, str) or url_value_type not in value_types
                    or not isinstance(url_form, str) or url_form not in url_forms
                    or not isinstance(rejection_reason, str)
                    or rejection_reason not in rejection_reasons - {"track_not_object"}
                    or not isinstance(diagnostic.get("id_str"), str)
                    or not re.fullmatch(r"[A-Za-z0-9._-]{0,80}", diagnostic["id_str"])
                    or not isinstance(diagnostic.get("language"), str)
                    or not re.fullmatch(r"[A-Za-z0-9._-]{0,40}", diagnostic["language"])
                    or any(
                        value is not None and (type(value) is not int or abs(value) > 2**31)
                        for value in (
                            diagnostic.get("track_type"), diagnostic.get("ai_type"),
                            diagnostic.get("ai_status"),
                        )
                    )
                    or url_port is not None
                    and (type(url_port) is not int or not 1 <= url_port <= 65_535)
                    or diagnostic.get("is_lock") is not None
                    and type(diagnostic.get("is_lock")) is not bool
                    or rejection_reason == "accepted" and not (
                        url_value_type == "str"
                        and url_form in {"https", "scheme_relative"}
                        and isinstance(host, str)
                        and (host == "hdslb.com" or host.endswith(".hdslb.com"))
                        and url_port in {None, 443}
                    )
                ):
                    raise ValueError("caption inventory object diagnostic values are invalid")
            elif (
                set(diagnostic) != {"index", "track_value_type", "rejection_reason"}
                or track_value_type not in track_value_types
                or diagnostic.get("rejection_reason") != "track_not_object"
            ):
                raise ValueError("caption inventory non-object diagnostic values are invalid")
            host = diagnostic.get("url_host", "")
            if not isinstance(host, str) or not re.fullmatch(r"[a-z0-9.-]{0,253}", host):
                raise ValueError("caption inventory diagnostic host is unsafe")
    if count == 2 and not (
        attempts[0]["inventory_status"] == "provided"
        and attempts[0]["selected_track_count"] == 0
    ):
        raise ValueError("caption inventory refresh was not eligible")
    if count == 1 and (
        attempts[0]["inventory_status"] == "provided"
        and attempts[0]["selected_track_count"] == 0
    ):
        raise ValueError("caption inventory omitted its eligible refresh")

    top_status = caption.get("status")
    top_need_login = caption.get("need_login_subtitle")
    top_track_count = caption.get("track_count")
    top_tracks = caption.get("tracks")
    payload_status = caption.get("payload_status")
    rejected_track_count = caption.get("rejected_track_count")
    rejected_tracks = caption.get("rejected_tracks")
    has_rejected_fields = any(
        key in caption for key in (
            "payload_status", "rejected_track_count", "rejected_tracks",
        )
    )
    if has_rejected_fields:
        if (
            payload_status not in {"partial", "rejected"}
            or type(rejected_track_count) is not int
            or rejected_track_count <= 0
            or not isinstance(rejected_tracks, list)
            or rejected_track_count != len(rejected_tracks)
            or any(not isinstance(track, dict) for track in rejected_tracks)
        ):
            raise ValueError("caption rejected-track summary is invalid")
    else:
        rejected_tracks = []
        rejected_track_count = 0
    final_attempt = attempts[-1]
    if final_attempt["inventory_status"] == "refresh_failed":
        evidence_attempt = attempts[0]
        expected_status = evidence_attempt["selection_status"]
    else:
        evidence_attempt = final_attempt
        if evidence_attempt["selected_track_count"] > 0:
            expected_status = "downloaded"
        elif count == 2:
            expected_status = attempts[0]["selection_status"]
        else:
            expected_status = evidence_attempt["selection_status"]
    accepted_count = len(top_tracks) if isinstance(top_tracks, list) else -1
    payload_count = accepted_count + rejected_track_count
    if evidence_attempt["selected_track_count"] > 0:
        expected_status = "downloaded" if accepted_count > 0 else "invalid_timeline"
    if rejected_track_count:
        expected_payload_status = "partial" if accepted_count > 0 else "rejected"
        if payload_status != expected_payload_status:
            raise ValueError("caption payload status is inconsistent")
    expected_track_count = accepted_count
    if (
        top_status != expected_status
        or top_need_login is not evidence_attempt["need_login_subtitle"]
        or type(top_track_count) is not int
        or not isinstance(top_tracks, list)
        or top_track_count != len(top_tracks)
        or top_track_count != expected_track_count
        or payload_count != evidence_attempt["selected_track_count"]
    ):
        raise ValueError("caption summary does not match its final inventory evidence")
    if payload_count:
        available_identities = Counter(
            (track.get("id_str"), track.get("language"))
            for track in evidence_attempt["tracks"]
            if track.get("track_value_type") == "dict"
            and track.get("rejection_reason") == "accepted"
        )
        for track in [*top_tracks, *rejected_tracks]:
            identity = (track.get("id_str"), track.get("language"))
            if available_identities[identity] <= 0:
                raise ValueError("caption track is absent from final inventory evidence")
            available_identities[identity] -= 1
    rendered = json.dumps(attempts, ensure_ascii=False).lower()
    if any(marker in rendered for marker in (
        "auth_key", "token=", "sessdata", "bili_jct",
    )):
        raise ValueError("caption inventory diagnostics contain a secret marker")


def validate_bundle(sidecar_path: Path, *, allow_staging: bool = False) -> dict[str, Any]:
    sidecar_path = Path(sidecar_path).resolve()
    bundle = sidecar_path.parent
    metadata = json.loads(sidecar_path.read_text(encoding="utf-8"))
    if metadata.get("schema_version") != SCHEMA_VERSION or metadata.get("asset_type") != "bilibili_parent":
        raise ValueError("bundle sidecar has an unsupported schema or asset type")
    if metadata.get("job_key") != bundle.name:
        raise ValueError("bundle directory does not match job_key")
    if metadata.get("source") != "bilibili" or not BVID_RE.fullmatch(str(metadata.get("bvid") or "")):
        raise ValueError("bundle has invalid source identity")
    bvid = metadata["bvid"]
    cid = metadata.get("cid")
    part = metadata.get("part")
    if type(cid) is not int or cid <= 0 or type(part) is not int or not 1 <= part <= MAX_PARTS:
        raise ValueError("bundle has invalid cid or part")
    if metadata.get("source_id") != f"{bvid}_p{part}":
        raise ValueError("bundle source_id does not match bvid/part")
    if metadata.get("canonical_url") != f"https://www.bilibili.com/video/{bvid}?p={part}":
        raise ValueError("bundle canonical_url does not match bvid/part")
    layout = metadata.get("storage_layout") or (
        "integrated_queue"
        if bundle.parent.name == metadata.get("source_id")
        else "legacy_dataset"
    )
    formal_hierarchy = (
        layout == "legacy_dataset"
        and bundle.parent.name == f"p{part}"
        and bundle.parent.parent.name == bvid
    )
    integrated_hierarchy = (
        layout == "integrated_queue"
        and bundle.parent.name == metadata.get("source_id")
    )
    staging_hierarchy = allow_staging and bundle.parent.name == ".staging"
    if not formal_hierarchy and not integrated_hierarchy and not staging_hierarchy:
        raise ValueError("bundle directory hierarchy does not match bvid/part")
    if type(metadata.get("aid")) is not int or metadata["aid"] <= 0:
        raise ValueError("bundle has invalid aid")
    if not isinstance(metadata.get("content_language"), str) or not metadata["content_language"]:
        raise ValueError("bundle content_language is missing")
    if not isinstance(metadata.get("source_metadata"), dict):
        raise ValueError("bundle source_metadata is missing")
    source_streams = metadata.get("source_streams")
    if not isinstance(source_streams, dict) or not all(
        isinstance(source_streams.get(name), dict) for name in ("video", "audio")
    ):
        raise ValueError("bundle source_stream descriptors are missing")
    if "url" in json.dumps(source_streams, ensure_ascii=False).lower():
        raise ValueError("bundle source_streams must not contain URLs")
    toolchain = metadata.get("toolchain")
    if not isinstance(toolchain, dict) or toolchain.get("encoding_profile") != ENCODING_PROFILE:
        raise ValueError("bundle toolchain is invalid")
    if not isinstance(metadata.get("acquired_at"), str) or not metadata["acquired_at"]:
        raise ValueError("bundle acquired_at is missing")
    rights = metadata.get("rights")
    if not isinstance(rights, dict) or rights.get("status") not in RIGHTS_STATUSES:
        raise ValueError("bundle rights are invalid")
    expected_clearance = rights.get("status") in CLEARED_RIGHTS and bool(
        rights.get("evidence_url") or rights.get("evidence_text")
    )
    if metadata.get("rights_cleared") is not expected_clearance:
        raise ValueError("bundle rights_cleared is inconsistent")
    ai_generation = metadata.get("ai_generation")
    if not isinstance(ai_generation, dict) or ai_generation.get("status") not in AI_GENERATION_STATUSES:
        raise ValueError("bundle ai_generation is invalid")
    speaker_count = metadata.get("speaker_count")
    speaker_status = metadata.get("speaker_count_status")
    if speaker_count is None and speaker_status != "needs_review":
        raise ValueError("unlabeled speaker_count must stay needs_review")
    if speaker_count is not None and (
        type(speaker_count) is not int or speaker_count <= 0 or speaker_status != "verified_manual"
    ):
        raise ValueError("speaker_count must be a positive manually verified integer")
    files = metadata.get("files")
    if not isinstance(files, dict) or not {"video", "audio"}.issubset(files):
        raise ValueError("bundle files closure is incomplete")
    paths = {name: _safe_file(bundle, record, name) for name, record in files.items()}
    recorded_paths = [str(record.get("path") or "") for record in files.values()]
    if len(set(recorded_paths)) != len(recorded_paths):
        raise ValueError("bundle files map contains duplicate paths")
    if sum(path.stat().st_size for path in paths.values()) > MAX_BUNDLE_BYTES:
        raise ValueError("bundle exceeds the total byte limit")
    video = _media_summary(paths["video"])
    audio = _media_summary(paths["audio"])
    if video["video_stream_count"] < 1:
        raise ValueError("source.mp4 has no video stream")
    if video["audio_stream_count"] < 1:
        raise ValueError("source.mp4 has no audio stream")
    policy = metadata.get("acquisition_policy") or {}
    max_height = int(policy.get("max_height") or 0)
    if not 144 <= max_height <= 1080:
        raise ValueError("bundle max_height policy is invalid")
    if video["height"] and int(video["height"]) > max_height:
        raise ValueError("source.mp4 exceeds the requested height limit")
    if audio["audio_stream_count"] != 1 or audio["video_stream_count"] != 0:
        raise ValueError("audio.wav must contain exactly one audio stream")
    if audio["audio_codec"] != "pcm_s16le" or audio["sample_rate"] != 16000 or audio["channels"] != 1:
        raise ValueError("audio.wav must be 16 kHz mono PCM16")
    if video["duration_seconds"] <= 0 or audio["duration_seconds"] <= 0:
        raise ValueError("bundle media has invalid duration")
    if abs(video["duration_seconds"] - audio["duration_seconds"]) > 0.5:
        raise ValueError("video/audio duration drift exceeds 0.5 seconds")
    declared_duration = float(metadata.get("declared_duration_seconds") or 0)
    if not math.isfinite(declared_duration) or declared_duration <= 0:
        raise ValueError("bundle declared_duration_seconds is invalid")
    if abs(video["duration_seconds"] - declared_duration) > 2.0:
        raise ValueError("actual media duration differs from the declared part duration")
    for name, observed in (("media", video), ("audio", audio)):
        recorded_summary = metadata.get(name)
        if not isinstance(recorded_summary, dict):
            raise ValueError(f"bundle {name} probe summary is missing")
        if recorded_summary != observed:
            raise ValueError(f"bundle {name} probe summary is stale")

    caption = metadata.get("caption")
    empty_statuses = {
        "auth_required", "not_provided_publicly", "no_matching_language",
        "invalid_track_inventory", "invalid_timeline", "unknown",
    }
    if not isinstance(caption, dict) or caption.get("status") not in {"downloaded", *empty_statuses}:
        raise ValueError("bundle caption status is invalid")
    tracks = caption.get("tracks")
    if not isinstance(tracks, list) or caption.get("track_count") != len(tracks):
        raise ValueError("bundle caption track count is invalid")
    if caption["status"] in empty_statuses and tracks:
        raise ValueError("empty caption status cannot contain tracks")
    if caption["status"] == "downloaded" and not tracks:
        raise ValueError("downloaded caption status requires tracks")
    _validate_inventory_attempts(caption)
    require_caption = policy.get("require_caption")
    if not isinstance(require_caption, bool):
        raise ValueError("bundle require_caption policy is invalid")
    if require_caption and caption["status"] != "downloaded":
        raise ValueError("require_caption bundle has no downloaded caption")
    need_login = caption.get("need_login_subtitle")
    if caption["status"] == "auth_required" and need_login is not True:
        raise ValueError("auth_required caption must record need_login_subtitle=true")
    if caption["status"] == "not_provided_publicly" and need_login is not False:
        raise ValueError("publicly missing caption must record need_login_subtitle=false")
    expected_file_keys = {"video", "audio"}
    for index, track in enumerate(tracks):
        kind = track.get("kind")
        source = track.get("text_source")
        expected = {
            "manual": "platform_manual", "automatic": "platform_auto",
            "unknown": "platform_unknown",
        }.get(kind)
        if (
            source != expected
            or track.get("status") != "downloaded"
            or "timeline_rejection" in track
            or "url" in track
            or "selection_rank" in track
        ):
            raise ValueError(f"caption track {index} has inconsistent provenance")
        redacted = urlsplit(str(track.get("url_redacted") or ""))
        redacted_host = (redacted.hostname or "").lower().rstrip(".")
        if (
            redacted.scheme != "https" or redacted.username or redacted.password
            or redacted.query or redacted.fragment or redacted.port not in {None, 443}
            or not (
                redacted_host == "hdslb.com"
                or redacted_host.endswith(".hdslb.com")
            )
        ):
            raise ValueError(f"caption track {index} has unsafe redacted URL")
        if not caption_language_matches_content(metadata["content_language"], track.get("language", "")):
            raise ValueError(f"caption track {index} language does not match media content")
        raw_track = {
            "type": track.get("track_type"),
            "lan": track.get("language"),
            "lan_doc": track.get("label"),
            "lan_doc_brief": track.get("label_brief"),
            "ai_type": track.get("ai_type"),
            "ai_status": track.get("ai_status"),
            "author": track.get("author"),
        }
        recomputed = classify_subtitle_track(raw_track)
        for field in (
            "kind", "text_source", "selected_by_rule", "translation_kind", "rule_version",
        ):
            if track.get(field) != recomputed[field]:
                raise ValueError(f"caption track {index} {field} does not match classifier")
        keys = track.get("files") or {}
        if set(keys) != {"json", "vtt", "txt"}:
            raise ValueError(f"caption track {index} has invalid file references")
        if any(key not in paths for key in keys.values()):
            raise ValueError(f"caption track {index} references a missing file")
        if not str(track.get("id_str") or "") or not str(track.get("language") or ""):
            raise ValueError(f"caption track {index} identity is incomplete")
        expected_file_keys.update(keys.values())
        document = json.loads(paths[keys["json"]].read_text(encoding="utf-8"))
        if sanitize_subtitle_document(document) != document:
            raise ValueError(f"caption track {index} JSON contains transport credentials")
        cues = parse_subtitle_document(document)
        if int(track.get("cue_count", -1)) != len(cues):
            raise ValueError(f"caption track {index} cue count mismatch")
        if paths[keys["vtt"]].read_text(encoding="utf-8") != render_subtitle_vtt(cues):
            raise ValueError(f"caption track {index} VTT is not derived from JSON")
        if paths[keys["txt"]].read_text(encoding="utf-8") != render_subtitle_text(cues):
            raise ValueError(f"caption track {index} TXT is not derived from JSON")
        if max(cue.end for cue in cues) - video["duration_seconds"] > 2.0:
            raise ValueError(f"caption track {index} extends too far beyond the media")
    for index, track in enumerate(caption.get("rejected_tracks") or []):
        kind = track.get("kind")
        source = track.get("text_source")
        expected = {
            "manual": "platform_manual", "automatic": "platform_auto",
            "unknown": "platform_unknown",
        }.get(kind)
        if (
            source != expected or track.get("status") != "rejected"
            or "url" in track or "selection_rank" in track
        ):
            raise ValueError(f"rejected caption track {index} has inconsistent provenance")
        redacted = urlsplit(str(track.get("url_redacted") or ""))
        redacted_host = (redacted.hostname or "").lower().rstrip(".")
        if (
            redacted.scheme != "https" or redacted.username or redacted.password
            or redacted.query or redacted.fragment or redacted.port not in {None, 443}
            or not (
                redacted_host == "hdslb.com"
                or redacted_host.endswith(".hdslb.com")
            )
        ):
            raise ValueError(f"rejected caption track {index} has unsafe redacted URL")
        if not caption_language_matches_content(
            metadata["content_language"], track.get("language", "")
        ):
            raise ValueError(f"rejected caption track {index} language does not match media")
        raw_track = {
            "type": track.get("track_type"),
            "lan": track.get("language"),
            "lan_doc": track.get("label"),
            "lan_doc_brief": track.get("label_brief"),
            "ai_type": track.get("ai_type"),
            "ai_status": track.get("ai_status"),
            "author": track.get("author"),
        }
        recomputed = classify_subtitle_track(raw_track)
        for field in (
            "kind", "text_source", "selected_by_rule", "translation_kind",
            "rule_version",
        ):
            if track.get(field) != recomputed[field]:
                raise ValueError(
                    f"rejected caption track {index} {field} does not match classifier"
                )
        keys = track.get("files") or {}
        if set(keys) != {"json"} or keys["json"] not in paths:
            raise ValueError(f"rejected caption track {index} has invalid file reference")
        if not str(track.get("id_str") or "") or not str(track.get("language") or ""):
            raise ValueError(f"rejected caption track {index} identity is incomplete")
        expected_file_keys.add(keys["json"])
        document = json.loads(paths[keys["json"]].read_text(encoding="utf-8"))
        if sanitize_subtitle_document(document) != document:
            raise ValueError(
                f"rejected caption track {index} JSON contains transport credentials"
            )
        cues = parse_subtitle_document(document)
        if int(track.get("cue_count", -1)) != len(cues):
            raise ValueError(f"rejected caption track {index} cue count mismatch")
        rejection = caption_timeline_rejection(cues, video["duration_seconds"])
        if rejection is None or track.get("timeline_rejection") != rejection:
            raise ValueError(f"rejected caption track {index} timeline evidence mismatch")
    if set(files) != expected_file_keys:
        raise ValueError("bundle files map has an unexpected closure")
    serialized = json.dumps(metadata, ensure_ascii=False).lower()
    if any(marker in serialized for marker in (
        "auth_key=", "sessdata=", "bili_jct=", "authorization:", "cookie:",
    )):
        raise ValueError("bundle sidecar contains a secret marker")
    recorded = {str(record["path"]) for record in files.values()}
    actual = set()
    for path in bundle.rglob("*"):
        relative = str(path.relative_to(bundle))
        if path.is_symlink():
            raise ValueError("bundle contains a symbolic link")
        if path.is_dir():
            raise ValueError("bundle contains an unexpected nested directory")
        if path.is_file() and relative != "metadata.json":
            actual.add(relative)
    if actual != recorded:
        raise ValueError("bundle contains unrecorded or missing regular files")
    return {"metadata": metadata, "paths": paths, "video": video, "audio": audio}


def audit_dataset(
    output_root: Path, expected_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    root = Path(output_root).resolve()
    sidecars = sorted((root / "parents").glob("**/metadata.json"))
    failures = []
    counts = Counter()
    found_parts: set[tuple[str, int]] = set()
    for sidecar in sidecars:
        try:
            result = validate_bundle(sidecar)
            metadata = result["metadata"]
            counts["valid_bundle_count"] += 1
            found_parts.add((metadata["bvid"], metadata["part"]))
            counts[f"caption_{metadata['caption']['status']}"] += 1
            for track in metadata["caption"]["tracks"]:
                counts[f"caption_kind_{track['kind']}"] += 1
        except Exception as exc:
            failures.append({"path": str(sidecar.relative_to(root)), "error": str(exc)[:2000]})
    staging = [path for path in (root / ".staging").glob("*") if path.is_dir()]
    if not sidecars:
        failures.append({"path": "parents", "error": "dataset contains no completed bundles"})
    if staging:
        failures.append({"path": ".staging", "error": f"{len(staging)} incomplete jobs remain"})
    if expected_items is not None:
        expected_parts = {
            (item["bvid"], part) for item in expected_items for part in item["parts"]
        }
        missing = sorted(expected_parts - found_parts)
        for bvid, part in missing:
            failures.append({
                "path": f"parents/{bvid}/p{part}", "error": "manifest part has no valid bundle",
            })
    return {
        "root": str(root),
        "discovered_sidecar_count": len(sidecars),
        "valid_bundle_count": counts["valid_bundle_count"],
        "incomplete_staging_count": len(staging),
        "counts": dict(sorted(counts.items())),
        "failure_count": len(failures),
        "failures": failures,
        "audited_at": utc_now(),
    }


def repair_dataset_metadata(output_root: Path) -> dict[str, Any]:
    """Upgrade derived sidecar fields without replacing source media or captions."""

    root = Path(output_root).resolve()
    declared_by_job: dict[str, float] = {}
    for manifest in sorted((root / "manifests").glob("*.json")):
        try:
            document = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for item in document.get("items") or []:
            if isinstance(item, dict) and item.get("job_key") and item.get("duration_seconds"):
                declared_by_job[str(item["job_key"])] = float(item["duration_seconds"])
    repaired = 0
    failures = []
    for sidecar in sorted((root / "parents").glob("**/metadata.json")):
        try:
            metadata = json.loads(sidecar.read_text(encoding="utf-8"))
            if not metadata.get("declared_duration_seconds"):
                declared = declared_by_job.get(str(metadata.get("job_key") or ""))
                if declared is None:
                    raise ValueError("no run manifest contains the declared part duration")
                metadata["declared_duration_seconds"] = declared
                metadata["declared_duration_source"] = "sanitized_run_manifest"
            metadata["media"] = _media_summary(sidecar.parent / metadata["files"]["video"]["path"])
            metadata["audio"] = _media_summary(sidecar.parent / metadata["files"]["audio"]["path"])
            metadata["repaired_at"] = utc_now()
            write_json_atomic(sidecar, metadata)
            validate_bundle(sidecar)
            repaired += 1
        except Exception as exc:
            failures.append({
                "path": str(sidecar.relative_to(root)),
                "error": redact_urls_in_text(str(exc))[:2000],
            })
    return {"repaired": repaired, "failure_count": len(failures), "failures": failures}


async def _inspect_or_download(args: argparse.Namespace, *, download: bool) -> int:
    import aiohttp

    items = load_manifest(args.manifest)
    Path(args.output).mkdir(parents=True, exist_ok=True)
    write_json_atomic(Path(args.output) / ".audiospider-dataset.json", {
        "dataset_type": "bilibili_video", "schema_version": SCHEMA_VERSION,
    })
    timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_connect=30, sock_read=60)
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}
    cookie = os.environ.get("BILIBILI_COOKIE", "")
    os.environ.pop("BILIBILI_COOKIE", None)
    if cookie:
        if len(cookie) > 16_384 or "\r" in cookie or "\n" in cookie:
            raise ValueError("BILIBILI_COOKIE is too long or contains a newline")
    report = {"status": "running", "mode": "download" if download else "inspect", "items": []}
    manifests = Path(args.output).resolve() / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    name = f"{report['mode']}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.json"
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            client = BilibiliClient(
                session, auth_cookie=cookie, proxy=get_bilibili_proxy()
            )
            for item in items:
                try:
                    view = await client.view(item["bvid"])
                    jobs = build_jobs(item, view)
                except Exception as exc:
                    report["items"].append({
                        "bvid": item["bvid"], "status": "failed",
                        "error": redact_urls_in_text(str(exc))[:2000],
                    })
                    continue
                for job in jobs:
                    result = {
                        "job_key": job["job_key"], "bvid": job["bvid"], "cid": job["cid"],
                        "part": job["part"], "title": str(view.get("title") or ""),
                        "part_title": job["part_title"], "duration_seconds": job["duration_seconds"],
                        "require_caption": job["require_caption"],
                    }
                    try:
                        inventory = await client.caption_inventory(job["bvid"], job["cid"])
                        tracks = select_caption_tracks(inventory["tracks"], job["languages"])
                        caption_status = caption_selection_status(inventory, tracks, job["languages"])
                        result.update({
                            "caption_track_count": len(tracks),
                            "caption_status": caption_status,
                            "need_login_subtitle": inventory["need_login_subtitle"],
                            "response_authenticated": client.authenticated,
                            "captions": [
                                {key: value for key, value in track.items()
                                 if key not in {"url", "selection_rank"}}
                                for track in tracks
                            ],
                        })
                        if job["require_caption"] and not tracks:
                            result.update({
                                "status": "rejected", "error": "no acceptable platform caption",
                            })
                        elif download:
                            destination = await download_job(client, job, view, args.output)
                            result.update({"status": "downloaded", "path": str(destination)})
                        else:
                            result["status"] = "accepted"
                    except Exception as exc:
                        result.update({
                            "status": "failed", "error": redact_urls_in_text(str(exc))[:2000],
                        })
                    report["items"].append(result)
        report["status"] = (
            "completed" if all(row["status"] not in {"rejected", "failed"}
                               for row in report["items"])
            else "completed_with_failures"
        )
    finally:
        if report["status"] == "running":
            report["status"] = "aborted"
        write_json_atomic(manifests / name, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(row["status"] not in {"rejected", "failed"} for row in report["items"]) else 1


def _positive_parts(value: str) -> int:
    number = int(value)
    if not 1 <= number <= MAX_PARTS:
        raise argparse.ArgumentTypeError(f"expected 1-{MAX_PARTS}")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="下载并审计 B 站视频、WAV 和平台字幕")
    parser.add_argument("--output", type=Path, required=True, help="数据集输出目录")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "download"):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--manifest", type=Path, required=True)
    audit = subparsers.add_parser("audit")
    audit.add_argument("--manifest", type=Path)
    subparsers.add_parser("repair-metadata")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "audit":
        expected = load_manifest(args.manifest) if args.manifest else None
        report = audit_dataset(args.output, expected_items=expected)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["failure_count"] == 0 else 1
    if args.command == "repair-metadata":
        report = repair_dataset_metadata(args.output)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["failure_count"] == 0 else 1
    return asyncio.run(_inspect_or_download(args, download=args.command == "download"))


if __name__ == "__main__":
    sys.exit(main())
