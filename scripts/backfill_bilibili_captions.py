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
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bilibili_dataset import (
    BilibiliClient,
    _caption_basename,
    _file_record,
    caption_language_matches_content,
    default_caption_languages,
    job_lock,
    parse_subtitle_document,
    render_subtitle_text,
    render_subtitle_vtt,
    resolve_caption_inventory,
    validate_bundle,
)
from bilibili_proxy import get_bilibili_proxy
from youtube_dataset import write_json_atomic, write_text_atomic


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="B站已完成 bundle 的字幕-only 回填（默认只预览）"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--downloads", type=Path, default=DEFAULT_DOWNLOAD_DIR)
    parser.add_argument("--limit", type=positive_int, default=100)
    parser.add_argument(
        "--apply", action="store_true",
        help="显式执行回填；省略时不发起网络请求且不写文件/数据库",
    )
    parser.add_argument(
        "--allow-bilibili-cookie", action="store_true",
        help="仅在已明确授权时从当前进程 BILIBILI_COOKIE 读取登录态",
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
    candidates = []
    for row in rows:
        bundle = Path(str(row["bundle_path"] or "")).resolve()
        sidecar = bundle / "metadata.json"
        if not _is_below(bundle, root) or not sidecar.is_file():
            continue
        try:
            result = validate_bundle(sidecar)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        metadata = result["metadata"]
        caption_status = str((metadata.get("caption") or {}).get("status") or "")
        if caption_status not in REFRESHABLE_STATUSES:
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
        if len(candidates) >= limit:
            break
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
) -> list[dict[str, Any]]:
    prepared = []
    for index, track in enumerate(tracks):
        document = await client.subtitle(track["url"])
        cues = parse_subtitle_document(document)
        base = _caption_basename(track, index)
        paths = {
            "json": stage / f"{base}.json",
            "vtt": stage / f"{base}.vtt",
            "txt": stage / f"{base}.txt",
        }
        write_json_atomic(paths["json"], document)
        write_text_atomic(paths["vtt"], render_subtitle_vtt(cues))
        write_text_atomic(paths["txt"], render_subtitle_text(cues))
        persisted = {
            key: value for key, value in track.items()
            if key not in {"url", "selection_rank"}
        }
        persisted.update({"status": "downloaded", "cue_count": len(cues)})
        prepared.append({"index": index, "paths": paths, "track": persisted})
    return prepared


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


def _restore_bundle(sidecar: Path, original_sidecar: bytes, moved: list[Path]) -> None:
    """Idempotently restore one bundle after an interrupted mutation window."""

    for path in reversed(moved):
        path.unlink(missing_ok=True)
    if not sidecar.is_file() or sidecar.read_bytes() != original_sidecar:
        _atomic_write_bytes(sidecar, original_sidecar)
    validate_bundle(sidecar)


def _commit_sidecar_and_database(
    db_path: Path,
    candidate: dict[str, Any],
    metadata: dict[str, Any],
    prepared: list[dict[str, Any]],
) -> tuple[int, str]:
    """Exception-safely promote captions and CAS-update the matching DB row."""

    bundle = candidate["bundle"]
    # The network/caption preparation phase may be slow. Recheck the old disk
    # closure and DB snapshot immediately before the first filesystem mutation.
    _verify_pre_mutation(db_path, candidate)
    sidecar = bundle / "metadata.json"
    original_sidecar = sidecar.read_bytes()
    moved: list[Path] = []
    connection: sqlite3.Connection | None = None
    transaction_started = False
    database_committed = False
    try:
        files = metadata["files"]
        captions = []
        for item in prepared:
            keys = {
                extension: f"caption_{item['index']}_{extension}"
                for extension in ("json", "vtt", "txt")
            }
            for extension, source in item["paths"].items():
                destination = bundle / source.name
                if destination.exists() or keys[extension] in files:
                    raise FileExistsError("caption destination already exists")
                os.replace(source, destination)
                moved.append(destination)
                files[keys[extension]] = _file_record(destination, bundle)
            persisted = item["track"]
            persisted["files"] = keys
            captions.append(persisted)
        metadata["caption"]["tracks"] = captions
        metadata["caption"]["track_count"] = len(captions)
        write_json_atomic(sidecar, metadata)
        validate_bundle(sidecar)
        total, content_hash = bundle_fingerprint(bundle)
        connection = sqlite3.connect(str(Path(db_path).resolve()), timeout=60)
        connection.row_factory = sqlite3.Row
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
                _restore_bundle(sidecar, original_sidecar, moved)
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
                prepared = await _prepare_captions(client, tracks, stage)
            except Exception as exc:
                raise _phase_failure("caption_payload", exc) from exc
            metadata["caption"].update({
                "status": "downloaded" if prepared else status,
                "need_login_subtitle": inventory.get("need_login_subtitle"),
                "response_authenticated": client.authenticated,
                "requested_languages": job["languages"],
                "inventory_attempt_count": len(attempts),
                "inventory_attempts": attempts,
            })
            try:
                total, _ = _commit_sidecar_and_database(
                    db_path, candidate, metadata, prepared
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
        "bundle_bytes": total,
    }


async def async_main(args: argparse.Namespace) -> int:
    candidates = discover_candidates(args.db, args.downloads, args.limit)
    if not args.apply:
        print(json.dumps({
            "mode": "dry-run",
            "candidate_count": len(candidates),
            "candidates": [{
                **{
                    key: value for key, value in candidate.items()
                    if key != "bundle" and not key.startswith("_")
                },
                "bundle_path": str(candidate["bundle"]),
                "database_file_size": candidate["_old_file_size"],
                "database_content_hash": candidate["_old_content_hash"],
            } for candidate in candidates],
            "next_step": "review, then rerun with --apply",
        }, ensure_ascii=False, indent=2))
        return 0
    if not candidates:
        print(json.dumps({"mode": "apply", "candidate_count": 0}, ensure_ascii=False))
        return 0

    backup = backup_database(args.db)
    cookie = os.environ.get("BILIBILI_COOKIE", "") if args.allow_bilibili_cookie else ""
    if cookie and (len(cookie) > 16_384 or "\r" in cookie or "\n" in cookie):
        raise ValueError("BILIBILI_COOKIE is too long or contains a newline")
    timeout = aiohttp.ClientTimeout(total=120)
    completed = []
    failures = []
    async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
        client = BilibiliClient(
            session, auth_cookie=cookie, proxy=get_bilibili_proxy()
        )
        for candidate in candidates:
            try:
                completed.append(await repair_candidate(client, args.db, candidate))
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
        "completed": completed,
        "failures": failures,
    }, ensure_ascii=False, indent=2))
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(build_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
