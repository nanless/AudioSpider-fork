#!/usr/bin/env python3
"""Move legacy standalone video bundles into the shared downloads hierarchy.

Dry-run is the default.  ``--apply`` performs an idempotent move, validates the
bundle at its new location and registers the completed artifact in the shared
SQLite queue without modifying historical audio rows.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bilibili_dataset import validate_bundle as validate_bilibili_bundle
from config import DB_PATH, DOWNLOAD_DIR
from media_artifacts import bundle_fingerprint
from storage import AudioRecord, Storage
from youtube_dataset import _validate_bundle as validate_youtube_bundle


DEFAULT_ROOTS = [
    Path(DOWNLOAD_DIR) / "bilibili-video-20260910",
    Path(DOWNLOAD_DIR) / "youtube-staging-20260909",
]


def _safe_category(value: str) -> str:
    value = re.sub(r"[^\w\u4e00-\u9fff]", "_", str(value or "")).strip("_")
    return value[:80] or "视频"


def _load_sidecar(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"sidecar must be an object: {path}")
    return value


def _category(metadata: dict[str, Any]) -> str:
    if metadata.get("source") == "youtube":
        return "访谈" if metadata.get("profile") == "youtube_interviews" else "影视"
    source_metadata = metadata.get("source_metadata") or {}
    declared = source_metadata.get("category") or source_metadata.get("tname")
    if declared:
        return _safe_category(declared)
    searchable = " ".join(str(value or "") for value in (
        source_metadata.get("title"), metadata.get("part_title"), metadata.get("program")
    ))
    for category, needles in (
        ("有声书", ("有声书", "有声小说", "听书")),
        ("评书", ("评书",)), ("相声", ("相声",)),
        ("访谈", ("访谈", "人物对谈")), ("演讲", ("演讲", "讲座")),
        ("播客", ("播客",)), ("影视", ("电影", "电视剧", "影视")),
    ):
        if any(needle in searchable for needle in needles):
            return category
    return "视频"


def discover(
    roots: list[Path], download_root: Path = Path(DOWNLOAD_DIR)
) -> list[dict[str, Any]]:
    jobs = []
    for root in roots:
        if not root.exists():
            continue
        for sidecar in sorted(root.rglob("metadata.json")):
            metadata = _load_sidecar(sidecar)
            source = metadata.get("source")
            if source not in {"youtube", "bilibili"}:
                continue
            source_id = str(metadata.get("source_id") or "")
            job_key = str(metadata.get("job_key") or sidecar.parent.name)
            if not source_id or not job_key:
                raise ValueError(f"bundle has no stable identity: {sidecar}")
            category = _category(metadata)
            destination = (
                Path(download_root) / source / category / source_id / job_key
            ).resolve()
            jobs.append({
                "source": source,
                "source_id": source_id,
                "category": category,
                "source_path": sidecar.parent.resolve(),
                "destination": destination,
                "metadata": metadata,
            })
    return jobs


def discover_integrated(download_root: Path = Path(DOWNLOAD_DIR)) -> list[dict[str, Any]]:
    """Find already-moved bundles so an interrupted DB registration can resume."""

    jobs = []
    download_root = Path(download_root).resolve()
    for source in ("bilibili", "youtube"):
        source_root = download_root / source
        if not source_root.exists():
            continue
        for sidecar in sorted(source_root.rglob("metadata.json")):
            metadata = _load_sidecar(sidecar)
            if metadata.get("source") != source:
                continue
            bundle = sidecar.parent.resolve()
            source_id = str(metadata.get("source_id") or "")
            job_key = str(metadata.get("job_key") or bundle.name)
            if bundle.name != job_key or bundle.parent.name != source_id:
                continue
            category = bundle.parent.parent.name
            if bundle.parent.parent.parent != source_root:
                continue
            jobs.append({
                "source": source, "source_id": source_id, "category": category,
                "source_path": bundle, "destination": bundle, "metadata": metadata,
            })
    return jobs


def _validate(job: dict[str, Any], directory: Path) -> None:
    if job["source"] == "youtube":
        validate_youtube_bundle(directory / "metadata.json")
    else:
        validate_bilibili_bundle(directory / "metadata.json")


def _backup_database(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.backup-{stamp}")
    source = sqlite3.connect(path)
    target = sqlite3.connect(backup)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return backup


def _build_record(job: dict[str, Any], directory: Path) -> AudioRecord:
    metadata = job["metadata"]
    canonical = str(metadata.get("canonical_url") or "")
    if not canonical:
        raise ValueError(f"bundle has no canonical_url: {directory}")
    title = str(
        metadata.get("title") or metadata.get("part_title")
        or metadata.get("program") or job["source_id"]
    )
    duration = int(float(
        metadata.get("duration_seconds")
        or metadata.get("declared_duration_seconds") or 0
    ))
    total, fingerprint = bundle_fingerprint(directory)
    if job["source"] == "youtube":
        portable_task = {
            "url": canonical,
            "profile": metadata.get("profile"),
            "content_language": metadata.get("language") or "und",
            "languages": metadata.get("requested_languages") or [
                (metadata.get("caption") or {}).get("track_language")
            ],
            "speaker_count": metadata.get("speaker_count"),
            "speaker_count_status": metadata.get("speaker_count_status", "needs_review"),
            "program": metadata.get("program") or "",
            "rights": metadata.get("rights") or {"status": "needs_review"},
            "ai_generation": metadata.get("ai_generation") or {
                "status": "unknown", "evidence": [],
            },
            "source_revision": metadata.get("source_revision", "current"),
        }
        portable_task["languages"] = [
            value for value in portable_task["languages"] if value
        ]
        source_data = {"artifact_kind": "video_bundle", "job": portable_task}
    else:
        policy = metadata.get("acquisition_policy") or {}
        source_data = {
            "artifact_kind": "video_bundle",
            "bvid": metadata.get("bvid"), "cid": metadata.get("cid"),
            "page": metadata.get("part"),
            "download_task": {
                "artifact_kind": "video_bundle",
                "bvid": metadata.get("bvid"), "cid": metadata.get("cid"),
                "page": metadata.get("part"), "parts": [metadata.get("part")],
                "max_parts": 1, "max_height": policy.get("max_height", 720),
                "max_duration_seconds": policy.get("max_duration_seconds", 4 * 3600),
                "content_language": metadata.get("content_language") or "zh",
                "caption_policy": {
                    "mode": "all_matching_public_tracks",
                    "languages": (metadata.get("caption") or {}).get(
                        "requested_languages", []
                    ),
                    "require_caption": bool(policy.get("require_caption", False)),
                },
                "rights": metadata.get("rights") or {"status": "needs_review"},
                "ai_generation": metadata.get("ai_generation") or {
                    "status": "unknown", "evidence": [],
                },
                "speaker_count": metadata.get("speaker_count"),
                "speaker_count_status": metadata.get(
                    "speaker_count_status", "needs_review"
                ),
                "source_revision": policy.get("source_revision", "current"),
            },
        }
    record = AudioRecord(
        url=canonical,
        source=job["source"],
        title=title,
        file_format="mp4",
        file_size=total,
        duration=duration,
        language=str(metadata.get("language") or metadata.get("content_language") or ""),
        category=job["category"],
        webpage_url=canonical,
        author=str(metadata.get("channel") or ""),
        metadata_json=json.dumps({
            "schema_version": 1,
            "common": {
                "webpage_url": canonical,
                "author": str(metadata.get("channel") or ""),
                "categories": [job["category"]],
            },
            "source_data": {job["source"]: source_data},
            "migration": {
                "from": str(job["source_path"]),
                "sidecar_sha256": fingerprint,
            },
        }, ensure_ascii=False, separators=(",", ":")),
        artifact_kind="video_bundle",
        job_key=str(metadata.get("job_key") or directory.name),
        source_id=job["source_id"],
    )
    record.local_path = str(directory / "source.mp4")
    record.bundle_path = str(directory)
    record.content_hash = fingerprint
    record.discovered_at = datetime.now(timezone.utc).isoformat()
    return record


def _register_in_transaction(storage: Storage, record: AudioRecord) -> int:
    """Insert or verify one immutable job without committing the caller transaction."""

    conn = storage._get_conn()
    row = conn.execute(
        "SELECT * FROM audio_urls WHERE source=? AND artifact_kind=? AND job_key=?",
        (record.source, record.artifact_kind, record.job_key),
    ).fetchone()
    if row is None:
        cursor = conn.execute(
            "INSERT INTO audio_urls "
            "(url, source, artifact_kind, job_key, title, file_format, file_size, duration, "
            "language, category, speaker, webpage_url, description, author, cover_url, "
            "metadata_json, status, local_path, bundle_path, content_hash, source_id, "
            "published_at, discovered_at) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'downloading', ?, ?, ?, ?, ?, ?)",
            (
                record.url, record.source, record.artifact_kind, record.job_key,
                record.title, record.file_format, record.file_size, record.duration,
                record.language, record.category, record.speaker, record.webpage_url,
                record.description, record.author, record.cover_url, record.metadata_json,
                record.local_path, record.bundle_path, record.content_hash,
                record.source_id, record.published_at, record.discovered_at,
            ),
        )
        record_id = int(cursor.lastrowid)
    else:
        if (
            row["url"] != record.url
            or row["source_id"] != record.source_id
            or row["artifact_kind"] != "video_bundle"
        ):
            raise ValueError("database job_key conflicts with another source artifact")
        record_id = int(row["id"])
    conn.execute(
        "UPDATE audio_urls SET status='done', title=?, file_format=?, file_size=?, duration=?, "
        "language=?, category=?, webpage_url=?, author=?, metadata_json=?, local_path=?, "
        "bundle_path=?, content_hash=?, downloaded_at=?, claimed_by='', claimed_at='', "
        "lease_expires_at='' WHERE id=?",
        (
            record.title, record.file_format, record.file_size, record.duration,
            record.language, record.category, record.webpage_url, record.author,
            record.metadata_json, record.local_path, record.bundle_path,
            record.content_hash, datetime.now(timezone.utc).isoformat(), record_id,
        ),
    )
    return record_id


def _database_has_job(db_path: Path, job: dict[str, Any]) -> bool:
    if not db_path.is_file():
        return False
    connection = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(audio_urls)")}
        if "job_key" not in columns:
            return False
        key = str(job["metadata"].get("job_key") or job["destination"].name)
        return connection.execute(
            "SELECT 1 FROM audio_urls WHERE source=? AND artifact_kind='video_bundle' "
            "AND job_key=? AND status='done' AND bundle_path=?",
            (job["source"], key, str(job["destination"])),
        ).fetchone() is not None
    finally:
        connection.close()


def migrate(
    roots: list[Path], *, apply: bool, db_path: Path,
    download_root: Path = Path(DOWNLOAD_DIR),
) -> dict[str, Any]:
    discovered = discover(roots, download_root) + discover_integrated(download_root)
    jobs_by_path = {str(job["source_path"]): job for job in discovered}
    jobs = list(jobs_by_path.values())
    actionable = [
        job for job in jobs
        if job["source_path"] != job["destination"] or not _database_has_job(db_path, job)
    ]
    report: dict[str, Any] = {
        "mode": "apply" if apply else "audit-only",
        "discovered": len(jobs), "actionable": len(actionable),
        "moved": 0, "already_migrated": 0,
        "registered": 0, "failures": [], "database_backup": None, "jobs": [],
    }
    storage = None
    if apply and actionable:
        report["database_backup"] = str(_backup_database(db_path))
        storage = Storage(str(db_path))
    for job in actionable if apply else jobs:
        source = job["source_path"]
        destination = job["destination"]
        row = {
            "source": job["source"], "source_id": job["source_id"],
            "category": job["category"], "from": str(source), "to": str(destination),
        }
        moved = False
        conn = None
        try:
            _validate(job, source)
            if apply:
                conn = storage._get_conn()
                conn.execute("BEGIN IMMEDIATE")
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    _validate(job, destination)
                    if source != destination:
                        if bundle_fingerprint(source) != bundle_fingerprint(destination):
                            raise ValueError("source and destination bundles conflict")
                    report["already_migrated"] += 1
                else:
                    os.replace(source, destination)
                    moved = True
                    report["moved"] += 1
                _validate(job, destination)
                record = _build_record(job, destination)
                _register_in_transaction(storage, record)
                conn.commit()
                report["registered"] += 1
            row["status"] = "ready" if not apply else "migrated"
        except Exception as exc:
            if conn is not None:
                conn.rollback()
            if moved and destination.exists() and not source.exists():
                try:
                    source.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(destination, source)
                    report["moved"] -= 1
                    row["rolled_back_move"] = True
                except Exception as rollback_exc:
                    row["rollback_error"] = str(rollback_exc)
            row["status"] = "failed"
            row["error"] = str(exc)
            report["failures"].append(row.copy())
        report["jobs"].append(row)
    report["failure_count"] = len(report["failures"])
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-root", action="append", type=Path, dest="roots")
    parser.add_argument("--db", type=Path, default=Path(DB_PATH))
    parser.add_argument("--downloads", type=Path, default=Path(DOWNLOAD_DIR))
    parser.add_argument("--apply", action="store_true", help="执行可恢复的移动和数据库登记")
    parser.add_argument("--report", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = migrate(
        args.roots or DEFAULT_ROOTS, apply=args.apply,
        db_path=args.db, download_root=args.downloads,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 1 if report["failure_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
