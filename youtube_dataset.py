#!/usr/bin/env python3
"""Compatibility, repair, and audit library for complete YouTube video bundles."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlsplit, urlunsplit

from youtube_vtt import Cue, CueGroup, group_cues, parse_vtt, render_vtt


SCHEMA_VERSION = 1
GROUPING_ALGORITHM_VERSION = "subtitle-group-v2"
ENCODING_PROFILE_VERSION = "h264-aac-wav16k-v1"
MAX_MANIFEST_BYTES = 10 * 1024 * 1024
MAX_CAPTION_BYTES = 50 * 1024 * 1024
MAX_BUNDLE_BYTES = 10 * 1024 * 1024 * 1024
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
MEDIA_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
CLEARED_RIGHTS = {"licensed", "public_domain", "permission_granted", "creative_commons"}
RIGHTS_STATUSES = CLEARED_RIGHTS | {"needs_review", "unknown"}
AI_GENERATION_STATUSES = {"declared", "not_declared", "suspected", "unknown"}


@dataclass(frozen=True)
class Profile:
    name: str
    minimum_duration: float
    maximum_duration: float
    target_duration: float | None
    minimum_speakers: int
    maximum_speakers: int

    def accepts_duration(self, duration: float) -> bool:
        return self.minimum_duration <= float(duration) <= self.maximum_duration


PROFILES = {
    "youtube_interviews": Profile(
        "youtube_interviews", 25.5 * 60, 60.6 * 60, 44.3 * 60, 2, 11
    ),
    "youtube_screen_clips": Profile(
        "youtube_screen_clips", 0.418, 29.888, 11.5, 1, 6
    ),
}


@dataclass(frozen=True)
class CaptionSelection:
    language: str
    kind: str
    text_source: str
    ext: str
    name: str = ""
    url: str = ""
    source_language: str = ""
    is_translated: bool | None = None
    translation_kind: str = "unknown"
    selected_by_rule: str = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def redact_url(value: str) -> str:
    """Remove query strings, fragments and credentials from a persisted URL."""

    if not value:
        return ""
    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    if not hostname:
        return ""
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit((parsed.scheme, hostname + port, parsed.path, "", ""))


def redact_urls_in_text(value: Any) -> str:
    text = str(value or "")
    return re.sub(
        r"https?://[^\s'\"<>]+",
        lambda match: redact_url(match.group(0)),
        text,
        flags=re.IGNORECASE,
    )


def youtube_video_id(url: str) -> str:
    """Return a single public YouTube video ID from a supported canonical URL."""

    if not isinstance(url, str):
        raise ValueError("YouTube URL must be text")
    parsed = urlsplit(url.strip())
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise ValueError("only credential-free HTTPS YouTube URLs are accepted")
    host = (parsed.hostname or "").lower().rstrip(".")
    candidate = ""
    if host in {"youtu.be", "www.youtu.be"}:
        candidate = parsed.path.strip("/").split("/", 1)[0]
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if parsed.path == "/watch":
            candidate = parse_qs(parsed.query).get("v", [""])[0]
        else:
            parts = parsed.path.strip("/").split("/")
            if len(parts) == 2 and parts[0] in {"shorts", "embed", "live"}:
                candidate = parts[1]
    if not VIDEO_ID_RE.fullmatch(candidate):
        raise ValueError("URL is not a supported single-video YouTube URL")
    return candidate


def _safe_component(value: str, label: str) -> str:
    if not isinstance(value, str) or not SAFE_COMPONENT_RE.fullmatch(value):
        raise ValueError(f"unsafe {label}: {value!r}")
    if value in {".", ".."}:
        raise ValueError(f"unsafe {label}: {value!r}")
    return value


def _language_family(value: str) -> str:
    return value.replace("_", "-").split("-", 1)[0].lower()


def caption_language_matches_content(content_language: str, caption_language: str) -> bool:
    """Require native-language captions; Chinese and Cantonese share one text family."""

    content_family = _language_family(content_language)
    caption_family = _language_family(caption_language)
    chinese = {"zh", "yue", "cmn"}
    if content_family in chinese:
        return caption_family in chinese
    return content_family == caption_family and content_family not in {"", "und"}


def default_caption_languages(content_language: str) -> list[str]:
    family = _language_family(content_language)
    if family == "yue":
        return ["yue", "zh-Hant", "zh-HK", "zh"]
    return [content_language] if family not in {"", "und"} else []


def validate_manifest(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate and normalize a source manifest without making network requests."""

    if not isinstance(document, dict) or not isinstance(document.get("items"), list):
        raise ValueError("manifest must contain an items array")
    if not 1 <= len(document["items"]) <= 10_000:
        raise ValueError("manifest items must contain between 1 and 10000 entries")
    result = []
    seen_job_keys: set[str] = set()
    for index, raw in enumerate(document["items"], 1):
        if not isinstance(raw, dict):
            raise ValueError(f"item {index} must be an object")
        item = dict(raw)
        profile_name = item.get("profile")
        if profile_name not in PROFILES:
            raise ValueError(f"item {index} has unsupported profile: {profile_name!r}")
        video_id = youtube_video_id(item.get("url", ""))
        languages = item.get("languages") or []
        if not isinstance(languages, list) or any(
            not isinstance(language, str) or not language.strip() for language in languages
        ):
            raise ValueError(f"item {index} languages must be an array of strings")
        languages = list(dict.fromkeys(language.strip().replace("_", "-") for language in languages))
        content_language = str(item.get("content_language") or (languages[0] if languages else "und"))
        content_language = _safe_component(content_language.replace("_", "-"), "content_language")
        if not languages:
            languages = default_caption_languages(content_language)
        if any(
            not caption_language_matches_content(content_language, language)
            for language in languages
        ):
            raise ValueError(
                f"item {index} caption languages must match content_language {content_language!r}"
            )
        maximum_parent_duration = float(item.get("max_parent_duration_seconds", 4 * 3600))
        if not math.isfinite(maximum_parent_duration) or not 0 < maximum_parent_duration <= 4 * 3600:
            raise ValueError(f"item {index} max_parent_duration_seconds must be in (0, 14400]")
        item["max_parent_duration_seconds"] = maximum_parent_duration
        require_caption = item.get("require_caption", True)
        if not isinstance(require_caption, bool):
            raise ValueError(f"item {index} require_caption must be a boolean")
        ai_generation = item.get("ai_generation") or {"status": "unknown", "evidence": []}
        if not isinstance(ai_generation, dict) or ai_generation.get("status") not in AI_GENERATION_STATUSES:
            raise ValueError(f"item {index} ai_generation has an unsupported status")
        evidence = ai_generation.get("evidence") or []
        if not isinstance(evidence, list) or any(
            not isinstance(value, str) or len(value) > 2048 for value in evidence
        ):
            raise ValueError(f"item {index} ai_generation evidence must be short strings")
        ai_generation = {"status": ai_generation["status"], "evidence": evidence}

        count = item.get("speaker_count")
        status = item.get("speaker_count_status")
        if count is None:
            status = status or "needs_review"
            if status != "needs_review":
                raise ValueError(f"item {index} cannot verify an absent speaker_count")
        else:
            if isinstance(count, bool) or not isinstance(count, int):
                raise ValueError(f"item {index} speaker_count must be an integer or null")
            profile = PROFILES[profile_name]
            if not profile.minimum_speakers <= count <= profile.maximum_speakers:
                raise ValueError(
                    f"item {index} speaker_count {count} is outside "
                    f"{profile.minimum_speakers}-{profile.maximum_speakers}"
                )
            status = status or "verified_manual"
            if status != "verified_manual":
                raise ValueError(f"item {index} speaker_count must be manually verified")

        rights = item.get("rights") or {"status": "needs_review"}
        if not isinstance(rights, dict) or not isinstance(rights.get("status"), str):
            raise ValueError(f"item {index} rights must contain a status string")
        rights = dict(rights)
        if rights["status"] not in RIGHTS_STATUSES:
            raise ValueError(f"item {index} has unsupported rights status")
        evidence_url = rights.get("evidence_url")
        if evidence_url is not None:
            if not isinstance(evidence_url, str) or len(evidence_url) > 2048:
                raise ValueError(f"item {index} rights evidence_url must be a short string")
            parsed_evidence = urlsplit(evidence_url)
            if parsed_evidence.scheme != "https" or not parsed_evidence.hostname:
                raise ValueError(f"item {index} rights evidence_url must use HTTPS")
            if parsed_evidence.username or parsed_evidence.password:
                raise ValueError(f"item {index} rights evidence_url cannot contain credentials")
            try:
                if (parsed_evidence.hostname or "").lower() in {
                    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be",
                }:
                    evidence_url = (
                        f"https://www.youtube.com/watch?v={youtube_video_id(evidence_url)}"
                    )
                else:
                    evidence_url = redact_url(evidence_url)
            except ValueError as exc:
                raise ValueError(f"item {index} rights evidence_url is invalid") from exc
            rights["evidence_url"] = evidence_url
        evidence_text = rights.get("evidence_text")
        if evidence_text is not None and (
            not isinstance(evidence_text, str) or not evidence_text.strip() or len(evidence_text) > 4096
        ):
            raise ValueError(f"item {index} rights evidence_text must be 1-4096 characters")
        revision = str(item.get("source_revision") or "current")
        _safe_component(revision, "source_revision")
        language_key = languages[0] if languages else "any"
        identity = [video_id, profile_name, languages, revision]
        if not require_caption:
            identity.insert(3, require_caption)
        fingerprint = hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:12]
        job_key = f"{video_id}-{profile_name}-{_safe_component(language_key, 'language')}-{fingerprint}"
        if job_key in seen_job_keys:
            raise ValueError(f"item {index} duplicates job_key {job_key}")
        seen_job_keys.add(job_key)
        item.update(
            {
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "video_id": video_id,
                "profile": profile_name,
                "languages": languages,
                "content_language": content_language,
                "require_caption": require_caption,
                "ai_generation": ai_generation,
                "speaker_count": count,
                "speaker_count_status": status,
                "rights": rights,
                "source_revision": revision,
                "job_key": job_key,
            }
        )
        result.append(item)
    return result


