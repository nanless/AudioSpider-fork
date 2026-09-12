#!/usr/bin/env python3
"""Validate and reconcile one immutable cross-platform video batch."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any

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
            connection = sqlite3.connect(str(baseline_path))
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
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    rows = list(connection.execute(
        "SELECT source,source_id,job_key,category,status,bundle_path,metadata_json "
        "FROM audio_urls WHERE artifact_kind='video_bundle' AND source IN ('bilibili','youtube')"
    ))
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-index", default="config/multispeaker_video_100_20260912.batch.json")
    parser.add_argument("--db", default="audiospider.db")
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()
    batch = load_and_validate_batch(Path(args.batch_index))
    report = audit_database(batch, Path(args.db))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if args.require_complete and not report["complete"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
