#!/usr/bin/env python3
"""Audit SQLite video_bundle rows against the unified downloads hierarchy."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bilibili_dataset import validate_bundle as validate_bilibili_bundle
from config import DB_PATH, DOWNLOAD_DIR
from media_artifacts import bundle_fingerprint
from youtube_dataset import _validate_bundle as validate_youtube_bundle


def audit(db_path: Path, download_root: Path) -> dict:
    root = download_root.resolve()
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(audio_urls)")}
        required = {"artifact_kind", "job_key", "bundle_path", "content_hash"}
        if not required.issubset(columns):
            raise ValueError(f"database lacks unified columns: {sorted(required - columns)}")
        rows = connection.execute(
            "SELECT * FROM audio_urls WHERE artifact_kind='video_bundle' AND status='done'"
        ).fetchall()
    finally:
        connection.close()

    failures = []
    seen = set()
    caption_counts: Counter[str] = Counter()
    visual_ocr_counts: Counter[str] = Counter()
    for row in rows:
        try:
            bundle = Path(row["bundle_path"]).resolve()
            if root != bundle and root not in bundle.parents:
                raise ValueError("bundle_path escapes downloads root")
            if bundle.name != row["job_key"] or bundle.parent.name != row["source_id"]:
                raise ValueError("bundle path identity does not match SQLite")
            sidecar = bundle / "metadata.json"
            if row["source"] == "youtube":
                validate_youtube_bundle(sidecar)
            elif row["source"] == "bilibili":
                validated = validate_bilibili_bundle(sidecar)
                metadata = validated["metadata"]
                caption_counts[str(metadata["caption"]["status"])] += 1
                visual = ((metadata.get("derived_text") or {}).get("visual_ocr") or {})
                visual_ocr_counts[str(visual.get("status") or "not_run")] += 1
            else:
                raise ValueError(f"unsupported video source: {row['source']}")
            _, fingerprint = bundle_fingerprint(bundle)
            if fingerprint != row["content_hash"]:
                raise ValueError("bundle closure hash differs from SQLite")
            if Path(row["local_path"]).resolve() != bundle / "source.mp4":
                raise ValueError("local_path is not bundle/source.mp4")
            seen.add(str(bundle))
        except Exception as exc:
            failures.append({
                "id": row["id"], "source": row["source"],
                "job_key": row["job_key"], "error": str(exc),
            })

    orphans = []
    for source in ("bilibili", "youtube"):
        source_root = root / source
        if not source_root.exists():
            continue
        for sidecar in source_root.rglob("metadata.json"):
            bundle = str(sidecar.parent.resolve())
            if bundle not in seen:
                orphans.append(bundle)
    return {
        "database": str(db_path.resolve()), "downloads": str(root),
        "done_video_bundles": len(rows), "validated": len(rows) - len(failures),
        "failure_count": len(failures), "failures": failures,
        "orphan_count": len(orphans), "orphans": sorted(orphans),
        "bilibili_platform_caption_status": dict(sorted(caption_counts.items())),
        "bilibili_visual_ocr_status": dict(sorted(visual_ocr_counts.items())),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path(DB_PATH))
    parser.add_argument("--downloads", type=Path, default=Path(DOWNLOAD_DIR))
    args = parser.parse_args(argv)
    report = audit(args.db, args.downloads)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["failure_count"] or report["orphan_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
