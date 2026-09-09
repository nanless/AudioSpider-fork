#!/usr/bin/env python3
"""Small, dependency-free WebVTT parser and deterministic cue grouper."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass


TIMING_RE = re.compile(
    r"(?P<start>(?:\d{1,3}:)?\d{2}:\d{2}[.,]\d{3})\s*-->\s*"
    r"(?P<end>(?:\d{1,3}:)?\d{2}:\d{2}[.,]\d{3})"
)
TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"[ \t\f\v]+")


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class CueGroup:
    cues: tuple[Cue, ...]

    @property
    def start(self) -> float:
        return self.cues[0].start

    @property
    def end(self) -> float:
        return max(cue.end for cue in self.cues)

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def text(self) -> str:
        return "\n".join(cue.text for cue in self.cues if cue.text)


def parse_timestamp(value: str) -> float:
    """Parse WebVTT timestamps with either MM:SS.mmm or HH:MM:SS.mmm."""

    parts = value.strip().replace(",", ".").split(":")
    if len(parts) == 2:
        hours = 0
        minutes, seconds = parts
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        raise ValueError(f"invalid WebVTT timestamp: {value!r}")
    try:
        hours_value = int(hours)
        minutes_value = int(minutes)
        seconds_value = float(seconds)
    except ValueError as exc:
        raise ValueError(f"invalid WebVTT timestamp: {value!r}") from exc
    if minutes_value >= 60 or seconds_value >= 60:
        raise ValueError(f"out-of-range WebVTT timestamp: {value!r}")
    result = hours_value * 3600 + minutes_value * 60 + seconds_value
    if result < 0:
        raise ValueError(f"negative WebVTT timestamp: {value!r}")
    return result


def format_timestamp(seconds: float) -> str:
    """Format seconds as a WebVTT HH:MM:SS.mmm timestamp."""

    if seconds < 0:
        raise ValueError("timestamp cannot be negative")
    milliseconds = int(round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def clean_caption_text(value: str) -> str:
    """Remove WebVTT/HTML markup while keeping readable line boundaries."""

    lines = []
    for raw_line in value.replace("\r", "").split("\n"):
        line = html.unescape(TAG_RE.sub("", raw_line))
        line = WHITESPACE_RE.sub(" ", line).strip()
        if line and (not lines or line != lines[-1]):
            lines.append(line)
    return "\n".join(lines)


def _remove_rolling_overlap(previous: list[str], current: list[str]) -> list[str]:
    maximum = min(len(previous), len(current))
    for size in range(maximum, 0, -1):
        if previous[-size:] == current[:size]:
            return current[size:]
    return current


def parse_vtt(
    value: str,
    *,
    max_cues: int = 200_000,
    deduplicate_rolling: bool = False,
) -> list[Cue]:
    """Parse useful cues; rolling de-duplication is opt-in for automatic captions."""

    if not isinstance(value, str):
        raise TypeError("WebVTT input must be text")
    normalized = value.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n[ \t]*\n", normalized)
    cues: list[Cue] = []
    previous_lines: list[str] = []
    for block in blocks:
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines or lines[0].strip().upper() == "WEBVTT":
            continue
        if lines[0].lstrip().startswith(("NOTE", "STYLE", "REGION")):
            continue
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        match = TIMING_RE.search(lines[timing_index])
        if not match:
            continue
        start = parse_timestamp(match.group("start"))
        end = parse_timestamp(match.group("end"))
        if end <= start:
            continue
        text = clean_caption_text("\n".join(lines[timing_index + 1 :]))
        original_lines = text.splitlines()
        current_lines = (
            _remove_rolling_overlap(previous_lines, original_lines)
            if deduplicate_rolling else original_lines
        )
        if current_lines:
            text = "\n".join(current_lines)
            cues.append(Cue(start=start, end=end, text=text))
            if len(cues) > max_cues:
                raise ValueError(f"WebVTT exceeds the cue limit ({max_cues})")
        previous_lines = original_lines
    return cues


def group_cues(
    cues: list[Cue],
    *,
    min_duration: float,
    max_duration: float,
    target_duration: float,
    max_gap: float = 2.0,
) -> list[CueGroup]:
    """Group consecutive cues deterministically without violating hard bounds."""

    if not 0 < min_duration <= target_duration <= max_duration:
        raise ValueError("expected 0 < min_duration <= target_duration <= max_duration")
    if max_gap < 0:
        raise ValueError("max_gap cannot be negative")

    valid = sorted(
        (cue for cue in cues if cue.text and cue.end > cue.start and cue.duration <= max_duration),
        key=lambda cue: (cue.start, cue.end, cue.text),
    )
    result: list[CueGroup] = []
    current: list[Cue] = []

    def emit() -> None:
        nonlocal current
        if current:
            group = CueGroup(tuple(current))
            if min_duration <= group.duration <= max_duration:
                result.append(group)
        current = []

    for cue in valid:
        if not current:
            current = [cue]
            continue
        start = current[0].start
        previous_end = max(part.end for part in current)
        candidate_end = max(previous_end, cue.end)
        if cue.start - previous_end > max_gap or candidate_end - start > max_duration:
            emit()
            current = [cue]
            continue
        current.append(cue)
        if candidate_end - start >= target_duration:
            emit()
    emit()
    return result


def render_vtt(cues: list[Cue] | tuple[Cue, ...], *, origin: float = 0.0) -> str:
    """Render cues as valid WebVTT, shifting timestamps by ``origin``."""

    lines = ["WEBVTT", ""]
    for index, cue in enumerate(cues, 1):
        start = max(0.0, cue.start - origin)
        end = max(start, cue.end - origin)
        lines.extend(
            [str(index), f"{format_timestamp(start)} --> {format_timestamp(end)}", cue.text, ""]
        )
    return "\n".join(lines)