def rights_cleared(item: dict[str, Any]) -> bool:
    rights = item.get("rights") or {}
    return rights.get("status") in CLEARED_RIGHTS and bool(
        rights.get("evidence_url") or rights.get("evidence_text")
    )


def _caption_format(entries: Any) -> dict[str, Any] | None:
    if not isinstance(entries, list):
        return None
    valid = [entry for entry in entries if isinstance(entry, dict)]
    if not valid:
        return None
    return next((entry for entry in valid if entry.get("ext") == "vtt"), valid[0])


def select_caption(info: dict[str, Any], preferred_languages: Iterable[str]) -> CaptionSelection | None:
    """Select one platform caption with deterministic manual-first provenance."""

    requested = [value.replace("_", "-") for value in preferred_languages if value]
    sources = [
        ("manual", "platform_manual", info.get("subtitles") or {}),
        ("automatic", "platform_auto", info.get("automatic_captions") or {}),
    ]

    candidates: list[tuple[int, str, str, str, dict[str, Any], str]] = []
    for kind_rank, (kind, text_source, mapping) in enumerate(sources):
        if not isinstance(mapping, dict):
            continue
        for language, entries in mapping.items():
            selected_format = _caption_format(entries)
            if selected_format is None:
                continue
            if language in requested:
                language_rank, rule = requested.index(language), "exact_requested_language"
            else:
                family_matches = [
                    index for index, value in enumerate(requested)
                    if _language_family(value) == _language_family(language)
                ]
                if family_matches:
                    language_rank, rule = 100 + family_matches[0], "language_family_fallback"
                elif requested:
                    continue
                else:
                    language_rank, rule = 1000, "first_available_language"
            candidates.append(
                (language_rank * 2 + kind_rank, language, kind, text_source, selected_format, rule)
            )
    if not candidates:
        return None
    _, language, kind, text_source, entry, rule = sorted(candidates, key=lambda row: (row[0], row[1]))[0]
    return CaptionSelection(
        language=language,
        kind=kind,
        text_source=text_source,
        ext=str(entry.get("ext") or "vtt"),
        name=str(entry.get("name") or ""),
        url=str(entry.get("url") or ""),
        source_language=str(entry.get("source_language") or ""),
        is_translated=(
            entry.get("is_translated") if isinstance(entry.get("is_translated"), bool) else None
        ),
        translation_kind=str(entry.get("translation_kind") or "unknown"),
        selected_by_rule=rule,
    )


def caption_availability_status(
    info: dict[str, Any],
    preferred_languages: Iterable[str],
    caption: CaptionSelection | None = None,
) -> str:
    """Classify an inspected caption inventory without mistaking errors for absence."""

    if caption is not None:
        return "available"
    requested = [value.replace("_", "-") for value in preferred_languages if value]
    has_any_track = False
    for mapping in (info.get("subtitles") or {}, info.get("automatic_captions") or {}):
        if not isinstance(mapping, dict):
            continue
        for entries in mapping.values():
            if _caption_format(entries) is not None:
                has_any_track = True
                break
        if has_any_track:
            break
    return "no_matching_language" if has_any_track and requested else "missing"


def output_paths(
    root: Path,
    profile: str,
    video_id: str,
    language: str,
    job_key: str | None = None,
) -> dict[str, Path]:
    if profile not in PROFILES:
        raise ValueError(f"unknown profile: {profile}")
    video_id = _safe_component(video_id, "video_id")
    language = _safe_component(language.replace("_", "-"), "language")
    section = "interviews" if profile == "youtube_interviews" else "screen_sources"
    root = Path(root).resolve()
    parts = [section, video_id, language]
    if job_key:
        parts.append(_safe_component(job_key, "job_key"))
    directory = root.joinpath(*parts).resolve()
    if root != directory and root not in directory.parents:
        raise ValueError("output path escaped its configured root")
    return {
        "directory": directory,
        "video": directory / "source.mp4",
        "audio": directory / "audio.wav",
        "metadata": directory / "metadata.json",
    }


