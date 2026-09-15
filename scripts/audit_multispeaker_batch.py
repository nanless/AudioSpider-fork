#!/usr/bin/env python3
"""Reconcile one immutable batch and optionally validate its exact artifacts."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from background import decode_metadata
from bilibili_dataset import validate_manifest as validate_bilibili_manifest
from youtube_dataset import validate_manifest as validate_youtube_manifest

CATEGORIES = ("影视", "访谈", "会议论坛")
KINDS = {
    "影视": "screen_media",
    "访谈": "interview_roundtable",
    "会议论坛": "conference_forum",
}
SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")
PARTIAL_SUFFIXES = (".part", ".tmp", ".ytdl")


def _open_readonly_database(path: Path) -> sqlite3.Connection:
    """Open the live SQLite database without permitting accidental writes."""

    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise ValueError(f"database does not exist: {resolved}")
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    connection.row_factory = sqlite3.Row
    return connection


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
        raise ValueError(f"missing or oversized JSON file: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _inside(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ValueError("manifest path must be a non-empty string")
    target = (root / relative).resolve()
    if os.path.commonpath((str(root.resolve()), str(target))) != str(root.resolve()):
        raise ValueError("manifest path escapes repository root")
    return target


def load_and_validate_batch(index_path: Path) -> dict[str, Any]:
    index_path = Path(index_path).resolve()
    root = index_path.parent.parent if index_path.parent.name == "config" else index_path.parent
    document = _read_json(index_path)
    batch_id = str(document.get("batch_id") or "")
    manifests = document.get("manifests")
    targets = document.get("target_counts")
    if not batch_id or not isinstance(manifests, dict) or not isinstance(targets, dict):
        raise ValueError("batch index lacks batch_id, manifests, or target_counts")

    normalized: dict[str, list[dict[str, Any]]] = {}
    validators = {
        "bilibili": validate_bilibili_manifest,
        "youtube": validate_youtube_manifest,
    }
    for source, validator in validators.items():
        path = _inside(root, manifests.get(source, ""))
        raw = _read_json(path)
        if raw.get("batch_id") != batch_id:
            raise ValueError(f"{source} manifest batch_id does not match index")
        items = validator(raw)
        expected = targets.get(source)
        if not isinstance(expected, dict):
            raise ValueError(f"missing target counts for {source}")
        category_counts = Counter(item.get("dataset_category") for item in items)
        for category in CATEGORIES:
            if category_counts[category] != int(expected.get(category, -1)):
                raise ValueError(
                    f"{source} {category} quota mismatch: "
                    f"{category_counts[category]} != {expected.get(category)}"
                )
        if len(items) != int(expected.get("total", -1)):
            raise ValueError(f"{source} total quota mismatch")
        for item in items:
            if item.get("batch_id") != batch_id:
                raise ValueError(f"{source} item lacks matching batch_id")
            if item.get("source_revision") != batch_id:
                raise ValueError(f"{source} item lacks matching source_revision")
            if KINDS[item["dataset_category"]] != item.get("content_kind"):
                raise ValueError(f"{source} item has inconsistent classification")
            if item.get("speaker_count") is None and item.get("speaker_count_status") != "needs_review":
                raise ValueError(f"{source} unverified speaker count is mislabeled")
        normalized[source] = items

    bilibili_ids = [item["bvid"] for item in normalized["bilibili"]]
    youtube_ids = [item["video_id"] for item in normalized["youtube"]]
    if len(bilibili_ids) != len(set(bilibili_ids)):
        raise ValueError("Bilibili manifest contains duplicate parent BV ids")
    if len(youtube_ids) != len(set(youtube_ids)):
        raise ValueError("YouTube manifest contains duplicate video ids")
    if sum(map(len, normalized.values())) != int(targets.get("total", -1)):
        raise ValueError("cross-platform total quota mismatch")
    baseline_report = {"status": "not_configured"}
    snapshot = document.get("baseline_snapshot")
    if isinstance(snapshot, dict) and snapshot.get("database"):
        baseline_path = _inside(root, snapshot["database"])
        if baseline_path.is_file():
            connection = _open_readonly_database(baseline_path)
            baseline_report = {"status": "verified", "database": str(baseline_path)}
            for source, id_field in (("bilibili", "bvid"), ("youtube", "video_id")):
                ids = []
                for (source_id,) in connection.execute(
                    "SELECT source_id FROM audio_urls WHERE source=? "
                    "AND artifact_kind='video_bundle' AND source_id!=''", (source,),
                ):
                    if source == "bilibili":
                        source_id = source_id.split("_p", 1)[0]
                    ids.append(source_id)
                ids = sorted(set(ids))
                digest = hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()
                declared = snapshot.get(source) or {}
                if len(ids) != declared.get("parent_count") or digest != declared.get("sha256"):
                    raise ValueError(f"{source} baseline snapshot identity mismatch")
                overlap = sorted({item[id_field] for item in normalized[source]} & set(ids))
                if overlap:
                    raise ValueError(f"{source} new manifest overlaps baseline: {overlap}")
                baseline_report[source] = {"parent_count": len(ids), "sha256": digest}
            connection.close()
        else:
            baseline_report = {"status": "unavailable", "database": str(baseline_path)}
    return {
        "root": root, "batch_id": batch_id, "document": document,
        "items": normalized, "baseline": baseline_report,
    }


def _row_batch_id(row: sqlite3.Row) -> str:
    try:
        metadata = decode_metadata(row["metadata_json"])
        source_data = metadata.get("source_data", {}).get(row["source"], {})
        task = source_data.get("download_task") if row["source"] == "bilibili" else source_data.get("job")
        return str((task or {}).get("batch_id") or "")
    except Exception:
        return ""


def audit_database(batch: dict[str, Any], db_path: Path) -> dict[str, Any]:
    connection = _open_readonly_database(db_path)
    try:
        rows = list(connection.execute(
            "SELECT source,source_id,job_key,category,status,bundle_path,metadata_json "
            "FROM audio_urls WHERE artifact_kind='video_bundle' "
            "AND source IN ('bilibili','youtube')"
        ))
    finally:
        connection.close()

    expected = {
        "bilibili": {item["bvid"]: item for item in batch["items"]["bilibili"]},
        "youtube": {item["video_id"]: item for item in batch["items"]["youtube"]},
    }
    matched: dict[str, list[sqlite3.Row]] = defaultdict(list)
    extras = []
    for row in rows:
        if _row_batch_id(row) != batch["batch_id"]:
            continue
        identity = row["source_id"].split("_p", 1)[0] if row["source"] == "bilibili" else row["source_id"]
        if identity not in expected[row["source"]]:
            extras.append({"source": row["source"], "source_id": row["source_id"]})
            continue
        matched[f"{row['source']}:{identity}"].append(row)

    cells = {}
    missing = []
    for source, items in expected.items():
        for category in CATEGORIES:
            selected = {identity for identity, item in items.items() if item["dataset_category"] == category}
            present = {
                identity for identity in selected
                if matched.get(f"{source}:{identity}")
            }
            done = {
                identity for identity in selected
                if matched.get(f"{source}:{identity}")
                and all(row["status"] == "done" for row in matched[f"{source}:{identity}"])
            }
            cells[f"{source}:{category}"] = {
                "expected_parents": len(selected),
                "queued_parents": len(present),
                "done_parents": len(done),
                "missing_parents": len(selected - present),
                "row_statuses": dict(sorted(Counter(
                    row["status"] for identity in selected
                    for row in matched.get(f"{source}:{identity}", [])
                ).items())),
            }
            missing.extend(f"{source}:{identity}" for identity in sorted(selected - present))

    expected_total = sum(len(items) for items in expected.values())
    done_total = sum(cell["done_parents"] for cell in cells.values())
    return {
        "batch_id": batch["batch_id"],
        "baseline": batch.get("baseline", {"status": "unknown"}),
        "expected_parents": expected_total,
        "queued_parents": sum(cell["queued_parents"] for cell in cells.values()),
        "done_parents": done_total,
        "complete": done_total == expected_total and not missing and not extras,
        "cells": cells,
        "missing": missing,
        "extras": extras,
    }


def _expected_items(batch: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    return {
        "bilibili": {item["bvid"]: item for item in batch["items"]["bilibili"]},
        "youtube": {item["video_id"]: item for item in batch["items"]["youtube"]},
    }


def _row_parent_identity(row: sqlite3.Row) -> str:
    source_id = str(row["source_id"] or "")
    return source_id.split("_p", 1)[0] if row["source"] == "bilibili" else source_id


def _relative_to_root(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return "[outside-downloads-root]"


def _safe_component(value: Any) -> bool:
    text = str(value or "")
    return bool(SAFE_COMPONENT.fullmatch(text)) and text not in {".", ".."}


def _is_partial(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(PARTIAL_SUFFIXES) or ".part-" in name


def _recorded_path(value: Any) -> Path:
    path = Path(str(value or ""))
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve()


def _artifact_failure(
    row: sqlite3.Row, code: str, error: BaseException | None = None,
) -> dict[str, str]:
    """Return only stable identifiers and error classes, never payload text."""

    result = {
        "source": str(row["source"] or ""),
        "source_id": str(row["source_id"] or ""),
        "job_key": str(row["job_key"] or ""),
        "code": code,
    }
    if error is not None:
        result["error_type"] = type(error).__name__
    return result


def _increment(counter: Counter[str], value: Any, fallback: str = "unknown") -> None:
    text = str(value or fallback)
    counter[text] += 1


def _summarize_metadata(
    summary: dict[str, Counter[str]], source: str, category: str, metadata: dict[str, Any],
) -> None:
    _increment(summary["source_rows"], source)
    _increment(summary["category_rows"], category)
    caption = metadata.get("caption") if isinstance(metadata.get("caption"), dict) else {}
    _increment(summary["caption_status"], caption.get("status"), "unknown")
    if source == "bilibili":
        tracks = caption.get("tracks") if isinstance(caption.get("tracks"), list) else []
        for track in tracks:
            if isinstance(track, dict):
                _increment(summary["caption_track_kind"], track.get("kind"))
                _increment(summary["caption_text_source"], track.get("text_source"))
        visual = ((metadata.get("derived_text") or {}).get("visual_ocr") or {})
        _increment(summary["visual_ocr_status"], visual.get("status"), "not_run")
    else:
        if caption.get("kind"):
            _increment(summary["caption_track_kind"], caption.get("kind"))
        if caption.get("text_source"):
            _increment(summary["caption_text_source"], caption.get("text_source"))
    rights = metadata.get("rights") if isinstance(metadata.get("rights"), dict) else {}
    ai_generation = (
        metadata.get("ai_generation")
        if isinstance(metadata.get("ai_generation"), dict) else {}
    )
    _increment(summary["rights_status"], rights.get("status"))
    _increment(summary["ai_generation_status"], ai_generation.get("status"))
    _increment(summary["speaker_count_status"], metadata.get("speaker_count_status"))
    _increment(
        summary["speaker_count_values"],
        "unknown" if metadata.get("speaker_count") is None else metadata.get("speaker_count"),
    )


def audit_artifacts(
    batch: dict[str, Any], db_path: Path, downloads: Path, *,
    database_report: dict[str, Any] | None = None,
    validators: dict[str, Callable[[Path], dict[str, Any]]] | None = None,
    fingerprint: Callable[[Path], tuple[int, str]] | None = None,
) -> dict[str, Any]:
    """Validate only this batch's persisted bundles and unfinished staging."""

    if validators is None or fingerprint is None:
        from bilibili_dataset import validate_bundle as validate_bilibili_bundle
        from media_artifacts import bundle_fingerprint
        from youtube_dataset import _validate_bundle as validate_youtube_bundle

        validators = validators or {
            "bilibili": validate_bilibili_bundle,
            "youtube": validate_youtube_bundle,
        }
        fingerprint = fingerprint or bundle_fingerprint

    root = Path(downloads)
    if not root.is_absolute():
        root = REPO_ROOT / root
    root = root.resolve()
    expected = _expected_items(batch)
    expected_parent_count = sum(len(items) for items in expected.values())
    connection = _open_readonly_database(db_path)
    try:
        rows = list(connection.execute(
            "SELECT source,source_id,job_key,category,status,bundle_path,local_path,"
            "content_hash,metadata_json FROM audio_urls "
            "WHERE artifact_kind='video_bundle' AND source IN ('bilibili','youtube')"
        ))
    finally:
        connection.close()
    batch_rows = [row for row in rows if _row_batch_id(row) == batch["batch_id"]]

    failures: list[dict[str, str]] = []
    staging: set[str] = set()
    partials: set[str] = set()
    audited_rows = 0
    done_rows = 0
    total_bytes = 0
    total_duration = 0.0
    seen_jobs: set[tuple[str, str]] = set()
    present_parents: set[tuple[str, str]] = set()
    summary = {
        name: Counter() for name in (
            "source_rows", "category_rows", "caption_status", "caption_track_kind",
            "caption_text_source", "visual_ocr_status", "rights_status",
            "ai_generation_status", "speaker_count_status", "speaker_count_values",
        )
    }

    for row in batch_rows:
        source = str(row["source"] or "")
        source_id = str(row["source_id"] or "")
        job_key = str(row["job_key"] or "")
        category = str(row["category"] or "")
        identity = _row_parent_identity(row)
        if category not in CATEGORIES:
            failures.append(_artifact_failure(row, "category_mismatch"))
            continue
        if not _safe_component(source_id) or not _safe_component(job_key):
            failures.append(_artifact_failure(row, "unsafe_storage_identity"))
            continue
        job_identity = (source, job_key)
        if job_identity in seen_jobs:
            failures.append(_artifact_failure(row, "duplicate_job_key"))
            continue
        seen_jobs.add(job_identity)

        expected_bundle = (root / source / category / source_id / job_key).resolve()
        stage = (root / source / category / ".staging" / job_key).resolve()
        if stage.exists():
            staging.add(_relative_to_root(stage, root))
            if stage.is_dir() and not stage.is_symlink():
                for candidate in stage.rglob("*"):
                    if candidate.is_file() and _is_partial(candidate):
                        partials.add(_relative_to_root(candidate, root))

        item = expected.get(source, {}).get(identity)
        if item is None:
            failures.append(_artifact_failure(row, "unexpected_batch_identity"))
            continue
        present_parents.add((source, identity))
        if category != item.get("dataset_category"):
            failures.append(_artifact_failure(row, "category_mismatch"))
            continue

        if str(row["status"] or "") != "done":
            failures.append(_artifact_failure(row, "row_not_done"))
            continue
        done_rows += 1
        try:
            bundle = _recorded_path(row["bundle_path"])
            local_path = _recorded_path(row["local_path"])
            if bundle != expected_bundle or root not in bundle.parents:
                raise ValueError("bundle_path_mismatch")
            if bundle.is_symlink() or not bundle.is_dir():
                raise ValueError("bundle_missing_or_linked")
            sidecar = bundle / "metadata.json"
            if sidecar.is_symlink() or not sidecar.is_file():
                raise ValueError("sidecar_missing_or_linked")
            if local_path != bundle / "source.mp4" or local_path.is_symlink() or not local_path.is_file():
                raise ValueError("local_path_mismatch")
            for candidate in bundle.rglob("*"):
                if candidate.is_file() and _is_partial(candidate):
                    partials.add(_relative_to_root(candidate, root))

            validated = validators[source](sidecar)
            metadata = validated.get("metadata")
            if not isinstance(metadata, dict):
                raise ValueError("validator_missing_metadata")
            if (
                metadata.get("source") != source
                or metadata.get("source_id") != source_id
                or metadata.get("job_key") != job_key
                or (
                    source == "youtube"
                    and metadata.get("dataset_category") != category
                )
            ):
                raise ValueError("sidecar_identity_mismatch")
            observed_bytes, observed_hash = fingerprint(bundle)
            if observed_hash != str(row["content_hash"] or ""):
                raise ValueError("database_fingerprint_mismatch")
            duration = (
                validated.get("video_duration")
                if source == "youtube"
                else (validated.get("video") or {}).get("duration_seconds")
            )
            duration = float(duration or 0)
            if duration <= 0:
                raise ValueError("validator_missing_duration")
            audited_rows += 1
            total_bytes += int(observed_bytes)
            total_duration += duration
            _summarize_metadata(summary, source, category, metadata)
        except Exception as exc:
            code = str(exc) if str(exc) in {
                "bundle_path_mismatch", "bundle_missing_or_linked",
                "sidecar_missing_or_linked", "local_path_mismatch",
                "validator_missing_metadata", "sidecar_identity_mismatch",
                "database_fingerprint_mismatch", "validator_missing_duration",
            } else "bundle_validation_failed"
            failures.append(_artifact_failure(row, code, exc))

    artifact_report = {
        "schema_version": 1,
        "downloads": str(root),
        "batch_rows": len(batch_rows),
        "done_rows": done_rows,
        "audited_rows": audited_rows,
        "total_bytes": total_bytes,
        "total_duration_seconds": round(total_duration, 3),
        "summary": {
            name: dict(sorted(counter.items())) for name, counter in summary.items()
        },
        "staging_count": len(staging),
        "staging": sorted(staging),
        "partial_count": len(partials),
        "partials": sorted(partials),
        "failure_count": len(failures),
        "failures": failures,
    }
    database_complete = bool(database_report and database_report.get("complete"))
    artifact_report["complete"] = bool(
        database_complete
        and len(present_parents) == expected_parent_count
        and done_rows == len(batch_rows)
        and audited_rows == len(batch_rows)
        and not failures
        and not staging
        and not partials
    )
    return artifact_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-index", default="config/multispeaker_video_100_20260912.batch.json")
    parser.add_argument("--db", default="audiospider.db")
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument(
        "--artifacts", action="store_true",
        help="also validate this exact batch's bundle, sidecar, hashes and staging",
    )
    parser.add_argument(
        "--downloads", default="downloads",
        help="unified downloads root used with --artifacts (default: downloads)",
    )
    args = parser.parse_args(argv)
    batch = load_and_validate_batch(Path(args.batch_index))
    report = audit_database(batch, Path(args.db))
    if args.artifacts:
        report["artifacts"] = audit_artifacts(
            batch, Path(args.db), Path(args.downloads), database_report=report,
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    failed = args.require_complete and not report["complete"]
    if args.artifacts and not report["artifacts"]["complete"]:
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
