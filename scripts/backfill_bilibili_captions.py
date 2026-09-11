#!/usr/bin/env python3
"""Safely refresh captions for completed Bilibili bundles.

Discovery is read-only by default.  ``--apply`` backs up SQLite before it
contacts Bilibili, then updates only caption payloads, the sidecar closure and
the matching completed database row.  Existing MP4/WAV payloads are never
opened for writing or downloaded again.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bilibili_dataset import (
    BilibiliClient,
    CURRENT_SIDECAR_SCHEMA_VERSION,
    DEFAULT_VISUAL_OCR_PROFILE,
    _file_record,
    caption_language_matches_content,
    default_caption_languages,
    job_lock,
    prepare_caption_payloads,
    resolve_caption_inventory,
    validate_bundle,
)
from bilibili_proxy import get_bilibili_proxy
from youtube_dataset import write_json_atomic


REFRESHABLE_STATUSES = {
    "auth_required",
    "not_provided_publicly",
    "no_matching_language",
    "invalid_track_inventory",
    "unknown",
}
DEFAULT_DB_PATH = REPO_ROOT / "audiospider.db"
DEFAULT_DOWNLOAD_DIR = REPO_ROOT / "downloads"


class CaptionBackfillFailure(RuntimeError):
    """Carry only a bounded, non-secret diagnostic across one repair phase."""

    def __init__(self, phase: str, cause: Exception):
        super().__init__(f"caption backfill failed during {phase}")
        self.phase = phase
        self.error_type = type(cause).__name__
        self.reason = _safe_failure_reason(cause)


def _safe_failure_reason(exc: Exception) -> str:
    """Map controlled failures to stable codes without serializing raw messages."""

    message = str(exc)
    exact = {
        "selected caption language does not match video content language":
            "caption_language_mismatch",
        "subtitle JSON must contain a body array": "subtitle_body_missing",
        "caption summary does not match its final inventory evidence":
            "inventory_summary_mismatch",
        "caption track is absent from final inventory evidence":
            "inventory_track_mismatch",
        "bundle files map has an unexpected closure": "bundle_file_closure_mismatch",
        "bundle contains unrecorded or missing regular files":
            "bundle_regular_file_closure_mismatch",
    }
    if message in exact:
        return exact[message]
    if message.startswith("caption track "):
        caption_failures = (
            (" has inconsistent provenance", "caption_provenance_mismatch"),
            (" language does not match media content", "caption_language_mismatch"),
            (" does not match classifier", "caption_classifier_mismatch"),
            (" has invalid file references", "caption_file_reference_invalid"),
            (" references a missing file", "caption_file_missing"),
            (" identity is incomplete", "caption_identity_incomplete"),
            (" cue count mismatch", "caption_cue_count_mismatch"),
            (" VTT is not derived from JSON", "caption_vtt_derivation_mismatch"),
            (" TXT is not derived from JSON", "caption_txt_derivation_mismatch"),
            (" extends too far beyond the media", "caption_exceeds_media_duration"),
        )
        for suffix, reason in caption_failures:
            if message.endswith(suffix):
                return reason
        return "caption_track_validation_failed"
    prefixes = (
        ("subtitle cue count must be", "subtitle_cue_count_invalid"),
        ("subtitle cue ", "subtitle_cue_invalid"),
        ("caption inventory ", "inventory_diagnostics_invalid"),
        ("bundle closure ", "bundle_closure_changed"),
        ("completed SQLite row ", "database_row_changed"),
        ("exact SQLite artifact update ", "database_compare_and_swap_failed"),
    )
    for prefix, reason in prefixes:
        if message.startswith(prefix):
            return reason
    if isinstance(exc, FileExistsError):
        return "caption_destination_exists"
    if isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError)):
        return "network_or_timeout"
    if isinstance(exc, (OSError, sqlite3.Error)):
        return "storage_or_database_error"
    return "validation_or_runtime_error"


def _phase_failure(phase: str, exc: Exception) -> CaptionBackfillFailure:
    return CaptionBackfillFailure(phase, exc)


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _bounded_float(value: str, minimum: float, maximum: float, label: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{label} must be a number") from exc
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise argparse.ArgumentTypeError(
            f"{label} must be in [{minimum}, {maximum}]"
        )
    return number


def visual_ocr_sample_fps(value: str) -> float:
    return _bounded_float(value, 0.1, 12.0, "OCR sample FPS")


def visual_ocr_confidence(value: str) -> float:
    return _bounded_float(value, 0.0, 1.0, "OCR minimum confidence")


def visual_ocr_region(value: str) -> tuple[float, float, float, float]:
    try:
        region = tuple(float(part.strip()) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "OCR region must be x1,y1,x2,y2"
        ) from exc
    if (
        len(region) != 4 or any(not math.isfinite(number) for number in region)
        or not (0 <= region[0] < region[2] <= 1)
        or not (0 <= region[1] < region[3] <= 1)
    ):
        raise argparse.ArgumentTypeError(
            "OCR region must be normalized x1,y1,x2,y2 coordinates"
        )
    return region[0], region[1], region[2], region[3]


def visual_ocr_profile(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", value):
        raise argparse.ArgumentTypeError("OCR profile is invalid")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="B站已完成 bundle 的字幕-only 回填（默认只预览）"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--downloads", type=Path, default=DEFAULT_DOWNLOAD_DIR)
    parser.add_argument("--limit", type=positive_int, default=100)
    parser.add_argument(
        "--job-key", action="append", default=[],
        help=(
            "只处理完全匹配的 job_key；可重复传入。"
            "适合先对一个已审核样本做 OCR 金丝雀测试"
        ),
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="显式执行回填；省略时不发起网络请求且不写文件/数据库",
    )
    parser.add_argument(
        "--allow-bilibili-cookie", action="store_true",
        help="仅在已明确授权时从当前进程 BILIBILI_COOKIE 读取登录态",
    )
    parser.add_argument(
        "--retry-invalid-timeline", action="store_true",
        help="显式重查已标记 invalid_timeline 的平台轨道",
    )
    parser.add_argument(
        "--visual-ocr", action="store_true",
        help=(
            "平台无可用字幕时启用画面 OCR；"
            "仍需 --apply 才会写入"
        ),
    )
    parser.add_argument(
        "--refresh-visual-ocr", action="store_true",
        help=(
            "显式重做已有视觉 OCR；默认会跳过 status=downloaded/"
            "no_stable_text_detected 的完整 OCR sidecar"
        ),
    )
    parser.add_argument(
        "--ocr-engine", choices=("paddle",), default="paddle",
        help="--visual-ocr 的 OCR 后端（当前仅支持 paddle）",
    )
    parser.add_argument(
        "--ocr-sample-fps", type=visual_ocr_sample_fps, default=4.0,
    )
    parser.add_argument(
        "--ocr-region", type=visual_ocr_region,
        default=(0.0, 0.5, 1.0, 1.0),
        help="归一化区域 x1,y1,x2,y2（默认底部 50%%）",
    )
    parser.add_argument(
        "--ocr-min-confidence", type=visual_ocr_confidence, default=0.80,
    )
    parser.add_argument(
        "--ocr-profile", type=visual_ocr_profile,
        default=DEFAULT_VISUAL_OCR_PROFILE,
    )
    parser.add_argument(
        "--ocr-timeout-seconds", type=positive_int, default=14_400,
        help="单个 bundle OCR 的可终止硬超时（默认 14400 秒）",
    )
    return parser


def _read_connection(db_path: Path) -> sqlite3.Connection:
    db_path = db_path.resolve()
    if not db_path.is_file():
        raise FileNotFoundError(f"SQLite database does not exist: {db_path}")
    connection = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _is_below(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def bundle_fingerprint(bundle: Path) -> tuple[int, str]:
    """Compute the same stable file-closure fingerprint as the queue worker."""

    total = 0
    digest = hashlib.sha256()
    paths = sorted(
        path for path in Path(bundle).rglob("*")
        if path.is_file() and path.name != "failure.json"
    )
    if not paths:
        raise ValueError("video bundle has no files")
    for path in paths:
        relative = path.relative_to(bundle).as_posix()
        file_digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                file_digest.update(chunk)
        size = path.stat().st_size
        total += size
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(file_digest.hexdigest().encode("ascii"))
        digest.update(b"\n")
    return total, digest.hexdigest()


def discover_candidates(
    db_path: Path, download_root: Path, limit: int,
    *, include_invalid_timeline: bool = False,
    skip_existing_visual_ocr: bool = False,
    job_keys: set[str] | None = None,
    audit: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Return exact completed Bilibili bundle rows whose captions can refresh."""

    root = (Path(download_root).resolve() / "bilibili").resolve()
    connection = _read_connection(Path(db_path))
    try:
        rows = connection.execute(
            "SELECT id, source, artifact_kind, status, source_id, job_key, "
            "bundle_path, local_path, file_size, content_hash FROM audio_urls "
            "WHERE source='bilibili' AND artifact_kind='video_bundle' "
            "AND status='done' ORDER BY id"
        ).fetchall()
    finally:
        connection.close()
    counters = {
        "done_rows_scanned": len(rows),
        "requested_rows_matched": 0,
        "already_captioned": 0,
        "already_visual_ocr": 0,
        "skipped_path_or_sidecar": 0,
        "skipped_invalid_bundle": 0,
        "skipped_database_identity": 0,
        "eligible": 0,
    }
    candidates = []
    for row in rows:
        if job_keys is not None and str(row["job_key"]) not in job_keys:
            continue
        counters["requested_rows_matched"] += 1
        bundle = Path(str(row["bundle_path"] or "")).resolve()
        sidecar = bundle / "metadata.json"
        if not _is_below(bundle, root) or not sidecar.is_file():
            counters["skipped_path_or_sidecar"] += 1
            continue
        try:
            result = validate_bundle(sidecar)
        except (OSError, ValueError, json.JSONDecodeError):
            counters["skipped_invalid_bundle"] += 1
            continue
        metadata = result["metadata"]
        caption_status = str((metadata.get("caption") or {}).get("status") or "")
        refreshable = REFRESHABLE_STATUSES | (
            {"invalid_timeline"} if include_invalid_timeline else set()
        )
        if caption_status not in refreshable:
            counters["already_captioned"] += 1
            continue
        visual_ocr_status = str(
            (((metadata.get("derived_text") or {}).get("visual_ocr") or {}).get(
                "status"
            )) or ""
        )
        if (
            skip_existing_visual_ocr
            and visual_ocr_status in {"downloaded", "no_stable_text_detected"}
        ):
            counters["already_visual_ocr"] += 1
            continue
        old_file_size = row["file_size"]
        old_content_hash = row["content_hash"]
        if (
            bundle.name != row["job_key"]
            or bundle.parent.name != row["source_id"]
            or metadata.get("job_key") != row["job_key"]
            or metadata.get("source_id") != row["source_id"]
            or Path(str(row["local_path"] or "")).resolve() != bundle / "source.mp4"
            or type(old_file_size) is not int or old_file_size <= 0
            or not isinstance(old_content_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", old_content_hash)
        ):
            counters["skipped_database_identity"] += 1
            continue
        candidates.append({
            "id": int(row["id"]),
            "source_id": str(row["source_id"]),
            "job_key": str(row["job_key"]),
            "bundle": bundle,
            "_bundle_path_db": str(row["bundle_path"]),
            "_local_path_db": str(row["local_path"]),
            "_old_file_size": old_file_size,
            "_old_content_hash": old_content_hash,
            "caption_status": caption_status,
        })
        counters["eligible"] += 1
        if len(candidates) >= limit:
            break
    if audit is not None:
        audit.clear()
        audit.update(counters)
    return candidates


def backup_database(db_path: Path) -> Path:
    """Create a consistent SQLite backup before the first apply mutation."""

    db_path = Path(db_path).resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = db_path.with_name(f"{db_path.name}.caption-backfill-{stamp}.bak")
    suffix = 1
    while backup.exists():
        backup = db_path.with_name(
            f"{db_path.name}.caption-backfill-{stamp}-{suffix}.bak"
        )
        suffix += 1
    source = sqlite3.connect(str(db_path), timeout=60)
    destination = sqlite3.connect(str(backup))
    complete = False
    try:
        source.backup(destination)
        destination.commit()
        complete = True
    finally:
        destination.close()
        source.close()
        if not complete:
            backup.unlink(missing_ok=True)
    return backup


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path = Path(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(Path(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _journal_directory(bundle: Path, job_key: str) -> Path:
    return bundle.parents[2] / ".caption-backfill-journal" / job_key


def _write_repair_journal(
    directory: Path, candidate: dict[str, Any], sidecar: Path,
    old_assets: list[tuple[str, Path]], new_assets: list[Path],
) -> dict[str, Any]:
    """Persist enough exact evidence to recover an interrupted cross-domain commit."""

    if directory.exists():
        raise FileExistsError("an unfinished caption repair journal already exists")
    old_dir = directory / "old"
    old_dir.mkdir(parents=True)
    original_sidecar = sidecar.read_bytes()
    _atomic_write_bytes(directory / "original.metadata.json", original_sidecar)
    old_records = []
    for index, (relative, source) in enumerate(old_assets):
        backup = old_dir / f"{index:04d}.bin"
        shutil.copyfile(source, backup)
        with backup.open("rb") as stream:
            os.fsync(stream.fileno())
        old_records.append({
            "path": relative,
            "backup": f"old/{backup.name}",
            "sha256": _sha256_path(source),
        })
    new_records = [{
        "path": source.name,
        "sha256": _sha256_path(source),
    } for source in new_assets]
    if len({row["path"] for row in new_records}) != len(new_records):
        raise ValueError("caption repair has duplicate destination names")
    journal = {
        "schema_version": 1,
        "state": "prepared",
        "row_id": candidate["id"],
        "source_id": candidate["source_id"],
        "job_key": candidate["job_key"],
        "bundle_path": str(candidate["bundle"]),
        "database_pre": {
            "file_size": candidate["_old_file_size"],
            "content_hash": candidate["_old_content_hash"],
        },
        "original_sidecar_sha256": hashlib.sha256(original_sidecar).hexdigest(),
        "old_assets": old_records,
        "new_assets": new_records,
    }
    _atomic_write_bytes(
        directory / "journal.json",
        (json.dumps(journal, sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )
    _fsync_directory(old_dir)
    _fsync_directory(directory)
    _fsync_directory(directory.parent)
    return journal


def _job_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    caption = metadata.get("caption") or {}
    languages = caption.get("requested_languages")
    if not isinstance(languages, list) or not languages or not all(
        isinstance(value, str) and value for value in languages
    ):
        languages = default_caption_languages(str(metadata.get("content_language") or ""))
    return {
        "bvid": metadata["bvid"],
        "cid": metadata["cid"],
        "languages": languages,
    }


async def _prepare_captions(
    client: BilibiliClient, tracks: list[dict[str, Any]], stage: Path,
    media_duration_seconds: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return await prepare_caption_payloads(
        client, tracks, stage, media_duration_seconds
    )


def _prepare_visual_ocr(
    video_path: Path, *, config: Any, engine: Any = None,
    timeout_seconds: int = 14_400,
) -> dict[str, Any]:
    """Run the optional core only after apply and fallback policy allow it."""

    if engine is None:
        from bilibili_visual_ocr_worker import prepare_visual_ocr_payloads_bounded
        result = prepare_visual_ocr_payloads_bounded(
            Path(video_path), config, timeout_seconds=timeout_seconds
        )
    else:
        from bilibili_visual_ocr import prepare_visual_ocr_payloads
        result = prepare_visual_ocr_payloads(
            Path(video_path), config, engine=engine
        )
    if not isinstance(result, dict):
        raise ValueError("visual OCR result must be an object")
    return result


def _build_visual_ocr_config(args: argparse.Namespace) -> Any:
    from bilibili_visual_ocr import VisualOcrConfig

    return VisualOcrConfig(
        profile=args.ocr_profile,
        sample_fps=args.ocr_sample_fps,
        region_normalized=args.ocr_region,
        min_confidence=args.ocr_min_confidence,
    )


def _visual_ocr_config_for_language(config: Any, language: str) -> Any:
    from bilibili_visual_ocr import VisualOcrConfig

    if isinstance(config, VisualOcrConfig):
        return replace(config, language=language)
    return config


def _create_visual_ocr_engine(name: str) -> Any:
    if name != "paddle":
        raise ValueError("unsupported visual OCR engine")
    from bilibili_visual_ocr_paddle import create_paddle_engine

    return create_paddle_engine(device="gpu:0")


def _stage_visual_ocr_result(
    result: dict[str, Any], stage: Path,
) -> dict[str, Any]:
    """Materialize deterministic OCR payloads in the repair staging area."""

    document = result.get("document")
    payloads = result.get("payloads")
    if not isinstance(document, dict) or not isinstance(payloads, dict):
        raise ValueError("visual OCR result must contain document and payloads")
    status = document.get("status")
    if status not in {"downloaded", "no_stable_text_detected"}:
        raise ValueError("visual OCR result has an unsupported status")
    normalized = dict(payloads)
    normalized["json"] = (
        json.dumps(
            document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ) + "\n"
    )
    expected = {"json", "vtt", "txt"} if status == "downloaded" else {"json"}
    if set(normalized) != expected:
        raise ValueError("visual OCR payload closure is inconsistent")
    paths: dict[str, Path] = {}
    for extension in sorted(normalized):
        payload = normalized[extension]
        if isinstance(payload, str):
            encoded = payload.encode("utf-8")
        elif isinstance(payload, bytes):
            encoded = payload
        else:
            raise ValueError("visual OCR payload must be text or bytes")
        path = Path(stage) / f"visual_ocr.{extension}"
        _atomic_write_bytes(path, encoded)
        paths[extension] = path
    return {"document": copy.deepcopy(document), "paths": paths}


def _visual_ocr_sidecar(
    document: dict[str, Any], keys: dict[str, str],
) -> dict[str, Any]:
    summary = {
        key: copy.deepcopy(value)
        for key, value in document.items()
        if key not in {"cues", "frames", "observations"}
    }
    summary["files"] = dict(keys)
    summary["derivation_trigger"] = "posthoc_backfill"
    return summary


def _verify_exact_row(
    connection: sqlite3.Connection, candidate: dict[str, Any], bundle: Path,
) -> None:
    row = connection.execute(
        "SELECT source, artifact_kind, status, source_id, job_key, bundle_path, "
        "local_path, file_size, content_hash FROM audio_urls WHERE id=?",
        (candidate["id"],),
    ).fetchone()
    if row is None or (
        row["source"] != "bilibili"
        or row["artifact_kind"] != "video_bundle"
        or row["status"] != "done"
        or row["source_id"] != candidate["source_id"]
        or row["job_key"] != candidate["job_key"]
        or row["bundle_path"] != candidate["_bundle_path_db"]
        or row["local_path"] != candidate["_local_path_db"]
        or Path(str(row["bundle_path"] or "")).resolve() != bundle
        or Path(str(row["local_path"] or "")).resolve() != bundle / "source.mp4"
        or row["file_size"] != candidate["_old_file_size"]
        or row["content_hash"] != candidate["_old_content_hash"]
    ):
        raise RuntimeError("completed SQLite row changed since dry-run discovery")


def _verify_pre_mutation(
    db_path: Path, candidate: dict[str, Any],
) -> dict[str, Any]:
    """Verify media/sidecar closure and its saved DB fingerprint before apply."""

    bundle = candidate["bundle"]
    result = validate_bundle(bundle / "metadata.json")
    observed = bundle_fingerprint(bundle)
    expected = (
        candidate["_old_file_size"], candidate["_old_content_hash"],
    )
    if observed != expected:
        raise RuntimeError("bundle closure no longer matches the discovered SQLite row")
    connection = _read_connection(Path(db_path))
    try:
        _verify_exact_row(connection, candidate, bundle)
    finally:
        connection.close()
    return result


def _recover_one_journal(
    db_path: Path, download_root: Path, directory: Path,
) -> dict[str, Any]:
    """Resolve one prepared repair after a hard interruption.

    The database's exact pre-commit fingerprint decides rollback. If SQLite
    already contains the current validated bundle fingerprint, the filesystem
    commit won and only the journal needs cleanup. Any third state is left
    untouched for manual investigation.
    """

    journal_path = directory / "journal.json"
    original_path = directory / "original.metadata.json"
    if directory.is_symlink() or not journal_path.is_file() or not original_path.is_file():
        raise RuntimeError("caption repair journal is incomplete")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    required = {
        "schema_version", "state", "row_id", "source_id", "job_key",
        "bundle_path", "database_pre", "original_sidecar_sha256",
        "old_assets", "new_assets",
    }
    if (
        not isinstance(journal, dict) or set(journal) != required
        or journal.get("schema_version") != 1 or journal.get("state") != "prepared"
        or type(journal.get("row_id")) is not int
        or not isinstance(journal.get("job_key"), str)
        or directory.name != journal.get("job_key")
    ):
        raise RuntimeError("caption repair journal metadata is invalid")
    root = (Path(download_root).resolve() / "bilibili").resolve()
    bundle = Path(str(journal["bundle_path"])).resolve()
    if not _is_below(bundle, root) or bundle.name != journal["job_key"]:
        raise RuntimeError("caption repair journal bundle path is unsafe")
    sidecar = bundle / "metadata.json"
    original = original_path.read_bytes()
    if hashlib.sha256(original).hexdigest() != journal["original_sidecar_sha256"]:
        raise RuntimeError("caption repair journal sidecar backup is corrupt")
    pre = journal.get("database_pre")
    if (
        not isinstance(pre, dict) or set(pre) != {"file_size", "content_hash"}
        or type(pre.get("file_size")) is not int
        or not isinstance(pre.get("content_hash"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", pre["content_hash"])
    ):
        raise RuntimeError("caption repair journal database fingerprint is invalid")
    connection = _read_connection(Path(db_path))
    try:
        row = connection.execute(
            "SELECT source, artifact_kind, status, source_id, job_key, bundle_path, "
            "file_size, content_hash FROM audio_urls WHERE id=?",
            (journal["row_id"],),
        ).fetchone()
    finally:
        connection.close()
    if row is None or (
        row["source"] != "bilibili" or row["artifact_kind"] != "video_bundle"
        or row["status"] != "done" or row["source_id"] != journal["source_id"]
        or row["job_key"] != journal["job_key"]
        or Path(str(row["bundle_path"])).resolve() != bundle
    ):
        raise RuntimeError("caption repair journal no longer matches its database row")

    database_fingerprint = (row["file_size"], row["content_hash"])
    pre_fingerprint = (pre["file_size"], pre["content_hash"])
    if database_fingerprint != pre_fingerprint:
        try:
            validate_bundle(sidecar)
            observed = bundle_fingerprint(bundle)
        except Exception as exc:
            raise RuntimeError(
                "caption repair has an ambiguous post-commit state"
            ) from exc
        if observed != database_fingerprint:
            raise RuntimeError("caption repair database and bundle both diverged")
        shutil.rmtree(directory)
        _fsync_directory(directory.parent)
        return {"job_key": journal["job_key"], "action": "finalized_committed"}

    new_assets = journal.get("new_assets")
    old_assets = journal.get("old_assets")
    if not isinstance(new_assets, list) or not isinstance(old_assets, list):
        raise RuntimeError("caption repair journal asset lists are invalid")
    old_hash_by_path = {
        str(record.get("path")): str(record.get("sha256"))
        for record in old_assets if isinstance(record, dict)
    }
    for record in new_assets:
        if (
            not isinstance(record, dict) or set(record) != {"path", "sha256"}
            or Path(str(record["path"])).name != record["path"]
            or not re.fullmatch(r"[0-9a-f]{64}", str(record["sha256"]))
        ):
            raise RuntimeError("caption repair journal new asset is invalid")
        target = bundle / record["path"]
        if target.exists():
            if target.is_symlink() or not target.is_file():
                raise RuntimeError("caption repair new asset changed after interruption")
            observed_hash = _sha256_path(target)
            if observed_hash == record["sha256"]:
                target.unlink()
            elif observed_hash != old_hash_by_path.get(record["path"]):
                raise RuntimeError("caption repair new asset changed after interruption")
    for record in old_assets:
        if (
            not isinstance(record, dict)
            or set(record) != {"path", "backup", "sha256"}
            or Path(str(record["path"])).is_absolute()
            or ".." in Path(str(record["path"])).parts
            or not str(record["backup"]).startswith("old/")
            or not re.fullmatch(r"[0-9a-f]{64}", str(record["sha256"]))
        ):
            raise RuntimeError("caption repair journal old asset is invalid")
        target = (bundle / record["path"]).resolve()
        backup = (directory / record["backup"]).resolve()
        if bundle not in target.parents or directory not in backup.parents:
            raise RuntimeError("caption repair journal asset path escapes its root")
        if not backup.is_file() or _sha256_path(backup) != record["sha256"]:
            raise RuntimeError("caption repair journal old asset backup is corrupt")
        if target.exists() and (target.is_symlink() or _sha256_path(target) != record["sha256"]):
            raise RuntimeError("caption repair old asset changed after interruption")
        if not target.exists():
            _atomic_write_bytes(target, backup.read_bytes())
    _atomic_write_bytes(sidecar, original)
    _fsync_directory(bundle)
    validate_bundle(sidecar)
    if bundle_fingerprint(bundle) != pre_fingerprint:
        raise RuntimeError("caption repair rollback did not restore the prior closure")
    shutil.rmtree(directory)
    _fsync_directory(directory.parent)
    return {"job_key": journal["job_key"], "action": "rolled_back"}


def recover_interrupted_repairs(
    db_path: Path, download_root: Path,
) -> list[dict[str, Any]]:
    root = (Path(download_root).resolve() / "bilibili").resolve()
    journal_root = root / ".caption-backfill-journal"
    if not journal_root.exists():
        return []
    if journal_root.is_symlink() or not journal_root.is_dir():
        raise RuntimeError("caption repair journal root is unsafe")
    recovered = []
    for directory in sorted(journal_root.iterdir()):
        if not directory.is_dir() or directory.is_symlink():
            raise RuntimeError("caption repair journal root contains an unsafe entry")
        try:
            preview = json.loads((directory / "journal.json").read_text(encoding="utf-8"))
            bundle = Path(str(preview["bundle_path"])).resolve()
            job_key = str(preview["job_key"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("caption repair journal cannot be inspected safely") from exc
        with job_lock(bundle.parents[1], job_key):
            recovered.append(
                _recover_one_journal(db_path, download_root, directory)
            )
    return recovered


def _restore_bundle(
    sidecar: Path,
    original_sidecar: bytes,
    moved: list[Path],
    displaced: list[tuple[Path, Path]] | None = None,
    rollback_dir: Path | None = None,
) -> None:
    """Restore new and displaced caption files plus the original sidecar.

    ``displaced`` contains ``(rollback_copy, original_path)`` pairs.  Cleanup
    happens only after every state domain validates, so an incomplete recovery
    keeps its same-filesystem rollback directory for manual inspection.
    """

    errors: list[BaseException] = []
    for path in reversed(moved):
        try:
            path.unlink(missing_ok=True)
        except BaseException as exc:
            errors.append(exc)
    for backup, original in reversed(displaced or []):
        try:
            if backup.exists():
                _atomic_write_bytes(original, backup.read_bytes())
        except BaseException as exc:
            errors.append(exc)
    try:
        if not sidecar.is_file() or sidecar.read_bytes() != original_sidecar:
            _atomic_write_bytes(sidecar, original_sidecar)
    except BaseException as exc:
        errors.append(exc)
    try:
        validate_bundle(sidecar)
    except BaseException as exc:
        errors.append(exc)
    if errors:
        raise RuntimeError(
            "caption repair rollback could not restore every state domain"
        ) from errors[0]
    if rollback_dir is not None:
        parent = rollback_dir.parent
        shutil.rmtree(rollback_dir)
        _fsync_directory(parent)


def _commit_sidecar_and_database(
    db_path: Path,
    candidate: dict[str, Any],
    metadata: dict[str, Any],
    prepared: list[dict[str, Any]],
    rejected: list[dict[str, Any]] | None = None,
    visual_ocr: dict[str, Any] | None = None,
) -> tuple[int, str]:
    """Exception-safely promote captions and CAS-update the matching DB row."""

    bundle = candidate["bundle"]
    # The network/caption preparation phase may be slow. Recheck the old disk
    # closure and DB snapshot immediately before the first filesystem mutation.
    _verify_pre_mutation(db_path, candidate)
    sidecar = bundle / "metadata.json"
    original_sidecar = sidecar.read_bytes()
    moved: list[Path] = []
    displaced: list[tuple[Path, Path]] = []
    rollback_dir: Path | None = None
    connection: sqlite3.Connection | None = None
    transaction_started = False
    database_committed = False
    try:
        files = metadata["files"]
        caption = metadata.get("caption") or {}
        caption_rows = [
            *(caption.get("tracks") or []),
            *(caption.get("rejected_tracks") or []),
        ]
        old_caption_keys = sorted({
            key
            for row in caption_rows if isinstance(row, dict)
            for key in ((row.get("files") or {}).values())
            if isinstance(key, str)
        })
        old_visual_ocr_keys: list[str] = []
        if visual_ocr is not None:
            old_visual = ((metadata.get("derived_text") or {}).get("visual_ocr") or {})
            old_visual_ocr_keys = sorted({
                key for key in (old_visual.get("files") or {}).values()
                if isinstance(key, str)
            })
        displaced_keys = [*old_caption_keys, *old_visual_ocr_keys]
        old_assets: list[tuple[str, Path]] = []
        for key in displaced_keys:
            if key not in files:
                raise ValueError("old caption file key is missing from bundle closure")
            record = files[key]
            relative = Path(str(record.get("path") or ""))
            unresolved = bundle / relative
            original = unresolved.resolve()
            if (
                relative.is_absolute() or ".." in relative.parts
                or bundle not in original.parents
                or unresolved.is_symlink() or not original.is_file()
            ):
                raise ValueError("old caption file is unsafe or missing")
            old_assets.append((relative.as_posix(), original))
        rejected = rejected or []
        outcomes = (
            [("downloaded", value) for value in prepared]
            + [("rejected", value) for value in rejected]
        )
        new_assets = [
            Path(source)
            for _outcome, item in outcomes
            for source in item["paths"].values()
        ]
        if visual_ocr is not None:
            new_assets.extend(Path(source) for source in visual_ocr["paths"].values())
        rollback_dir = _journal_directory(bundle, candidate["job_key"])
        try:
            journal = _write_repair_journal(
                rollback_dir, candidate, sidecar, old_assets, new_assets
            )
        except BaseException:
            if rollback_dir.exists():
                shutil.rmtree(rollback_dir)
                if rollback_dir.parent.exists():
                    _fsync_directory(rollback_dir.parent)
            rollback_dir = None
            raise
        for key, old_record in zip(displaced_keys, journal["old_assets"]):
            relative = Path(old_record["path"])
            original = (bundle / relative).resolve()
            backup = (rollback_dir / old_record["backup"]).resolve()
            os.replace(original, backup)
            displaced.append((backup, original))
            files.pop(key)
        captions = []
        rejected_captions = []
        for outcome, item in outcomes:
            keys = item["track"].get("files") or {
                extension: f"caption_{item['index']}_{extension}"
                for extension in item["paths"]
            }
            for extension, source in item["paths"].items():
                destination = bundle / source.name
                if destination.exists() or keys[extension] in files:
                    raise FileExistsError("caption destination already exists")
                os.replace(source, destination)
                moved.append(destination)
                files[keys[extension]] = _file_record(destination, bundle)
            persisted = dict(item["track"])
            persisted["files"] = keys
            if outcome == "downloaded":
                captions.append(persisted)
            else:
                rejected_captions.append(persisted)
        metadata["caption"]["tracks"] = captions
        metadata["caption"]["track_count"] = len(captions)
        if rejected_captions:
            metadata["caption"].update({
                "payload_status": "partial" if captions else "rejected",
                "rejected_track_count": len(rejected_captions),
                "rejected_tracks": rejected_captions,
            })
        else:
            for key in (
                "payload_status", "rejected_track_count", "rejected_tracks",
            ):
                metadata["caption"].pop(key, None)
        if visual_ocr is not None:
            document = visual_ocr.get("document")
            paths = visual_ocr.get("paths")
            if not isinstance(document, dict) or not isinstance(paths, dict):
                raise ValueError("staged visual OCR result is invalid")
            keys = {
                extension: f"visual_ocr_{extension}"
                for extension in paths
            }
            for extension, source in paths.items():
                if extension not in {"json", "vtt", "txt"}:
                    raise ValueError("visual OCR payload extension is invalid")
                source = Path(source)
                destination = bundle / source.name
                if destination.exists() or keys[extension] in files:
                    raise FileExistsError("visual OCR destination already exists")
                os.replace(source, destination)
                moved.append(destination)
                files[keys[extension]] = _file_record(destination, bundle)
            derived_text = metadata.setdefault("derived_text", {})
            if not isinstance(derived_text, dict):
                raise ValueError("derived_text sidecar namespace is invalid")
            derived_text["visual_ocr"] = _visual_ocr_sidecar(document, keys)
            metadata["schema_version"] = CURRENT_SIDECAR_SCHEMA_VERSION
            policy = metadata.get("acquisition_policy")
            if not isinstance(policy, dict):
                raise ValueError("acquisition_policy sidecar namespace is invalid")
            # This is post-acquisition enrichment. Do not rewrite acquisition
            # policy, because that policy participates in immutable job identity.
        write_json_atomic(sidecar, metadata)
        _fsync_directory(bundle)
        validate_bundle(sidecar)
        total, content_hash = bundle_fingerprint(bundle)
        connection = sqlite3.connect(str(Path(db_path).resolve()), timeout=60)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        transaction_started = True
        _verify_exact_row(connection, candidate, bundle)
        cursor = connection.execute(
            "UPDATE audio_urls SET file_size=?, content_hash=? "
            "WHERE id=? AND source='bilibili' AND artifact_kind='video_bundle' "
            "AND status='done' AND source_id=? AND job_key=? "
            "AND bundle_path=? AND local_path=? AND file_size=? AND content_hash=?",
            (
                total, content_hash, candidate["id"], candidate["source_id"],
                candidate["job_key"], candidate["_bundle_path_db"],
                candidate["_local_path_db"], candidate["_old_file_size"],
                candidate["_old_content_hash"],
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("exact SQLite artifact update did not match one row")
        connection.commit()
        database_committed = True
        if rollback_dir is not None:
            shutil.rmtree(rollback_dir)
            _fsync_directory(rollback_dir.parent)
        return total, content_hash
    except BaseException:
        if (
            transaction_started and connection is not None
            and not connection.in_transaction
        ):
            # Covers an async exception delivered immediately after commit().
            database_committed = True
        if not database_committed:
            recovery_errors: list[BaseException] = []
            if connection is not None and connection.in_transaction:
                try:
                    connection.rollback()
                except BaseException as rollback_error:
                    recovery_errors.append(rollback_error)
            try:
                _restore_bundle(
                    sidecar, original_sidecar, moved, displaced, rollback_dir
                )
            except BaseException as restore_error:
                recovery_errors.append(restore_error)
            if recovery_errors:
                raise RuntimeError(
                    "caption repair rollback could not restore every state domain"
                ) from recovery_errors[0]
        raise
    finally:
        if connection is not None:
            connection.close()


async def repair_candidate(
    client: BilibiliClient, db_path: Path, candidate: dict[str, Any],
    *, enable_visual_ocr: bool = False, visual_ocr_config: Any = None,
    visual_ocr_engine: Any = None, visual_ocr_engine_factory: Any = None,
    visual_ocr_timeout_seconds: int = 14_400,
) -> dict[str, Any]:
    bundle = candidate["bundle"]
    output_root = bundle.parents[1]
    with job_lock(output_root, candidate["job_key"]):
        try:
            result = _verify_pre_mutation(db_path, candidate)
        except Exception as exc:
            raise _phase_failure("preflight", exc) from exc
        metadata = copy.deepcopy(result["metadata"])
        job = _job_from_metadata(metadata)
        try:
            inventory, tracks, status, attempts = await resolve_caption_inventory(
                client, job,
                strict_refresh=bool(
                    (metadata.get("acquisition_policy") or {}).get("require_caption")
                ),
            )
        except Exception as exc:
            raise _phase_failure("inventory", exc) from exc
        if any(
            not caption_language_matches_content(
                str(metadata.get("content_language") or ""), track["language"]
            )
            for track in tracks
        ):
            cause = ValueError(
                "selected caption language does not match video content language"
            )
            raise _phase_failure("language_policy", cause) from cause
        stage = Path(tempfile.mkdtemp(
            prefix=f".{candidate['job_key']}.captions-", dir=output_root
        ))
        try:
            try:
                prepared, rejected = await _prepare_captions(
                    client, tracks, stage,
                    float(metadata["media"]["duration_seconds"]),
                )
            except Exception as exc:
                raise _phase_failure("caption_payload", exc) from exc
            require_caption = bool(
                (metadata.get("acquisition_policy") or {}).get("require_caption")
            )
            if rejected and not prepared and require_caption:
                cause = ValueError(
                    "no acceptable Bilibili platform caption is available"
                )
                raise _phase_failure("caption_payload", cause) from cause
            metadata["caption"].update({
                "status": (
                    "invalid_timeline" if rejected and not prepared
                    else "downloaded" if prepared else status
                ),
                "need_login_subtitle": inventory.get("need_login_subtitle"),
                "response_authenticated": client.authenticated,
                "requested_languages": job["languages"],
                "inventory_attempt_count": len(attempts),
                "inventory_attempts": attempts,
            })
            visual_ocr = None
            if enable_visual_ocr and not prepared:
                try:
                    if visual_ocr_engine is None and visual_ocr_engine_factory is not None:
                        visual_ocr_engine = await asyncio.to_thread(
                            visual_ocr_engine_factory
                        )
                    result = await asyncio.to_thread(
                        _prepare_visual_ocr,
                        bundle / "source.mp4",
                        config=_visual_ocr_config_for_language(
                            visual_ocr_config,
                            str(metadata.get("content_language") or ""),
                        ),
                        engine=visual_ocr_engine,
                        timeout_seconds=visual_ocr_timeout_seconds,
                    )
                    visual_ocr = _stage_visual_ocr_result(result, stage)
                except Exception as exc:
                    raise _phase_failure("visual_ocr", exc) from exc
            try:
                total, _ = _commit_sidecar_and_database(
                    db_path, candidate, metadata, prepared, rejected,
                    visual_ocr=visual_ocr,
                )
            except Exception as exc:
                raise _phase_failure("commit", exc) from exc
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    return {
        "id": candidate["id"],
        "source_id": candidate["source_id"],
        "job_key": candidate["job_key"],
        "before": candidate["caption_status"],
        "after": metadata["caption"]["status"],
        "track_count": metadata["caption"]["track_count"],
        "rejected_track_count": metadata["caption"].get(
            "rejected_track_count", 0
        ),
        "visual_ocr_status": (
            ((metadata.get("derived_text") or {}).get("visual_ocr") or {}).get(
                "status"
            )
        ),
        "bundle_bytes": total,
    }


async def async_main(args: argparse.Namespace) -> int:
    if args.refresh_visual_ocr and not args.visual_ocr:
        raise ValueError("--refresh-visual-ocr requires --visual-ocr")
    recovered = (
        recover_interrupted_repairs(args.db, args.downloads)
        if args.apply else []
    )
    discovery: dict[str, int] = {}
    candidates = discover_candidates(
        args.db, args.downloads, args.limit,
        include_invalid_timeline=args.retry_invalid_timeline,
        skip_existing_visual_ocr=(
            args.visual_ocr and not args.refresh_visual_ocr
        ),
        job_keys=set(args.job_key) if args.job_key else None,
        audit=discovery,
    )
    requested_not_eligible = sorted(
        set(args.job_key) - {candidate["job_key"] for candidate in candidates}
    )
    if not args.apply:
        print(json.dumps({
            "mode": "dry-run",
            "candidate_count": len(candidates),
            "discovery": discovery,
            "requested_job_keys_not_eligible": requested_not_eligible,
            "visual_ocr_enabled": bool(args.visual_ocr),
            "candidates": [{
                **{
                    key: value for key, value in candidate.items()
                    if key != "bundle" and not key.startswith("_")
                },
                "bundle_path": str(candidate["bundle"]),
                "database_file_size": candidate["_old_file_size"],
                "database_content_hash": candidate["_old_content_hash"],
            } for candidate in candidates],
            "next_step": (
                "review, then rerun with --apply --visual-ocr"
                if args.visual_ocr else "review, then rerun with --apply"
            ),
        }, ensure_ascii=False, indent=2))
        return 2 if requested_not_eligible else 0
    if not candidates:
        print(json.dumps({
            "mode": "apply", "candidate_count": 0,
            "visual_ocr_enabled": bool(args.visual_ocr),
            "recovered_interrupted_repairs": recovered,
            "discovery": discovery,
            "requested_job_keys_not_eligible": requested_not_eligible,
        }, ensure_ascii=False))
        return 2 if requested_not_eligible else 0

    backup = backup_database(args.db)
    cookie = os.environ.get("BILIBILI_COOKIE", "") if args.allow_bilibili_cookie else ""
    if cookie and (len(cookie) > 16_384 or "\r" in cookie or "\n" in cookie):
        raise ValueError("BILIBILI_COOKIE is too long or contains a newline")
    timeout = aiohttp.ClientTimeout(total=120)
    completed = []
    failures = []
    visual_ocr_config = _build_visual_ocr_config(args) if args.visual_ocr else None
    async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
        client = BilibiliClient(
            session, auth_cookie=cookie, proxy=get_bilibili_proxy()
        )
        # The client now owns the in-memory value needed for platform API
        # requests. Remove the ambient credential before any optional OCR
        # engine/model setup; neither FFmpeg nor PaddleOCR needs it.
        os.environ.pop("BILIBILI_COOKIE", None)
        cookie = ""
        for candidate in candidates:
            try:
                completed.append(await repair_candidate(
                    client, args.db, candidate,
                    enable_visual_ocr=bool(args.visual_ocr),
                    visual_ocr_config=visual_ocr_config,
                    visual_ocr_timeout_seconds=args.ocr_timeout_seconds,
                ))
            except Exception as exc:
                failure = {
                    "id": candidate["id"],
                    "source_id": candidate["source_id"],
                    "job_key": candidate["job_key"],
                    "error_type": (
                        exc.error_type
                        if isinstance(exc, CaptionBackfillFailure)
                        else type(exc).__name__
                    ),
                    "phase": (
                        exc.phase
                        if isinstance(exc, CaptionBackfillFailure)
                        else "unclassified"
                    ),
                    "reason": (
                        exc.reason
                        if isinstance(exc, CaptionBackfillFailure)
                        else _safe_failure_reason(exc)
                    ),
                }
                failures.append(failure)
    print(json.dumps({
        "mode": "apply",
        "backup": str(backup),
        "candidate_count": len(candidates),
        "recovered_interrupted_repairs": recovered,
        "discovery": discovery,
        "requested_job_keys_not_eligible": requested_not_eligible,
        "visual_ocr_enabled": bool(args.visual_ocr),
        "completed": completed,
        "failures": failures,
    }, ensure_ascii=False, indent=2))
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(build_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
