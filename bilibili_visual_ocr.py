#!/usr/bin/env python3
"""Deterministic, local-only visual OCR helpers for Bilibili video bundles.

This module deliberately does not know about SQLite, Bilibili APIs, cookies or
the bundle commit protocol.  It turns sampled local video frames into a
strictly validated ``visual_ocr`` document plus deterministic VTT/TXT
derivatives.  A concrete OCR implementation is supplied through ``OcrEngine``;
the core and its tests do not require PaddleOCR or any other ML package.
"""

from __future__ import annotations

import math
import hashlib
import json
import os
import re
import struct
import subprocess
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Protocol, Sequence


VISUAL_OCR_SCHEMA_VERSION = 1
VISUAL_OCR_ASSET_TYPE = "visual_ocr_transcript"
VISUAL_OCR_TEXT_SOURCE = "visual_ocr"
VISUAL_OCR_ALGORITHM_VERSION = "visual-ocr-postprocess-v1"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")

_EXACT_SENSITIVE_ENV_NAMES = {
    "BILIBILI_COOKIE",
    "AUDIOSPIDER_BILIBILI_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "PODCAST_INDEX_KEY",
    "PODCAST_INDEX_SECRET",
}
_SENSITIVE_ENV_MARKERS = (
    "COOKIE",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
    "AUTHORIZATION",
    "PROXY",
    "API_KEY",
    "ACCESS_KEY",
)


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def _positive_int(value: Any, label: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{label} must be an integer in 1..{maximum}")
    return value


def _safe_label(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_LABEL_RE.fullmatch(value):
        raise ValueError(f"{label} is not a safe bounded identifier")
    return value


def _round_number(value: float) -> float:
    return round(float(value), 6)


def clean_ocr_text(value: Any, *, maximum: int = 500) -> str:
    """Normalize OCR text without inventing or translating its content."""

    if not isinstance(value, str):
        raise ValueError("OCR text must be a string")
    normalized = unicodedata.normalize("NFKC", value)
    lines = []
    for raw_line in normalized.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        cleaned = "".join(
            " " if char in "\t\f\v"
            else "" if unicodedata.category(char).startswith("C")
            else char
            for char in raw_line
        )
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if cleaned:
            lines.append(cleaned)
    result = "\n".join(lines)
    if not result:
        return ""
    if len(result) > maximum:
        raise ValueError("OCR text exceeds the configured bound")
    return result


def _validate_bbox(value: Any, label: str = "bbox_norm") -> tuple[float, float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError(f"{label} must contain four normalized coordinates")
    x1, y1, x2, y2 = (_finite_number(item, label) for item in value)
    if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
        raise ValueError(f"{label} coordinates are invalid")
    return (x1, y1, x2, y2)


@dataclass(frozen=True)
class VisualOcrConfig:
    language: str = "zh-Hans"
    profile: str = "bilibili-visual-ocr-zh-v1"
    sample_fps: float = 4.0
    region_normalized: tuple[float, float, float, float] = (0.0, 0.5, 1.0, 1.0)
    max_width: int = 1280
    min_confidence: float = 0.80
    min_consecutive_frames: int = 2
    max_missing_frames: int = 1
    fuzzy_text_threshold: float = 0.90
    bbox_iou_threshold: float = 0.10
    bbox_center_distance: float = 0.12
    static_text_min_seconds: float = 20.0
    static_text_min_coverage: float = 0.50
    max_frames: int = 100_000
    max_cues: int = 200_000
    max_frame_bytes: int = 32 * 1024 * 1024
    max_text_chars: int = 500

    def __post_init__(self) -> None:
        _safe_label(self.language, "language")
        _safe_label(self.profile, "profile")
        fps = _finite_number(self.sample_fps, "sample_fps")
        if not 0.1 <= fps <= 12.0:
            raise ValueError("sample_fps must be in [0.1, 12]")
        _validate_bbox(self.region_normalized, "region_normalized")
        _positive_int(self.max_width, "max_width", 3840)
        confidence = _finite_number(self.min_confidence, "min_confidence")
        if not 0 <= confidence <= 1:
            raise ValueError("min_confidence must be in [0, 1]")
        _positive_int(self.min_consecutive_frames, "min_consecutive_frames", 120)
        if type(self.max_missing_frames) is not int or not 0 <= self.max_missing_frames <= 120:
            raise ValueError("max_missing_frames must be an integer in 0..120")
        for name in ("fuzzy_text_threshold", "bbox_iou_threshold"):
            number = _finite_number(getattr(self, name), name)
            if not 0 <= number <= 1:
                raise ValueError(f"{name} must be in [0, 1]")
        center = _finite_number(self.bbox_center_distance, "bbox_center_distance")
        if not 0 <= center <= math.sqrt(2):
            raise ValueError("bbox_center_distance is invalid")
        static_seconds = _finite_number(self.static_text_min_seconds, "static_text_min_seconds")
        if static_seconds < 0:
            raise ValueError("static_text_min_seconds must be non-negative")
        coverage = _finite_number(self.static_text_min_coverage, "static_text_min_coverage")
        if not 0 <= coverage <= 1:
            raise ValueError("static_text_min_coverage must be in [0, 1]")
        _positive_int(self.max_frames, "max_frames", 2_000_000)
        _positive_int(self.max_cues, "max_cues", 1_000_000)
        _positive_int(self.max_frame_bytes, "max_frame_bytes", 256 * 1024 * 1024)
        _positive_int(self.max_text_chars, "max_text_chars", 10_000)


@dataclass(frozen=True)
class OcrDetection:
    text: str
    confidence: float
    bbox_norm: tuple[float, float, float, float]

    def __post_init__(self) -> None:
        clean_ocr_text(self.text)
        confidence = _finite_number(self.confidence, "confidence")
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be in [0, 1]")
        _validate_bbox(self.bbox_norm)


@dataclass(frozen=True)
class SampledFrame:
    index: int
    timestamp: float
    image_bytes: bytes
    image_format: str = "png"

    def __post_init__(self) -> None:
        if type(self.index) is not int or self.index < 0:
            raise ValueError("frame index must be a non-negative integer")
        timestamp = _finite_number(self.timestamp, "frame timestamp")
        if timestamp < 0:
            raise ValueError("frame timestamp must be non-negative")
        if not isinstance(self.image_bytes, bytes) or not self.image_bytes:
            raise ValueError("frame image must contain bytes")
        if self.image_format != "png":
            raise ValueError("only PNG sampled frames are supported")


@dataclass(frozen=True)
class FrameObservation:
    frame_index: int
    timestamp: float
    detections: tuple[OcrDetection, ...]

    def __post_init__(self) -> None:
        if type(self.frame_index) is not int or self.frame_index < 0:
            raise ValueError("observation frame index is invalid")
        timestamp = _finite_number(self.timestamp, "observation timestamp")
        if timestamp < 0:
            raise ValueError("observation timestamp must be non-negative")
        if not isinstance(self.detections, tuple) or any(
            not isinstance(item, OcrDetection) for item in self.detections
        ):
            raise ValueError("observation detections must be a tuple of OcrDetection")


@dataclass(frozen=True)
class VisualOcrCue:
    start: float
    end: float
    text: str
    confidence: float
    observations: int
    bbox_norm: tuple[float, float, float, float]


class OcrEngine(Protocol):
    @property
    def metadata(self) -> Mapping[str, str]: ...

    def recognize(self, frame: SampledFrame) -> Iterable[OcrDetection | Mapping[str, Any]]: ...


class CallableOcrEngine:
    """Small adapter useful for tests and for wrapping concrete OCR runtimes."""

    def __init__(
        self,
        recognizer: Callable[[SampledFrame], Iterable[OcrDetection | Mapping[str, Any]]],
        *,
        name: str,
        version: str,
        model: str,
        model_version: str,
    ) -> None:
        if not callable(recognizer):
            raise TypeError("recognizer must be callable")
        self._recognizer = recognizer
        self._metadata = _validated_engine_metadata({
            "name": name,
            "version": version,
            "model": model,
            "model_version": model_version,
        })

    @property
    def metadata(self) -> Mapping[str, str]:
        return dict(self._metadata)

    def recognize(self, frame: SampledFrame) -> Iterable[OcrDetection | Mapping[str, Any]]:
        return self._recognizer(frame)


def _is_sensitive_environment_name(name: str) -> bool:
    normalized = re.sub(r"[^A-Z0-9]+", "_", str(name).upper())
    return normalized in _EXACT_SENSITIVE_ENV_NAMES or any(
        marker in normalized for marker in _SENSITIVE_ENV_MARKERS
    )


def safe_subprocess_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Copy an environment while dropping credentials and every proxy channel."""

    source = os.environ if source is None else source
    return {
        str(name): str(value)
        for name, value in source.items()
        if not _is_sensitive_environment_name(str(name))
    }


def build_ffmpeg_command(video_path: Path, config: VisualOcrConfig) -> list[str]:
    """Build the no-shell FFmpeg PNG pipe command used by the frame iterator."""

    video = Path(video_path)
    x1, y1, x2, y2 = config.region_normalized
    crop = (
        f"crop=iw*{_round_number(x2 - x1)}:ih*{_round_number(y2 - y1)}:"
        f"iw*{_round_number(x1)}:ih*{_round_number(y1)}"
    )
    filters = (
        f"fps=fps={_round_number(config.sample_fps)}:start_time=0,{crop},"
        f"scale=w={config.max_width}:h=-2:force_original_aspect_ratio=decrease"
    )
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", str(video), "-map", "0:v:0", "-an", "-sn", "-dn",
        "-vf", filters, "-vsync", "0", "-threads", "1",
        "-f", "image2pipe", "-vcodec", "png", "pipe:1",
    ]


def _read_exact(stream: Any, count: int, *, allow_clean_eof: bool = False) -> bytes | None:
    chunks = []
    remaining = count
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            if allow_clean_eof and not chunks:
                return None
            raise ValueError("FFmpeg produced a truncated PNG frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_png(stream: Any, maximum: int) -> bytes | None:
    signature = _read_exact(stream, len(PNG_SIGNATURE), allow_clean_eof=True)
    if signature is None:
        return None
    if signature != PNG_SIGNATURE:
        raise ValueError("FFmpeg frame stream is not PNG")
    payload = bytearray(signature)
    first = True
    while True:
        header = _read_exact(stream, 8)
        assert header is not None
        length, chunk_type = struct.unpack(">I4s", header)
        if length > maximum or len(payload) + 12 + length > maximum:
            raise ValueError("FFmpeg PNG frame exceeds the byte limit")
        body = _read_exact(stream, length + 4)
        assert body is not None
        payload.extend(header)
        payload.extend(body)
        if first and chunk_type != b"IHDR":
            raise ValueError("FFmpeg PNG frame lacks an initial IHDR chunk")
        first = False
        if chunk_type == b"IEND":
            return bytes(payload)


def iter_ffmpeg_frames(
    video_path: Path,
    config: VisualOcrConfig,
    *,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    environment: Mapping[str, str] | None = None,
) -> Iterator[SampledFrame]:
    """Yield bounded PNG frames from a local video through an FFmpeg pipe."""

    video = Path(video_path)
    if not video.is_file() or video.is_symlink():
        raise ValueError("visual OCR input must be a regular local video file")
    process = popen_factory(
        build_ffmpeg_command(video, config),
        stdout=subprocess.PIPE,
        # stderr is intentionally discarded: the iterator cannot concurrently
        # drain an unbounded pipe while yielding frames, which could deadlock.
        stderr=subprocess.DEVNULL,
        env=safe_subprocess_environment(environment),
        bufsize=0,
    )
    if process.stdout is None:
        process.kill()
        raise RuntimeError("FFmpeg frame extractor has no stdout pipe")
    completed = False
    try:
        for index in range(config.max_frames + 1):
            payload = _read_png(process.stdout, config.max_frame_bytes)
            if payload is None:
                completed = True
                break
            if index >= config.max_frames:
                raise ValueError("FFmpeg frame count exceeds the configured bound")
            yield SampledFrame(
                index=index,
                timestamp=_round_number(index / config.sample_fps),
                image_bytes=payload,
            )
        if not completed:
            raise ValueError("FFmpeg frame stream did not terminate")
        return_code = process.wait()
        if return_code != 0:
            raise RuntimeError(
                f"FFmpeg frame extraction failed with exit status {return_code}"
            )
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.wait()
        raise
    finally:
        try:
            process.stdout.close()
        except Exception:
            pass
        if getattr(process, "stderr", None) is not None:
            try:
                process.stderr.close()
            except Exception:
                pass
        if process.poll() is None:
            process.kill()
            process.wait()


def _coerce_detection(value: OcrDetection | Mapping[str, Any], config: VisualOcrConfig) -> OcrDetection:
    if isinstance(value, OcrDetection):
        text = clean_ocr_text(value.text, maximum=config.max_text_chars)
        return OcrDetection(text, value.confidence, value.bbox_norm)
    if not isinstance(value, Mapping) or set(value) != {"text", "confidence", "bbox_norm"}:
        raise ValueError("OCR engine returned an invalid detection object")
    text = clean_ocr_text(value["text"], maximum=config.max_text_chars)
    return OcrDetection(
        text=text,
        confidence=_finite_number(value["confidence"], "confidence"),
        bbox_norm=_validate_bbox(value["bbox_norm"]),
    )


def _map_detection_to_source_frame(
    detection: OcrDetection, config: VisualOcrConfig,
) -> OcrDetection:
    """Map an OCR box from the cropped ROI back to the full source frame."""

    region_x1, region_y1, region_x2, region_y2 = config.region_normalized
    width = region_x2 - region_x1
    height = region_y2 - region_y1
    x1, y1, x2, y2 = detection.bbox_norm
    return OcrDetection(
        text=detection.text,
        confidence=detection.confidence,
        bbox_norm=tuple(_round_number(value) for value in (
            region_x1 + x1 * width,
            region_y1 + y1 * height,
            region_x1 + x2 * width,
            region_y1 + y2 * height,
        )),
    )


def observe_frames(
    frames: Iterable[SampledFrame], engine: OcrEngine, config: VisualOcrConfig,
) -> tuple[list[FrameObservation], dict[str, int]]:
    """Run an injected OCR engine and retain only bounded, valid detections."""

    observations = []
    frames_with_candidates = 0
    detections_accepted = 0
    detections_filtered = 0
    previous_timestamp = -1.0
    for count, frame in enumerate(frames, 1):
        if count > config.max_frames:
            raise ValueError("frame source exceeds the configured bound")
        if not isinstance(frame, SampledFrame):
            raise ValueError("frame source yielded a non-SampledFrame value")
        if frame.timestamp <= previous_timestamp:
            raise ValueError("sampled frame timestamps must be strictly increasing")
        previous_timestamp = frame.timestamp
        raw_detections = engine.recognize(frame)
        if isinstance(raw_detections, (str, bytes, Mapping)):
            raise ValueError("OCR engine detections must be an iterable of objects")
        accepted = []
        saw_candidate = False
        for raw in raw_detections:
            detection = _map_detection_to_source_frame(
                _coerce_detection(raw, config), config
            )
            if not detection.text:
                detections_filtered += 1
                continue
            saw_candidate = True
            if detection.confidence < config.min_confidence:
                detections_filtered += 1
                continue
            accepted.append(detection)
            detections_accepted += 1
        if saw_candidate:
            frames_with_candidates += 1
        accepted.sort(key=lambda item: (
            item.bbox_norm[1], item.bbox_norm[0], item.bbox_norm[3],
            item.bbox_norm[2], item.text, -item.confidence,
        ))
        observations.append(FrameObservation(frame.index, frame.timestamp, tuple(accepted)))
    return observations, {
        "frames_sampled": len(observations),
        "frames_with_candidates": frames_with_candidates,
        "detections_accepted": detections_accepted,
        "detections_filtered_low_confidence": detections_filtered,
    }


def _bbox_iou(first: Sequence[float], second: Sequence[float]) -> float:
    x1 = max(first[0], second[0])
    y1 = max(first[1], second[1])
    x2 = min(first[2], second[2])
    y2 = min(first[3], second[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    first_area = (first[2] - first[0]) * (first[3] - first[1])
    second_area = (second[2] - second[0]) * (second[3] - second[1])
    union = first_area + second_area - intersection
    return intersection / union if union > 0 else 0.0


def _bbox_center_distance(first: Sequence[float], second: Sequence[float]) -> float:
    first_center = ((first[0] + first[2]) / 2, (first[1] + first[3]) / 2)
    second_center = ((second[0] + second[2]) / 2, (second[1] + second[3]) / 2)
    return math.dist(first_center, second_center)


@dataclass
class _DetectionRun:
    first: float
    last: float
    bboxes: list[tuple[float, float, float, float]]
    confidences: list[float]
    variants: Counter[str] = field(default_factory=Counter)
    variant_confidence: defaultdict[str, float] = field(
        default_factory=lambda: defaultdict(float)
    )

    @classmethod
    def create(cls, timestamp: float, detection: OcrDetection) -> "_DetectionRun":
        run = cls(timestamp, timestamp, [detection.bbox_norm], [detection.confidence])
        run.add(timestamp, detection)
        # ``add`` appends one bbox/confidence; remove the constructor seed.
        run.bboxes.pop(0)
        run.confidences.pop(0)
        return run

    @property
    def text(self) -> str:
        return min(
            self.variants,
            key=lambda value: (
                -self.variants[value], -self.variant_confidence[value], value,
            ),
        )

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return tuple(
            sum(values) / len(self.bboxes) for values in zip(*self.bboxes)
        )  # type: ignore[return-value]

    def add(self, timestamp: float, detection: OcrDetection) -> None:
        self.last = timestamp
        self.bboxes.append(detection.bbox_norm)
        self.confidences.append(detection.confidence)
        self.variants[detection.text] += 1
        self.variant_confidence[detection.text] += detection.confidence


def _run_matches(run: _DetectionRun, detection: OcrDetection, config: VisualOcrConfig) -> tuple[bool, float]:
    similarity = SequenceMatcher(None, run.text, detection.text, autojunk=False).ratio()
    overlap = _bbox_iou(run.bbox, detection.bbox_norm)
    center = _bbox_center_distance(run.bbox, detection.bbox_norm)
    matches = similarity >= config.fuzzy_text_threshold and (
        overlap >= config.bbox_iou_threshold or center <= config.bbox_center_distance
    )
    return matches, similarity + overlap - center


def merge_frame_observations(
    observations: Iterable[FrameObservation],
    config: VisualOcrConfig,
    media_duration_seconds: float,
) -> tuple[list[VisualOcrCue], dict[str, Any]]:
    """Track OCR lines across frames and suppress long-lived static overlays."""

    duration = _finite_number(media_duration_seconds, "media_duration_seconds")
    if duration <= 0:
        raise ValueError("media_duration_seconds must be positive")
    ordered = list(observations)
    if len(ordered) > config.max_frames:
        raise ValueError("observation count exceeds the configured bound")
    for index, observation in enumerate(ordered):
        if not isinstance(observation, FrameObservation):
            raise ValueError("observations must contain FrameObservation values")
        if index and observation.timestamp <= ordered[index - 1].timestamp:
            raise ValueError("observation timestamps must be strictly increasing")
        if observation.timestamp > duration + 1 / config.sample_fps:
            raise ValueError("observation timestamp exceeds media duration")

    active: list[_DetectionRun] = []
    completed: list[_DetectionRun] = []
    maximum_gap = (config.max_missing_frames + 1.25) / config.sample_fps
    for observation in ordered:
        still_active = []
        for run in active:
            if observation.timestamp - run.last <= maximum_gap:
                still_active.append(run)
            else:
                completed.append(run)
        active = still_active

        candidates = []
        for run_index, run in enumerate(active):
            for detection_index, detection in enumerate(observation.detections):
                matches, score = _run_matches(run, detection, config)
                if matches:
                    candidates.append((-score, run_index, detection_index))
        matched_runs = set()
        matched_detections = set()
        for _negative_score, run_index, detection_index in sorted(candidates):
            if run_index in matched_runs or detection_index in matched_detections:
                continue
            active[run_index].add(
                observation.timestamp, observation.detections[detection_index]
            )
            matched_runs.add(run_index)
            matched_detections.add(detection_index)
        for detection_index, detection in enumerate(observation.detections):
            if detection_index not in matched_detections:
                active.append(_DetectionRun.create(observation.timestamp, detection))
    completed.extend(active)

    frame_interval = 1 / config.sample_fps
    cues = []
    static_filtered = 0
    for run in completed:
        if len(run.confidences) < config.min_consecutive_frames:
            continue
        end = min(duration, run.last + frame_interval)
        run_duration = max(0.0, end - run.first)
        coverage = run_duration / duration
        if (
            config.static_text_min_seconds > 0
            and run_duration >= config.static_text_min_seconds
            and coverage >= config.static_text_min_coverage
        ):
            static_filtered += 1
            continue
        if end <= run.first:
            continue
        cue = VisualOcrCue(
            start=_round_number(run.first),
            end=_round_number(end),
            text=run.text,
            confidence=_round_number(sum(run.confidences) / len(run.confidences)),
            observations=len(run.confidences),
            bbox_norm=tuple(_round_number(value) for value in run.bbox),
        )
        cues.append(cue)
    cues.sort(key=lambda cue: (
        cue.start, cue.end, cue.bbox_norm[1], cue.bbox_norm[0], cue.text,
    ))
    if len(cues) > config.max_cues:
        raise ValueError("visual OCR cue count exceeds the configured bound")
    mean_confidence = (
        _round_number(sum(cue.confidence for cue in cues) / len(cues))
        if cues else None
    )
    return cues, {
        "static_tracks_filtered": static_filtered,
        "cues_emitted": len(cues),
        "mean_confidence": mean_confidence,
    }


def _validated_engine_metadata(value: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {
        "name", "version", "model", "model_version",
    }:
        raise ValueError("OCR engine metadata has an invalid shape")
    return {
        key: _safe_label(value[key], f"engine.{key}")
        for key in ("name", "version", "model", "model_version")
    }


def build_visual_ocr_document(
    *,
    cues: Sequence[VisualOcrCue],
    config: VisualOcrConfig,
    engine_metadata: Mapping[str, Any],
    media_sha256: str,
    media_duration_seconds: float,
    quality: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the deterministic source JSON from cues and extraction evidence."""

    if not isinstance(media_sha256, str) or not SHA256_RE.fullmatch(media_sha256):
        raise ValueError("media_sha256 must be a lowercase SHA-256 digest")
    duration = _finite_number(media_duration_seconds, "media_duration_seconds")
    if duration <= 0:
        raise ValueError("media_duration_seconds must be positive")
    engine = _validated_engine_metadata(engine_metadata)
    required_quality = {
        "frames_sampled", "frames_with_candidates", "detections_accepted",
        "detections_filtered_low_confidence", "static_tracks_filtered",
        "cues_emitted", "mean_confidence",
    }
    if set(quality) != required_quality:
        raise ValueError("visual OCR quality summary has an invalid shape")
    cue_payload = [{
        "start": _round_number(cue.start),
        "end": _round_number(cue.end),
        "text": clean_ocr_text(cue.text, maximum=config.max_text_chars),
        "confidence": _round_number(cue.confidence),
        "observations": cue.observations,
        "bbox_norm": [_round_number(value) for value in cue.bbox_norm],
    } for cue in cues]
    document = {
        "schema_version": VISUAL_OCR_SCHEMA_VERSION,
        "asset_type": VISUAL_OCR_ASSET_TYPE,
        "status": "downloaded" if cue_payload else "no_stable_text_detected",
        "text_source": VISUAL_OCR_TEXT_SOURCE,
        "extraction_kind": "automatic",
        "caption_authorship": "unknown",
        "human_review_status": "unreviewed",
        "language": config.language,
        "language_basis": "content_language_policy",
        "profile": config.profile,
        "algorithm_version": VISUAL_OCR_ALGORITHM_VERSION,
        "bbox_basis": "source_video_frame_normalized",
        "source_media": {
            "file_key": "video",
            "sha256": media_sha256,
            "duration_seconds": _round_number(duration),
        },
        "engine": engine,
        "sampling": {
            "fps": _round_number(config.sample_fps),
            "timestamp_basis": "ffmpeg_fps_filter_output_index",
            "timestamp_uncertainty_seconds": _round_number(
                1.0 / config.sample_fps
            ),
            "region_normalized": [
                _round_number(value) for value in config.region_normalized
            ],
            "max_width": config.max_width,
            "min_confidence": _round_number(config.min_confidence),
            "min_consecutive_frames": config.min_consecutive_frames,
            "max_missing_frames": config.max_missing_frames,
            "fuzzy_text_threshold": _round_number(config.fuzzy_text_threshold),
            "bbox_iou_threshold": _round_number(config.bbox_iou_threshold),
            "bbox_center_distance": _round_number(config.bbox_center_distance),
            "static_text_min_seconds": _round_number(config.static_text_min_seconds),
            "static_text_min_coverage": _round_number(config.static_text_min_coverage),
            "max_frames": config.max_frames,
            "max_cues": config.max_cues,
            "max_frame_bytes": config.max_frame_bytes,
            "max_text_chars": config.max_text_chars,
        },
        "quality": dict(quality),
        "cue_count": len(cue_payload),
        "cues": cue_payload,
    }
    validate_visual_ocr_document(
        document,
        media_sha256=media_sha256,
        media_duration_seconds=duration,
        content_language=config.language,
    )
    return document


def _config_from_document(document: Mapping[str, Any]) -> VisualOcrConfig:
    sampling = document.get("sampling")
    if not isinstance(sampling, Mapping) or set(sampling) != {
        "fps", "timestamp_basis", "timestamp_uncertainty_seconds",
        "region_normalized", "max_width", "min_confidence",
        "min_consecutive_frames", "max_missing_frames", "fuzzy_text_threshold",
        "bbox_iou_threshold", "bbox_center_distance", "static_text_min_seconds",
        "static_text_min_coverage", "max_frames", "max_cues", "max_frame_bytes",
        "max_text_chars",
    }:
        raise ValueError("visual OCR sampling evidence has an invalid shape")
    if sampling["timestamp_basis"] != "ffmpeg_fps_filter_output_index":
        raise ValueError("visual OCR timestamp basis is invalid")
    expected_uncertainty = _round_number(
        1.0 / _finite_number(sampling["fps"], "sampling.fps")
    )
    if sampling["timestamp_uncertainty_seconds"] != expected_uncertainty:
        raise ValueError("visual OCR timestamp uncertainty is inconsistent")
    return VisualOcrConfig(
        language=document.get("language"),
        profile=document.get("profile"),
        sample_fps=sampling["fps"],
        region_normalized=_validate_bbox(
            sampling["region_normalized"], "sampling.region_normalized"
        ),
        max_width=sampling["max_width"],
        min_confidence=sampling["min_confidence"],
        min_consecutive_frames=sampling["min_consecutive_frames"],
        max_missing_frames=sampling["max_missing_frames"],
        fuzzy_text_threshold=sampling["fuzzy_text_threshold"],
        bbox_iou_threshold=sampling["bbox_iou_threshold"],
        bbox_center_distance=sampling["bbox_center_distance"],
        static_text_min_seconds=sampling["static_text_min_seconds"],
        static_text_min_coverage=sampling["static_text_min_coverage"],
        max_frames=sampling["max_frames"],
        max_cues=sampling["max_cues"],
        max_frame_bytes=sampling["max_frame_bytes"],
        max_text_chars=sampling["max_text_chars"],
    )


def _language_family(value: str) -> str:
    normalized = value.lower().replace("_", "-")
    family = normalized.removeprefix("ai-").split("-", 1)[0]
    return "zh" if family in {"zh", "cmn", "yue"} else family


def validate_visual_ocr_document(
    document: Mapping[str, Any],
    *,
    media_sha256: str,
    media_duration_seconds: float,
    content_language: str,
) -> None:
    """Strictly validate visual OCR provenance, evidence and cue bounds."""

    expected_keys = {
        "schema_version", "asset_type", "status", "text_source",
        "extraction_kind", "caption_authorship", "human_review_status",
        "language", "language_basis", "profile", "algorithm_version",
        "bbox_basis", "source_media", "engine", "sampling", "quality",
        "cue_count", "cues",
    }
    if not isinstance(document, Mapping) or set(document) != expected_keys:
        raise ValueError("visual OCR document has an invalid shape")
    fixed = {
        "schema_version": VISUAL_OCR_SCHEMA_VERSION,
        "asset_type": VISUAL_OCR_ASSET_TYPE,
        "text_source": VISUAL_OCR_TEXT_SOURCE,
        "extraction_kind": "automatic",
        "caption_authorship": "unknown",
        "human_review_status": "unreviewed",
        "language_basis": "content_language_policy",
        "algorithm_version": VISUAL_OCR_ALGORITHM_VERSION,
        "bbox_basis": "source_video_frame_normalized",
    }
    if any(document.get(key) != value for key, value in fixed.items()):
        raise ValueError("visual OCR provenance is invalid")
    config = _config_from_document(document)
    if (
        not isinstance(content_language, str)
        or _language_family(config.language) != _language_family(content_language)
        or _language_family(content_language) in {"", "und"}
    ):
        raise ValueError("visual OCR language does not match media content language")
    _validated_engine_metadata(document["engine"])
    duration = _finite_number(media_duration_seconds, "media_duration_seconds")
    if duration <= 0:
        raise ValueError("media_duration_seconds must be positive")
    if not isinstance(media_sha256, str) or not SHA256_RE.fullmatch(media_sha256):
        raise ValueError("expected media hash is invalid")
    source_media = document.get("source_media")
    if not isinstance(source_media, Mapping) or set(source_media) != {
        "file_key", "sha256", "duration_seconds",
    }:
        raise ValueError("visual OCR source_media evidence is invalid")
    if (
        source_media.get("file_key") != "video"
        or source_media.get("sha256") != media_sha256
        or _finite_number(source_media.get("duration_seconds"), "source duration")
        != _round_number(duration)
    ):
        raise ValueError("visual OCR source_media does not match the bundle video")

    cues = document.get("cues")
    cue_count = document.get("cue_count")
    if not isinstance(cues, list) or type(cue_count) is not int or cue_count != len(cues):
        raise ValueError("visual OCR cue count is invalid")
    if len(cues) > config.max_cues:
        raise ValueError("visual OCR cue count exceeds the configured bound")
    expected_status = "downloaded" if cues else "no_stable_text_detected"
    if document.get("status") != expected_status:
        raise ValueError("visual OCR status does not match its cues")
    previous_key: tuple[Any, ...] | None = None
    cue_confidences = []
    normalized_cues = []
    for index, cue in enumerate(cues):
        if not isinstance(cue, Mapping) or set(cue) != {
            "start", "end", "text", "confidence", "observations", "bbox_norm",
        }:
            raise ValueError(f"visual OCR cue {index} has an invalid shape")
        start = _finite_number(cue["start"], "cue start")
        end = _finite_number(cue["end"], "cue end")
        confidence = _finite_number(cue["confidence"], "cue confidence")
        text = clean_ocr_text(cue["text"], maximum=config.max_text_chars)
        bbox = _validate_bbox(cue["bbox_norm"], "cue bbox_norm")
        observations = cue["observations"]
        if (
            start < 0 or end <= start or end > duration + 1e-6 or not text
            or not config.min_confidence <= confidence <= 1
            or type(observations) is not int
            or observations < config.min_consecutive_frames
        ):
            raise ValueError(f"visual OCR cue {index} values are invalid")
        if any(
            cue[key] != _round_number(value)
            for key, value in (("start", start), ("end", end), ("confidence", confidence))
        ) or list(cue["bbox_norm"]) != [_round_number(value) for value in bbox]:
            raise ValueError(f"visual OCR cue {index} is not canonically rounded")
        key = (start, end, bbox[1], bbox[0], text)
        if previous_key is not None and key < previous_key:
            raise ValueError("visual OCR cues are not deterministically ordered")
        previous_key = key
        cue_confidences.append(confidence)
        normalized_cues.append({
            "start": start, "end": end, "text": text,
            "confidence": confidence, "observations": observations,
            "bbox_norm": bbox,
        })

    quality = document.get("quality")
    quality_keys = {
        "frames_sampled", "frames_with_candidates", "detections_accepted",
        "detections_filtered_low_confidence", "static_tracks_filtered",
        "cues_emitted", "mean_confidence",
    }
    if not isinstance(quality, Mapping) or set(quality) != quality_keys:
        raise ValueError("visual OCR quality summary has an invalid shape")
    integer_quality = {
        key: quality[key] for key in quality_keys - {"mean_confidence"}
    }
    if any(type(value) is not int or value < 0 for value in integer_quality.values()):
        raise ValueError("visual OCR quality counters are invalid")
    if (
        quality["frames_sampled"] > config.max_frames
        or quality["frames_with_candidates"] > quality["frames_sampled"]
        or quality["cues_emitted"] != len(cues)
    ):
        raise ValueError("visual OCR quality counters are inconsistent")
    expected_mean = (
        _round_number(sum(cue_confidences) / len(cue_confidences))
        if cue_confidences else None
    )
    if quality["mean_confidence"] != expected_mean:
        raise ValueError("visual OCR mean confidence is inconsistent")
    return None


def _format_vtt_timestamp(seconds: float) -> str:
    milliseconds = int(round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def render_visual_ocr_vtt(document: Mapping[str, Any]) -> str:
    """Render deterministic WebVTT from an already validated document shape."""

    cues = document.get("cues") if isinstance(document, Mapping) else None
    if not isinstance(cues, list):
        raise ValueError("visual OCR document has no cues array")
    config = _config_from_document(document)
    lines = ["WEBVTT", ""]
    for index, cue in enumerate(cues, 1):
        if not isinstance(cue, Mapping):
            raise ValueError("visual OCR cue has an invalid shape")
        start = _finite_number(cue.get("start"), "cue start")
        end = _finite_number(cue.get("end"), "cue end")
        text = clean_ocr_text(cue.get("text"), maximum=config.max_text_chars)
        if start < 0 or end <= start or not text:
            raise ValueError("visual OCR cue cannot be rendered")
        lines.extend([
            str(index),
            f"{_format_vtt_timestamp(start)} --> {_format_vtt_timestamp(end)}",
            text,
            "",
        ])
    return "\n".join(lines) + ("" if lines[-1] == "" else "\n")


def render_visual_ocr_text(document: Mapping[str, Any]) -> str:
    cues = document.get("cues") if isinstance(document, Mapping) else None
    if not isinstance(cues, list):
        raise ValueError("visual OCR document has no cues array")
    config = _config_from_document(document)
    texts = []
    for cue in cues:
        if not isinstance(cue, Mapping):
            raise ValueError("visual OCR cue has an invalid shape")
        text = clean_ocr_text(cue.get("text"), maximum=config.max_text_chars)
        if not text:
            raise ValueError("visual OCR cue cannot be rendered")
        texts.append(text)
    return "\n".join(texts) + ("\n" if texts else "")


def extract_visual_ocr(
    video_path: Path,
    engine: OcrEngine,
    config: VisualOcrConfig,
    *,
    media_sha256: str,
    media_duration_seconds: float,
    frame_source: Iterable[SampledFrame] | None = None,
) -> tuple[dict[str, Any], str | None, str | None]:
    """Extract and validate one deterministic local visual OCR artifact set."""

    frames = iter_ffmpeg_frames(video_path, config) if frame_source is None else frame_source
    observations, observation_quality = observe_frames(frames, engine, config)
    cues, merge_quality = merge_frame_observations(
        observations, config, media_duration_seconds
    )
    quality = {**observation_quality, **merge_quality}
    document = build_visual_ocr_document(
        cues=cues,
        config=config,
        engine_metadata=engine.metadata,
        media_sha256=media_sha256,
        media_duration_seconds=media_duration_seconds,
        quality=quality,
    )
    if not cues:
        return document, None, None
    return document, render_visual_ocr_vtt(document), render_visual_ocr_text(document)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probe_video_duration(video_path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "json", str(video_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=safe_subprocess_environment(),
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("ffprobe could not determine visual OCR media duration")
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
        duration = _finite_number(
            (payload.get("format") or {}).get("duration"), "media_duration_seconds"
        )
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, ValueError) as exc:
        raise RuntimeError("ffprobe returned an invalid visual OCR duration") from exc
    if duration <= 0:
        raise RuntimeError("ffprobe returned an invalid visual OCR duration")
    return duration


def prepare_visual_ocr_payloads(
    video_path: Path,
    config: VisualOcrConfig,
    *,
    engine: OcrEngine | None = None,
) -> dict[str, Any]:
    """Prepare in-memory deterministic payloads for a bundle commit wrapper.

    No model is imported or downloaded implicitly.  The caller must inject a
    configured engine, which keeps package/model policy outside this pure core.
    """

    video = Path(video_path)
    if engine is None:
        raise RuntimeError("a visual OCR engine must be supplied explicitly")
    if not video.is_file() or video.is_symlink():
        raise ValueError("visual OCR input must be a regular local video file")
    media_sha256 = _sha256_file(video)
    duration = _probe_video_duration(video)
    document, vtt, text = extract_visual_ocr(
        video,
        engine,
        config,
        media_sha256=media_sha256,
        media_duration_seconds=duration,
    )
    json_payload = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) + "\n"
    payloads: dict[str, str] = {"json": json_payload}
    if vtt is not None and text is not None:
        payloads.update({"vtt": vtt, "txt": text})
    return {"document": document, "payloads": payloads}
