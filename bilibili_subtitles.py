#!/usr/bin/env python3
"""Pure helpers for Bilibili subtitle provenance and JSON conversion."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from youtube_vtt import Cue, clean_caption_text, render_vtt


MAX_SUBTITLE_CUES = 200_000
PROVENANCE_RULE_VERSION = "bilibili-caption-provenance-v1"
AUTO_MARKERS = (
    "自动生成", "自动字幕", "ai生成", "ai 字幕", "auto-generated", "automatic",
)


def redact_url(value: str) -> str:
    """Remove credentials, query and fragment from a URL kept in sidecars."""

    parsed = urlsplit(value or "")
    if parsed.scheme != "https" or not parsed.hostname:
        return ""
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit(("https", parsed.hostname + port, parsed.path, "", ""))


def normalize_subtitle_url(value: str) -> str:
    value = str(value or "").strip()
    if value.startswith("//"):
        value = "https:" + value
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.username or parsed.password or not parsed.hostname:
        return ""
    if parsed.port not in {None, 443}:
        return ""
    host = parsed.hostname.lower().rstrip(".")
    if not (host == "hdslb.com" or host.endswith(".hdslb.com")):
        return ""
    return value


def _has_author(track: dict[str, Any]) -> bool:
    author = track.get("author")
    if not isinstance(author, dict):
        return False
    return bool(author.get("mid") or str(author.get("name") or "").strip())


def _auto_label(track: dict[str, Any]) -> bool:
    label = " ".join(str(track.get(key) or "") for key in ("lan", "lan_doc")).lower()
    return str(track.get("lan") or "").lower().startswith("ai-") or any(
        marker in label for marker in AUTO_MARKERS
    )


def classify_subtitle_track(track: dict[str, Any]) -> dict[str, str]:
    """Classify platform captions conservatively and return evidence rule.

    Bilibili's ``ai_type`` has represented translation in observed schemas, so it
    is preserved as raw provenance but is not used to prove caption generation.
    """

    if not isinstance(track, dict):
        raise TypeError("subtitle track must be an object")
    raw_type = track.get("type")
    track_type = raw_type if type(raw_type) is int else None
    auto_label = _auto_label(track)
    authored = _has_author(track)
    if track_type == 1:
        result = {
            "kind": "automatic",
            "text_source": "platform_auto",
            "selected_by_rule": "bilibili_type_ai",
        }
    elif track_type == 0 and auto_label:
        result = {
            "kind": "unknown",
            "text_source": "platform_unknown",
            "selected_by_rule": "bilibili_conflicting_evidence",
        }
    elif track_type == 0 and authored:
        result = {
            "kind": "manual",
            "text_source": "platform_manual",
            "selected_by_rule": "bilibili_cc_with_author",
        }
    elif raw_type is None and auto_label:
        result = {
            "kind": "automatic",
            "text_source": "platform_auto",
            "selected_by_rule": "bilibili_auto_label_fallback",
        }
    else:
        result = {
            "kind": "unknown",
            "text_source": "platform_unknown",
            "selected_by_rule": "bilibili_insufficient_evidence",
        }
    ai_type = track.get("ai_type")
    result["translation_kind"] = (
        "normal" if type(ai_type) is int and ai_type == 0 else
        "automatic_translation" if type(ai_type) is int and ai_type == 1 else
        "unknown"
    )
    result["rule_version"] = PROVENANCE_RULE_VERSION
    return result


def sanitize_subtitle_track(track: dict[str, Any]) -> dict[str, Any]:
    """Return stable track metadata while keeping the live URL only in memory."""

    classification = classify_subtitle_track(track)
    url = normalize_subtitle_url(str(track.get("subtitle_url") or track.get("url") or ""))
    author = track.get("author") if isinstance(track.get("author"), dict) else {}
    return {
        "id": track.get("id"),
        "id_str": str(track.get("id_str") or track.get("id") or ""),
        "url": url,
        "url_redacted": redact_url(url),
        "type": "application/json",
        "track_type": track.get("type"),
        "language": str(track.get("lan") or track.get("language") or ""),
        "label": str(track.get("lan_doc") or track.get("label") or ""),
        "label_brief": str(track.get("lan_doc_brief") or ""),
        "ai_type": track.get("ai_type"),
        "ai_status": track.get("ai_status"),
        "is_lock": track.get("is_lock"),
        "author": {
            "mid": author.get("mid"),
            "name": str(author.get("name") or ""),
        },
        **classification,
    }


def classify_subtitle_inventory(data: Any) -> dict[str, Any]:
    """Distinguish an empty public list from login-gated or malformed results."""

    if not isinstance(data, dict):
        return {"status": "unknown", "need_login_subtitle": None, "tracks": []}
    need_login = data.get("need_login_subtitle")
    subtitle = data.get("subtitle")
    if not isinstance(subtitle, dict) or not isinstance(subtitle.get("subtitles"), list):
        return {"status": "unknown", "need_login_subtitle": need_login, "tracks": []}
    tracks = subtitle["subtitles"]
    if tracks:
        status = "provided"
    elif need_login is True:
        status = "auth_required"
    elif need_login is False:
        status = "not_provided_publicly"
    else:
        status = "unknown"
    return {"status": status, "need_login_subtitle": need_login, "tracks": tracks}


def parse_subtitle_document(
    document: dict[str, Any], *, max_cues: int = MAX_SUBTITLE_CUES
) -> list[Cue]:
    """Strictly convert Bilibili ``body[]`` entries into normalized cues."""

    if not isinstance(document, dict) or not isinstance(document.get("body"), list):
        raise ValueError("subtitle JSON must contain a body array")
    body = document["body"]
    if not 1 <= len(body) <= max_cues:
        raise ValueError(f"subtitle cue count must be between 1 and {max_cues}")
    cues: list[Cue] = []
    previous_start = -1.0
    for index, entry in enumerate(body, 1):
        if not isinstance(entry, dict):
            raise ValueError(f"subtitle cue {index} must be an object")
        try:
            start = float(entry.get("from"))
            end = float(entry.get("to"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"subtitle cue {index} has invalid timing") from exc
        text = clean_caption_text(str(entry.get("content") or ""))
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise ValueError(f"subtitle cue {index} has invalid timing")
        if start < previous_start:
            raise ValueError(f"subtitle cue {index} is out of order")
        if not text:
            raise ValueError(f"subtitle cue {index} has empty text")
        cues.append(Cue(start=start, end=end, text=text))
        previous_start = start
    return cues


def render_subtitle_vtt(cues: list[Cue]) -> str:
    return render_vtt(cues)


def render_subtitle_text(cues: list[Cue]) -> str:
    return "\n".join(cue.text for cue in cues) + "\n"


def safe_caption_component(value: str) -> str:
    """Use a portable filename component for a language/provenance label."""

    result = "".join(char if char.isalnum() or char in "._-" else "-" for char in value)
    result = result.strip(".-")
    if not result or result in {".", ".."} or Path(result).name != result:
        return "und"
    return result[:80]