def build_inspect_options() -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "skip_download": True,
        "extract_flat": False,
        "socket_timeout": 30,
        "retries": 3,
    }


def build_download_options(
    directory: Path, caption: CaptionSelection | None
) -> dict[str, Any]:
    """Build bounded single-video options without login, cookies or plugins."""

    options = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "outtmpl": str(Path(directory) / "source.%(ext)s"),
        "format": (
            "bestvideo[height<=720][vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
            "bestvideo[height<=720]+bestaudio/best[height<=720]"
        ),
        "merge_output_format": "mp4",
        "overwrites": False,
        "continuedl": True,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "max_filesize": 8 * 1024 * 1024 * 1024,
    }
    if caption is not None:
        options.update({
            "writesubtitles": caption.kind == "manual",
            "writeautomaticsub": caption.kind == "automatic",
            "subtitleslangs": [caption.language],
            "subtitlesformat": "vtt/best",
            "convertsubtitles": "vtt",
        })
    return options


def write_json_atomic(path: Path, value: Any) -> None:
    _write_atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def write_text_atomic(path: Path, value: str) -> None:
    _write_atomic(path, value)


def _write_atomic(path: Path, value: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("w", encoding="utf-8") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_parent_sidecar(
    item: dict[str, Any],
    info: dict[str, Any],
    caption: CaptionSelection | None,
    files: dict[str, Any],
    *,
    yt_dlp_version: str,
    caption_status: str | None = None,
) -> dict[str, Any]:
    """Build an allowlisted sidecar that never embeds extractor internals."""

    video_id = str(info.get("id") or item["video_id"])
    if caption and not caption_language_matches_content(
        str(item.get("content_language") or "und"), caption.language
    ):
        raise ValueError("selected caption language does not match the video content language")
    caption_data = {
        "status": (
            "downloaded" if caption else
            caption_status or caption_availability_status(
                info, item.get("languages") or [], caption
            )
        ),
        "required": bool(item.get("require_caption", True)),
        "kind": caption.kind if caption else None,
        "text_source": caption.text_source if caption else None,
        "requested_languages": list(item.get("languages") or []),
        "track_language": caption.language if caption else None,
        "source_language": caption.source_language if caption else None,
        "track_name": caption.name if caption else None,
        "track_format": "vtt" if caption else None,
        "is_translated": caption.is_translated if caption else None,
        "translation_kind": caption.translation_kind if caption else None,
        "selected_by_rule": caption.selected_by_rule if caption else None,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "asset_type": "youtube_parent",
        "profile": item["profile"],
        "job_key": item.get("job_key", ""),
        "source": "youtube",
        "source_id": video_id,
        "source_revision": item.get("source_revision", "current"),
        "canonical_url": f"https://www.youtube.com/watch?v={video_id}",
        "title": str(info.get("title") or ""),
        "description": redact_urls_in_text(info.get("description")),
        "duration_seconds": float(info.get("duration") or 0),
        "upload_date": info.get("upload_date"),
        "timestamp": info.get("timestamp"),
        "channel": str(info.get("channel") or info.get("uploader") or ""),
        "channel_id": str(info.get("channel_id") or info.get("uploader_id") or ""),
        "language": item.get("content_language", "und"),
        "requested_languages": list(item.get("languages") or []),
        "speaker_count": item.get("speaker_count"),
        "speaker_count_status": item.get("speaker_count_status", "needs_review"),
        "program": item.get("program", ""),
        "rights": item.get("rights") or {"status": "needs_review"},
        "rights_cleared": rights_cleared(item),
        "ai_generation": item.get("ai_generation") or {"status": "unknown", "evidence": []},
        "caption": caption_data,
        "files": files,
        "acquired_at": utc_now(),
        "toolchain": {
            "yt_dlp": yt_dlp_version,
            "grouping_algorithm": GROUPING_ALGORITHM_VERSION,
            "encoding_profile": ENCODING_PROFILE_VERSION,
        },
    }


def _import_yt_dlp():
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError(
            "未安装 yt-dlp；请先运行 bash scripts/bootstrap_conda.sh 更新 audiospider 环境"
        ) from exc
    return yt_dlp


def inspect_item(item: dict[str, Any]) -> tuple[dict[str, Any], CaptionSelection | None]:
    yt_dlp = _import_yt_dlp()
    with yt_dlp.YoutubeDL(build_inspect_options()) as ydl:
        info = ydl.extract_info(item["url"], download=False)
        info = ydl.sanitize_info(info)
    if not isinstance(info, dict) or str(info.get("id")) != item["video_id"]:
        raise RuntimeError("yt-dlp returned an unexpected video")
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
        raise ValueError("live and upcoming videos are not accepted")
    duration = float(info.get("duration") or 0)
    if item["profile"] == "youtube_interviews" and not PROFILES[item["profile"]].accepts_duration(duration):
        raise ValueError(f"interview duration {duration:.3f}s is outside 1530-3636s")
    maximum_parent_duration = float(item.get("max_parent_duration_seconds", 4 * 3600))
    if duration <= 0 or duration > maximum_parent_duration:
        raise ValueError(
            f"parent duration {duration:.3f}s is outside 0-{maximum_parent_duration:.3f}s"
        )
    caption = select_caption(info, item.get("languages") or [])
    if caption is None and item.get("require_caption", True):
        raise ValueError("no acceptable platform caption is available")
    if caption is not None and not caption_language_matches_content(
        item["content_language"], caption.language
    ):
        raise ValueError("selected caption language does not match the video content language")
    return info, caption


def _public_inspection(item: dict[str, Any], info: dict[str, Any], caption: CaptionSelection | None) -> dict[str, Any]:
    return {
        "status": "accepted",
        "inspected_at": utc_now(),
        "job_key": item["job_key"],
        "profile": item["profile"],
        "video_id": item["video_id"],
        "canonical_url": item["url"],
        "title": str(info.get("title") or ""),
        "duration_seconds": float(info.get("duration") or 0),
        "channel": str(info.get("channel") or info.get("uploader") or ""),
        "channel_id": str(info.get("channel_id") or info.get("uploader_id") or ""),
        "upload_date": info.get("upload_date"),
        "content_language": item.get("content_language", "und"),
        "caption": ({
            "status": "available",
            "required": bool(item.get("require_caption", True)),
            **{key: value for key, value in asdict(caption).items() if key != "url"},
        } if caption else {
            "status": caption_availability_status(
                info, item.get("languages") or [], caption
            ),
            "required": bool(item.get("require_caption", True)),
            "kind": None,
            "text_source": None,
            "track_language": None,
        }),
        "speaker_count": item.get("speaker_count"),
        "speaker_count_status": item["speaker_count_status"],
        "rights": item["rights"],
        "rights_cleared": rights_cleared(item),
    }


def _run(command: list[str], *, timeout: int = 7200) -> None:
    try:
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.decode("utf-8", "replace")[-2000:]
        raise RuntimeError(f"command failed: {command[0]}: {message}") from exc


def _find_video(directory: Path) -> Path:
    candidates = [
        path for path in directory.glob("source.*")
        if path.suffix.lower() in MEDIA_EXTENSIONS and path.is_file()
    ]
    if not candidates:
        raise FileNotFoundError("yt-dlp did not produce a video file")
    return sorted(candidates, key=lambda path: path.stat().st_size, reverse=True)[0]


def _find_caption(directory: Path, caption: CaptionSelection) -> Path:
    prefix = f"source.{caption.language}."
    candidates = [
        path for path in directory.glob("source.*")
        if path.name.startswith(prefix)
        and path.suffix.lower() in {".vtt", ".srt", ".ttml", ".srv1", ".srv2", ".srv3"}
    ]
    if not candidates:
        raise FileNotFoundError(f"yt-dlp did not produce the selected {caption.language} caption")
    return sorted(candidates)[0]


def _normalize_video(source: Path, target: Path) -> None:
    if source.suffix.lower() == ".mp4":
        os.replace(source, target)
        return
    temporary = target.with_name(f".{target.stem}.normalize-{uuid.uuid4().hex}.mp4")
    try:
        try:
            _run(["ffmpeg", "-nostdin", "-y", "-i", str(source), "-c", "copy", str(temporary)])
        except RuntimeError:
            _run([
                "ffmpeg", "-nostdin", "-y", "-i", str(source),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-movflags", "+faststart", str(temporary),
            ])
        _validate_media_probe("video", probe_media(temporary))
        os.replace(temporary, target)
        source.unlink(missing_ok=True)
    finally:
        temporary.unlink(missing_ok=True)


def _extract_wav(video: Path, target: Path) -> None:
    temporary = target.with_name(f".{target.stem}.extract-{uuid.uuid4().hex}.wav")
    try:
        _run([
            "ffmpeg", "-nostdin", "-y", "-i", str(video), "-vn",
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(temporary),
        ])
        _validate_media_probe("audio", probe_media(temporary))
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _caption_filename(caption: CaptionSelection) -> str:
    language = _safe_component(caption.language.replace("_", "-"), "caption language")
    return f"captions.{language}.{caption.kind}.vtt"


def _file_record(path: Path, relative_to: Path) -> dict[str, Any]:
    return {
        "path": str(path.relative_to(relative_to)),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


@contextmanager
def _exclusive_file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def download_item(
    item: dict[str, Any], output_root: Path, *, destination: Path | None = None
) -> Path:
    """Download one complete parent bundle and atomically promote its directory."""

    info, caption = inspect_item(item)
    lock = Path(output_root).resolve() / ".locks" / f"{item['job_key']}.lock"
    with _exclusive_file_lock(lock):
        return _download_item_locked(
            item, output_root, info, caption, destination=destination
        )


def _download_item_locked(
    item: dict[str, Any],
    output_root: Path,
    info: dict[str, Any],
    caption: CaptionSelection | None,
    *,
    destination: Path | None = None,
) -> Path:
    caption_status = caption_availability_status(
        info, item.get("languages") or [], caption
    )
    paths = output_paths(
        output_root,
        item["profile"],
        item["video_id"],
        caption.language if caption else item.get("content_language", "und"),
        item["job_key"],
    )
    configured_root = Path(output_root).resolve()
    if destination is None:
        destination = paths["directory"]
    else:
        destination = Path(destination).resolve()
        if configured_root != destination and configured_root not in destination.parents:
            raise ValueError("integrated destination escaped its configured root")
    destination_metadata = destination / "metadata.json"
    if destination_metadata.exists():
        completed = _validate_bundle(destination_metadata)
        if completed["metadata"].get("job_key") != item["job_key"]:
            raise ValueError("completed bundle has the wrong job_key")
        return destination

    staging_root = Path(output_root).resolve() / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    stage = staging_root / item["job_key"]
    stage.mkdir(parents=True, exist_ok=True)
    try:
        yt_dlp = _import_yt_dlp()
        with yt_dlp.YoutubeDL(build_download_options(stage, caption)) as ydl:
            downloaded_info = ydl.extract_info(item["url"], download=True)
            downloaded_info = ydl.sanitize_info(downloaded_info)
        confirmed_caption = select_caption(downloaded_info, item.get("languages") or [])
        if caption is not None:
            if confirmed_caption is None or (
                confirmed_caption.language, confirmed_caption.kind
            ) != (caption.language, caption.kind):
                raise ValueError("caption track changed between inspect and download")
            caption = confirmed_caption
        source_video = _find_video(stage)
        video = stage / "source.mp4"
        if source_video != video:
            _normalize_video(source_video, video)
        audio = stage / "audio.wav"
        _extract_wav(video, audio)

        files = {
            "video": _file_record(video, stage),
            "audio": _file_record(audio, stage),
        }
        cues: list[Cue] = []
        if caption is not None:
            source_caption = _find_caption(stage, caption)
            caption_path = stage / _caption_filename(caption)
            if source_caption != caption_path:
                os.replace(source_caption, caption_path)
            if caption_path.stat().st_size > MAX_CAPTION_BYTES:
                raise ValueError("downloaded caption exceeds the byte limit")
            cues = parse_vtt(
                caption_path.read_text(encoding="utf-8", errors="replace"),
                deduplicate_rolling=caption.kind == "automatic",
            )
            if not cues:
                raise ValueError("downloaded caption has no valid cues")
            transcript_path = caption_path.with_suffix(".txt")
            write_text_atomic(transcript_path, "\n".join(cue.text for cue in cues) + "\n")
            files.update({
                "caption_vtt": _file_record(caption_path, stage),
                "transcript_txt": _file_record(transcript_path, stage),
            })
        if sum(record["bytes"] for record in files.values()) > MAX_BUNDLE_BYTES:
            raise ValueError("downloaded bundle exceeds the total byte limit")
        sidecar = build_parent_sidecar(
            item, downloaded_info, caption, files,
            yt_dlp_version=getattr(yt_dlp.version, "__version__", "unknown"),
            caption_status=caption_status,
        )
        video_probe = probe_media(video)
        video_duration = _probe_duration(video_probe)
        sidecar["media"] = {
            "actual_duration_seconds": video_duration,
            "video_streams": sum(
                stream.get("codec_type") == "video" for stream in video_probe.get("streams") or []
            ),
            "audio_streams": sum(
                stream.get("codec_type") == "audio" for stream in video_probe.get("streams") or []
            ),
        }
        sidecar["caption"]["tail_overrun_seconds"] = (
            max(0.0, max(cue.end for cue in cues) - video_duration) if cues else None
        )
        (stage / "failure.json").unlink(missing_ok=True)
        write_json_atomic(stage / "metadata.json", sidecar)
        _validate_bundle(stage / "metadata.json")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError(f"destination exists without a completed sidecar: {destination}")
        os.replace(stage, destination)
        return destination
    except Exception as exc:
        write_json_atomic(
            stage / "failure.json",
            {
                "status": "failed",
                "job_key": item["job_key"],
                "failed_at": utc_now(),
                "error": redact_error(str(exc)),
            },
        )
        raise


def _clip_id(parent_hash: str, caption_hash: str, group: CueGroup) -> str:
    payload = "|".join(
        [
            parent_hash,
            caption_hash,
            str(round(group.start * 1000)),
            str(round(group.end * 1000)),
            GROUPING_ALGORITHM_VERSION,
            ENCODING_PROFILE_VERSION,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def create_clips(parent_directory: Path, output_root: Path, *, max_clips: int = 500) -> list[Path]:
    parent_directory = Path(parent_directory)
    if max_clips <= 0 or max_clips > 10_000:
        raise ValueError("max_clips must be between 1 and 10000")
    validated_parent = _validate_bundle(parent_directory / "metadata.json")
    metadata = validated_parent["metadata"]
    if not validated_parent["cues"]:
        raise ValueError("clips require a parent bundle with downloaded captions")
    if metadata.get("profile") != "youtube_screen_clips":
        raise ValueError("clips can only be generated from youtube_screen_clips parents")
    video = validated_parent["paths"]["video"]
    cues = validated_parent["cues"]
    media_duration = validated_parent["video_duration"]
    unclamped_cue_count = len(cues)
    clamped_cue_count = sum(cue.end > media_duration for cue in cues)
    cues = [
        Cue(cue.start, min(cue.end, media_duration), cue.text)
        for cue in cues
        if cue.start < media_duration and min(cue.end, media_duration) > cue.start
    ]
    profile = PROFILES["youtube_screen_clips"]
    groups = group_cues(
        cues,
        min_duration=profile.minimum_duration,
        max_duration=profile.maximum_duration,
        target_duration=profile.target_duration or 11.5,
        max_gap=5.0,
    )[:max_clips]
    parent_hash = metadata["files"]["video"]["sha256"]
    caption_hash = metadata["files"]["caption_vtt"]["sha256"]
    language = _safe_component(str(metadata.get("language") or "und").replace("_", "-"), "language")
    video_id = _safe_component(str(metadata["source_id"]), "video_id")
    created = []
    for group in groups:
        clip_id = _clip_id(parent_hash, caption_hash, group)
        directory = Path(output_root).resolve() / "screen_clips" / language / video_id / clip_id
        sidecar_path = directory / "metadata.json"
        if sidecar_path.exists():
            created.append(directory)
            continue
        stage_parent = Path(output_root).resolve() / ".staging"
        stage_parent.mkdir(parents=True, exist_ok=True)
        stage = stage_parent / f"clip-{clip_id}"
        stage.mkdir(parents=True, exist_ok=True)
        try:
            duration = group.duration
            clip_video = stage / "clip.mp4"
            _run([
                "ffmpeg", "-nostdin", "-y", "-ss", f"{group.start:.3f}", "-i", str(video),
                "-t", f"{duration:.3f}", "-map", "0:v:0", "-map", "0:a:0?",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-movflags", "+faststart", str(clip_video),
            ])
            audio = stage / "audio.wav"
            _extract_wav(clip_video, audio)
            clip_vtt = stage / "captions.vtt"
            write_text_atomic(clip_vtt, render_vtt(group.cues, origin=group.start))
            transcript = stage / "transcript.txt"
            write_text_atomic(transcript, group.text + "\n")
            files = {
                "video": _file_record(clip_video, stage),
                "audio": _file_record(audio, stage),
                "caption_vtt": _file_record(clip_vtt, stage),
                "transcript_txt": _file_record(transcript, stage),
            }
            sidecar = {
                "schema_version": SCHEMA_VERSION,
                "asset_type": "youtube_screen_clip",
                "profile": "youtube_screen_clips",
                "clip_id": clip_id,
                "parent_video_id": video_id,
                "parent_job_key": metadata.get("job_key"),
                "parent_media_sha256": parent_hash,
                "parent_caption_sha256": caption_hash,
                "start_ms": round(group.start * 1000),
                "end_ms": round(group.end * 1000),
                "duration_seconds": duration,
                "language": language,
                "caption": metadata["caption"],
                "speaker_count": None,
                "speaker_count_status": "needs_review",
                "rights": metadata.get("rights", {"status": "needs_review"}),
                "rights_cleared": metadata.get("rights_cleared", False),
                "ai_generation": metadata.get("ai_generation") or {
                    "status": "unknown", "evidence": [],
                },
                "cue_count": len(group.cues),
                "parent_caption_cue_count": unclamped_cue_count,
                "parent_caption_clamped_cue_count": clamped_cue_count,
                "files": files,
                "created_at": utc_now(),
                "toolchain": metadata.get("toolchain", {}),
            }
            write_json_atomic(stage / "metadata.json", sidecar)
            directory.parent.mkdir(parents=True, exist_ok=True)
            if directory.exists():
                raise FileExistsError(f"incomplete clip destination exists: {directory}")
            os.replace(stage, directory)
            created.append(directory)
        except Exception as exc:
            write_json_atomic(
                stage / "failure.json",
                {
                    "status": "failed", "clip_id": clip_id,
                    "failed_at": utc_now(), "error": redact_error(str(exc)),
                },
            )
            raise
    return created


def repair_parent_bundle(parent_directory: Path) -> dict[str, Any]:
    """Rebuild derived transcript/probe fields from an intact parent VTT and media."""

    directory = Path(parent_directory).resolve()
    sidecar_path = directory / "metadata.json"
    metadata = json.loads(sidecar_path.read_text(encoding="utf-8"))
    if metadata.get("schema_version") != SCHEMA_VERSION or metadata.get("asset_type") != "youtube_parent":
        raise ValueError("repair-parent requires a schema v1 youtube_parent bundle")
    if metadata.get("job_key") != directory.name:
        raise ValueError("parent directory does not match job_key")
    caption = _validate_caption_provenance(metadata.get("caption"), allow_missing=True)
    if (caption.get("status") or "downloaded") in {"missing", "no_matching_language"}:
        return _validate_bundle(sidecar_path)
    caption_language = str(caption.get("track_language") or caption.get("language") or "")
    if not caption_language_matches_content(str(metadata.get("language") or "und"), caption_language):
        raise ValueError("caption language does not match media content language")
    files = metadata.get("files")
    if not isinstance(files, dict) or set(files) != {
        "video", "audio", "caption_vtt", "transcript_txt"
    }:
        raise ValueError("parent sidecar has an invalid files closure")

    paths = {}
    for name, record in files.items():
        relative = Path(record.get("path", ""))
        path = (directory / relative).resolve()
        if relative.is_absolute() or ".." in relative.parts or directory not in path.parents:
            raise ValueError(f"{name} path escapes its bundle")
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"{name} is missing or linked")
        if name != "transcript_txt":
            if path.stat().st_size != int(record.get("bytes", -1)):
                raise ValueError(f"byte count mismatch before repair: {name}")
            if sha256_file(path) != record.get("sha256"):
                raise ValueError(f"hash mismatch before repair: {name}")
        paths[name] = path

    raw_vtt = paths["caption_vtt"].read_text(encoding="utf-8", errors="replace")
    cues = parse_vtt(raw_vtt, deduplicate_rolling=caption["kind"] == "automatic")
    if not cues:
        raise ValueError("caption has no valid cues")
    write_text_atomic(paths["transcript_txt"], "\n".join(cue.text for cue in cues) + "\n")
    metadata["files"]["transcript_txt"] = _file_record(paths["transcript_txt"], directory)
    video_probe = probe_media(paths["video"])
    audio_probe = probe_media(paths["audio"])
    _validate_media_probe("video", video_probe)
    _validate_media_probe("audio", audio_probe)
    video_duration = _probe_duration(video_probe)
    metadata["media"] = {
        "actual_duration_seconds": video_duration,
        "video_streams": sum(
            stream.get("codec_type") == "video" for stream in video_probe.get("streams") or []
        ),
        "audio_streams": sum(
            stream.get("codec_type") == "audio" for stream in video_probe.get("streams") or []
        ),
    }
    metadata["caption"]["tail_overrun_seconds"] = max(
        0.0, max(cue.end for cue in cues) - video_duration
    )
    if metadata["caption"].get("translation_kind") == "none":
        metadata["caption"]["source_language"] = ""
        metadata["caption"]["is_translated"] = None
        metadata["caption"]["translation_kind"] = "unknown"
    metadata.setdefault("ai_generation", {"status": "unknown", "evidence": []})
    metadata.setdefault("toolchain", {})["grouping_algorithm"] = GROUPING_ALGORITHM_VERSION
    metadata["repaired_at"] = utc_now()
    write_json_atomic(sidecar_path, metadata)
    return _validate_bundle(sidecar_path)


def repair_dataset_metadata(output_root: Path) -> dict[str, Any]:
    """Upgrade completed parent and clip sidecars without changing media or VTT files."""

    root = Path(output_root).resolve()
    parents: dict[str, dict[str, Any]] = {}
    repaired_parents = 0
    repaired_clips = 0
    failures = []
    for section in ("interviews", "screen_sources"):
        for sidecar_path in sorted((root / section).glob("**/metadata.json")):
            try:
                result = repair_parent_bundle(sidecar_path.parent)
                metadata = result["metadata"]
                parents[metadata["job_key"]] = metadata
                repaired_parents += 1
            except Exception as exc:
                failures.append({
                    "path": str(sidecar_path.relative_to(root)), "error": redact_error(str(exc)),
                })
    for sidecar_path in sorted((root / "screen_clips").glob("**/metadata.json")):
        try:
            metadata = json.loads(sidecar_path.read_text(encoding="utf-8"))
            parent = parents.get(str(metadata.get("parent_job_key") or ""))
            if parent is None:
                raise ValueError("cannot resolve clip parent during repair")
            metadata["caption"] = parent["caption"]
            metadata["language"] = parent["language"]
            metadata["rights"] = parent["rights"]
            metadata["rights_cleared"] = parent["rights_cleared"]
            metadata["ai_generation"] = parent["ai_generation"]
            metadata["parent_job_key"] = parent["job_key"]
            metadata.setdefault("toolchain", {})["grouping_algorithm"] = GROUPING_ALGORITHM_VERSION
            metadata["repaired_at"] = utc_now()
            write_json_atomic(sidecar_path, metadata)
            _validate_bundle(sidecar_path)
            repaired_clips += 1
        except Exception as exc:
            failures.append({
                "path": str(sidecar_path.relative_to(root)), "error": redact_error(str(exc)),
            })
    return {
        "repaired_parents": repaired_parents,
        "repaired_clips": repaired_clips,
        "failure_count": len(failures),
        "failures": failures,
    }


def probe_media(path: Path) -> dict[str, Any]:
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
    )
    return json.loads(completed.stdout.decode("utf-8"))


def _probe_duration(probe: dict[str, Any]) -> float:
    try:
        return float((probe.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0


def _validate_media_probe(name: str, probe: dict[str, Any]) -> None:
    streams = probe.get("streams") or []
    if name == "video":
        if not any(stream.get("codec_type") == "video" for stream in streams):
            raise ValueError("video asset has no video stream")
        if not any(stream.get("codec_type") == "audio" for stream in streams):
            raise ValueError("video asset has no audio stream")
        return
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if len(audio_streams) != 1:
        raise ValueError("WAV asset must contain exactly one audio stream")
    stream = audio_streams[0]
    if stream.get("codec_name") != "pcm_s16le":
        raise ValueError(f"WAV codec is not PCM16: {stream.get('codec_name')}")
    if int(stream.get("sample_rate") or 0) != 16_000:
        raise ValueError(f"WAV sample rate is not 16000: {stream.get('sample_rate')}")
    if int(stream.get("channels") or 0) != 1:
        raise ValueError(f"WAV channel count is not 1: {stream.get('channels')}")


def _validate_caption_provenance(
    caption: Any, *, allow_missing: bool = False
) -> dict[str, Any]:
    if not isinstance(caption, dict):
        raise ValueError("caption metadata is required")
    status = caption.get("status") or "downloaded"
    if status in {"missing", "no_matching_language"}:
        if not allow_missing:
            raise ValueError("this asset requires a downloaded caption")
        if caption.get("required") is not False:
            raise ValueError("captionless parent must declare required=false")
        if any(caption.get(key) is not None for key in (
            "kind", "text_source", "track_language", "source_language", "track_name",
        )):
            raise ValueError("captionless parent cannot declare a caption track")
        return caption
    if status != "downloaded":
        raise ValueError(f"unsupported caption status: {status!r}")
    expected = {"manual": "platform_manual", "automatic": "platform_auto"}
    kind = caption.get("kind")
    if kind not in expected or caption.get("text_source") != expected[kind]:
        raise ValueError("caption kind and text_source are inconsistent")
    if not isinstance(caption.get("track_language"), str) and not isinstance(
        caption.get("language"), str
    ):
        raise ValueError("caption track language is required")
    return caption


def _validate_bundle(sidecar_path: Path) -> dict[str, Any]:
    directory = sidecar_path.parent.resolve()
    metadata = json.loads(sidecar_path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict) or metadata.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported or missing sidecar schema_version")
    asset_type = metadata.get("asset_type")
    if asset_type not in {"youtube_parent", "youtube_screen_clip"}:
        raise ValueError("unsupported or missing asset_type")
    if metadata.get("profile") not in PROFILES:
        raise ValueError("unsupported or missing profile")
    if not isinstance(metadata.get("language"), str) or not metadata["language"]:
        raise ValueError("content language is required")
    caption = _validate_caption_provenance(
        metadata.get("caption"), allow_missing=asset_type == "youtube_parent"
    )
    caption_status = caption.get("status") or "downloaded"
    has_caption = caption_status == "downloaded"
    if has_caption:
        caption_language = str(caption.get("track_language") or caption.get("language") or "")
        if not caption_language_matches_content(metadata["language"], caption_language):
            raise ValueError("caption language does not match media content language")

    speaker_count = metadata.get("speaker_count")
    speaker_status = metadata.get("speaker_count_status")
    if speaker_count is None:
        if speaker_status != "needs_review":
            raise ValueError("unknown speaker_count must be needs_review")
    elif speaker_status != "verified_manual" or isinstance(speaker_count, bool) or not isinstance(
        speaker_count, int
    ):
        raise ValueError("speaker_count must be a manually verified integer")

    rights = metadata.get("rights")
    if not isinstance(rights, dict) or rights.get("status") not in RIGHTS_STATUSES:
        raise ValueError("valid rights metadata is required")
    if bool(metadata.get("rights_cleared")) != rights_cleared({"rights": rights}):
        raise ValueError("rights_cleared is inconsistent with rights evidence")
    ai_generation = metadata.get("ai_generation")
    if not isinstance(ai_generation, dict) or ai_generation.get("status") not in AI_GENERATION_STATUSES:
        raise ValueError("valid ai_generation provenance is required")
    if not isinstance(ai_generation.get("evidence", []), list):
        raise ValueError("ai_generation evidence must be an array")

    serialized = json.dumps(metadata, ensure_ascii=False).lower()
    sensitive_markers = [
        "token=", "signature=", "sig=", "expire=", "expires=", "policy=",
        "key-pair-id=", "x-goog-", "x-amz-", "authorization:", "authorization=",
        "cookie:", "cookie=",
    ]
    if any(marker in serialized for marker in sensitive_markers):
        raise ValueError("sidecar appears to contain credentials or a signed URL")

    required_files = {"video", "audio"}
    if has_caption:
        required_files.update({"caption_vtt", "transcript_txt"})
    files = metadata.get("files")
    if not isinstance(files, dict) or set(files) != required_files:
        raise ValueError("sidecar payload closure does not match its caption status")
    paths: dict[str, Path] = {}
    for name in sorted(required_files):
        record = files[name]
        if not isinstance(record, dict) or not all(
            key in record for key in ("path", "bytes", "sha256")
        ):
            raise ValueError(f"incomplete file record: {name}")
        relative = Path(record["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"{name} path escapes its bundle")
        path = (directory / relative).resolve()
        if directory not in path.parents or path.is_symlink() or not path.is_file():
            raise ValueError(f"missing, linked, or escaped {name} payload")
        if path.stat().st_size != int(record["bytes"]):
            raise ValueError(f"byte count mismatch: {path.name}")
        if sha256_file(path) != record["sha256"]:
            raise ValueError(f"hash mismatch: {path.name}")
        paths[name] = path
    actual_files = {path.name for path in directory.iterdir() if path.is_file()}
    expected_files = {sidecar_path.name} | {path.name for path in paths.values()}
    if actual_files != expected_files:
        raise ValueError("bundle contains missing or unexpected regular files")

    probes = {name: probe_media(paths[name]) for name in ("video", "audio")}
    for name, probe in probes.items():
        _validate_media_probe(name, probe)
    video_streams = [
        stream for stream in probes["video"].get("streams") or []
        if stream.get("codec_type") == "video"
    ]
    if any(int(stream.get("height") or 0) > 720 for stream in video_streams):
        raise ValueError("video height exceeds 720p")

    cues: list[Cue] = []
    if has_caption:
        raw_vtt = paths["caption_vtt"].read_text(encoding="utf-8", errors="replace")
        if not raw_vtt.lstrip("\ufeff").startswith("WEBVTT"):
            raise ValueError("caption is missing the WEBVTT header")
        cues = parse_vtt(
            raw_vtt,
            deduplicate_rolling=caption.get("kind") == "automatic",
        )
        if not cues:
            raise ValueError("caption has no valid cues")
        if any(current.start < previous.start for previous, current in zip(cues, cues[1:])):
            raise ValueError("caption cues are not monotonic")
        transcript = paths["transcript_txt"].read_text(
            encoding="utf-8", errors="replace"
        ).strip()
        if transcript != "\n".join(cue.text for cue in cues).strip():
            raise ValueError("transcript text does not match normalized VTT")

    video_duration = _probe_duration(probes["video"])
    audio_duration = _probe_duration(probes["audio"])
    if video_duration <= 0 or audio_duration <= 0 or abs(video_duration - audio_duration) > 0.35:
        raise ValueError("video and WAV durations are missing or inconsistent")
    if asset_type == "youtube_parent":
        if not VIDEO_ID_RE.fullmatch(str(metadata.get("source_id") or "")):
            raise ValueError("parent source_id is invalid")
        if metadata.get("job_key") != directory.name:
            raise ValueError("parent directory does not match job_key")
        declared = float(metadata.get("duration_seconds") or 0)
        if declared <= 0 or abs(declared - video_duration) > 2.0:
            raise ValueError("parent declared duration differs from media")
    else:
        if metadata.get("profile") != "youtube_screen_clips":
            raise ValueError("clip has the wrong profile")
        if metadata.get("clip_id") != directory.name:
            raise ValueError("clip directory does not match clip_id")
        duration = float(metadata.get("duration_seconds") or 0)
        derived = (int(metadata.get("end_ms")) - int(metadata.get("start_ms"))) / 1000
        if abs(duration - derived) > 0.001:
            raise ValueError("clip duration does not match start_ms/end_ms")
        if not PROFILES["youtube_screen_clips"].accepts_duration(video_duration):
            raise ValueError(f"actual clip video duration out of range: {video_duration}")
        if not PROFILES["youtube_screen_clips"].accepts_duration(audio_duration):
            raise ValueError(f"actual clip WAV duration out of range: {audio_duration}")
        if abs(video_duration - duration) > 0.35:
            raise ValueError("clip media duration differs from sidecar")
        if int(metadata.get("cue_count") or 0) != len(cues):
            raise ValueError("clip cue_count does not match VTT")
        if cues[0].start > 0.05 or max(cue.end for cue in cues) > video_duration + 0.10:
            raise ValueError("clip caption timing exceeds its media")
    return {
        "metadata": metadata,
        "paths": paths,
        "probes": probes,
        "cues": cues,
        "video_duration": video_duration,
        "audio_duration": audio_duration,
    }


def audit_dataset(output_root: Path) -> dict[str, Any]:
    root = Path(output_root).resolve()
    failures: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    counts: Counter[str] = Counter()
    clip_durations: list[float] = []
    sidecar_paths = sorted(
        path
        for section in ("interviews", "screen_sources", "screen_clips")
        for path in (root / section).glob("**/metadata.json")
    )
    valid: list[tuple[Path, dict[str, Any]]] = []
    if not sidecar_paths:
        failures.append({"path": ".", "error": "dataset contains no completed bundles"})
    for sidecar_path in sidecar_paths:
        try:
            result = _validate_bundle(sidecar_path)
            metadata = result["metadata"]
            caption = metadata["caption"]
            if metadata["asset_type"] == "youtube_parent" and result["cues"]:
                overrun = max(cue.end for cue in result["cues"]) - result["video_duration"]
                if overrun > 0.5:
                    if overrun > 5.0:
                        raise ValueError("platform caption tail exceeds video by more than 5s")
                    warnings.append({
                        "path": str(sidecar_path.relative_to(root)),
                        "warning": f"platform caption tail exceeds video by {overrun:.3f}s",
                    })
            valid.append((sidecar_path, result))
        except Exception as exc:
            failures.append({"path": str(sidecar_path.relative_to(root)), "error": str(exc)})

    parents = {
        (
            result["metadata"]["source_id"],
            result["metadata"]["files"]["video"]["sha256"],
            (result["metadata"]["files"].get("caption_vtt") or {}).get("sha256", ""),
        )
        for _, result in valid
        if result["metadata"]["asset_type"] == "youtube_parent"
    }
    invalid_closure_paths: set[Path] = set()
    for sidecar_path, result in valid:
        metadata = result["metadata"]
        if metadata["asset_type"] != "youtube_screen_clip":
            continue
        parent_key = (
            metadata.get("parent_video_id"),
            metadata.get("parent_media_sha256"),
            metadata.get("parent_caption_sha256"),
        )
        if parent_key not in parents:
            invalid_closure_paths.add(sidecar_path)
            failures.append({
                "path": str(sidecar_path.relative_to(root)),
                "error": "clip does not resolve to a valid parent bundle",
            })
    for sidecar_path, result in valid:
        if sidecar_path in invalid_closure_paths:
            continue
        metadata = result["metadata"]
        caption = metadata["caption"]
        counts[f"asset_type:{metadata['asset_type']}"] += 1
        counts[f"profile:{metadata['profile']}"] += 1
        counts[f"language:{metadata['language']}"] += 1
        caption_status = caption.get("status") or "downloaded"
        counts[f"caption_status:{caption_status}"] += 1
        if caption.get("kind"):
            counts[f"caption_kind:{caption['kind']}"] += 1
        counts[f"speaker_status:{metadata['speaker_count_status']}"] += 1
        counts[f"rights:{'cleared' if metadata.get('rights_cleared') else 'candidate'}"] += 1
        counts[f"ai_generation:{metadata['ai_generation']['status']}"] += 1
        if metadata["asset_type"] == "youtube_screen_clip":
            clip_durations.append(float(metadata["duration_seconds"]))
    if warnings:
        counts["warning:caption_tail_overrun"] = len(warnings)
    staging = root / ".staging"
    incomplete_staging = sorted(
        str(path.relative_to(root)) for path in staging.iterdir()
        if staging.is_dir() and path.is_dir()
    ) if staging.is_dir() else []
    for path in incomplete_staging:
        failures.append({"path": path, "error": "incomplete staging bundle"})
    return {
        "schema_version": 1,
        "audited_at": utc_now(),
        "root": str(root),
        "counts": dict(sorted(counts.items())),
        "clips": {
            "count": len(clip_durations),
            "average_duration_seconds": (
                sum(clip_durations) / len(clip_durations) if clip_durations else None
            ),
            "minimum_duration_seconds": min(clip_durations) if clip_durations else None,
            "maximum_duration_seconds": max(clip_durations) if clip_durations else None,
        },
        "failure_count": len(failures),
        "failures": failures,
        "warning_count": len(warnings),
        "warnings": warnings,
        "incomplete_staging_count": len(incomplete_staging),
        "incomplete_staging": incomplete_staging,
    }


def _load_manifest(path: Path) -> list[dict[str, Any]]:
    path = Path(path)
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("manifest exceeds the 10 MiB byte limit")
    return validate_manifest(json.loads(path.read_text(encoding="utf-8")))


def redact_error(value: str) -> str:
    """Remove signed query strings and control characters from persisted errors."""

    value = " ".join(str(value).replace("\x00", "").splitlines())
    return re.sub(r"https?://[^\s]+", lambda match: redact_url(match.group(0)), value)[:2000]


def _run_manifest(args: argparse.Namespace, *, download: bool) -> int:
    print(
        "提示：youtube_dataset.py 仅用于兼容、检查、修复和审计；"
        "新正式任务请使用 collect.py -> audiospider.db -> main.py。",
        file=sys.stderr,
    )
    items = _load_manifest(args.manifest)
    selected_ids = set(args.video_id or [])
    if selected_ids:
        unknown = selected_ids - {item["video_id"] for item in items}
        if unknown:
            raise ValueError(f"video ID not present in manifest: {', '.join(sorted(unknown))}")
        items = [item for item in items if item["video_id"] in selected_ids]
    records = []
    for item in items:
        try:
            if download:
                directory = download_item(item, args.output)
                records.append({"status": "downloaded", "job_key": item["job_key"], "path": str(directory)})
            else:
                info, caption = inspect_item(item)
                records.append(_public_inspection(item, info, caption))
        except Exception as exc:
            failure = {
                "status": "failed", "job_key": item.get("job_key"),
                "video_id": item.get("video_id"), "error": redact_error(str(exc)),
            }
            records.append(failure)
            print(json.dumps(failure, ensure_ascii=False), file=sys.stderr, flush=True)
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    manifest_dir = Path(args.output) / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    destination = manifest_dir / f"{'download' if download else 'inspect'}-{run_id}.jsonl"
    write_text_atomic(destination, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records))
    print(json.dumps({"manifest": str(destination), "records": records}, ensure_ascii=False, indent=2))
    return 1 if any(record["status"] == "failed" for record in records) else 0


def bounded_clip_count(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("max clips must be an integer") from exc
    if not 1 <= result <= 10_000:
        raise argparse.ArgumentTypeError("max clips must be between 1 and 10000")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("downloads/youtube"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ["inspect", "download"]:
        child = subparsers.add_parser(command)
        child.add_argument("--manifest", type=Path, required=True)
        child.add_argument(
            "--video-id", action="append",
            help="只处理清单中的指定 video ID；可重复使用",
        )
    clip = subparsers.add_parser("clip")
    clip.add_argument("--parent", type=Path, required=True)
    clip.add_argument("--max-clips", type=bounded_clip_count, default=500)
    clip.add_argument(
        "--allow-clips", action="store_true",
        help="明确确认本次需要生成短 clips；默认禁止",
    )
    repair = subparsers.add_parser("repair-parent")
    repair.add_argument("--parent", type=Path, required=True)
    subparsers.add_parser("repair-dataset")
    subparsers.add_parser("audit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "inspect":
        return _run_manifest(args, download=False)
    if args.command == "download":
        return _run_manifest(args, download=True)
    if args.command == "clip":
        if not args.allow_clips:
            raise ValueError("clip generation requires explicit --allow-clips authorization")
        created = create_clips(args.parent, args.output, max_clips=args.max_clips)
        print(json.dumps({"created": len(created), "paths": [str(path) for path in created]}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "repair-parent":
        result = repair_parent_bundle(args.parent)
        print(json.dumps({
            "status": "repaired",
            "path": str(Path(args.parent).resolve()),
            "video_id": result["metadata"]["source_id"],
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == "repair-dataset":
        result = repair_dataset_metadata(args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result["failure_count"] else 0
    report = audit_dataset(args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["failure_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
