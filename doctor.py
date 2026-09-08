#!/usr/bin/env python3
# Copyright (c) 2026 AudioSpider Contributors.

"""AudioSpider 环境与数据目录诊断工具。默认只读，不访问外网。"""

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys


BASE_DIR = Path(__file__).resolve().parent
REQUIRED_PACKAGES = {
    "requests": "requests",
    "aiohttp": "aiohttp",
    "aiofiles": "aiofiles",
    "beautifulsoup4": "beautifulsoup4",
    "lxml": "lxml",
    "fake-useragent": "fake-useragent",
    "brotli": "brotli",
}


def make_result(name: str, status: str, message: str, **details) -> dict:
    return {"name": name, "status": status, "message": message, **details}


def check_python() -> dict:
    version = ".".join(str(part) for part in sys.version_info[:3])
    status = "ok" if sys.version_info >= (3, 10) else "error"
    message = f"Python {version}" if status == "ok" else f"需要 Python 3.10+，当前为 {version}"
    return make_result("python", status, message, executable=sys.executable)


def check_packages() -> dict:
    versions = {}
    missing = []
    for display_name, distribution in REQUIRED_PACKAGES.items():
        try:
            versions[display_name] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            missing.append(display_name)
    if missing:
        return make_result(
            "packages", "error", f"缺少依赖：{', '.join(missing)}",
            versions=versions, missing=missing,
        )
    return make_result("packages", "ok", "Python 依赖完整", versions=versions)


def check_ffmpeg() -> dict:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        return make_result(
            "ffmpeg", "error", "缺少 ffmpeg 或 ffprobe",
            ffmpeg=ffmpeg, ffprobe=ffprobe,
        )
    try:
        probe = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return make_result(
            "ffmpeg", "error", f"执行 ffmpeg 失败：{exc}",
            ffmpeg=ffmpeg, ffprobe=ffprobe,
        )
    if probe.returncode != 0 or "libopus" not in probe.stdout:
        return make_result(
            "ffmpeg", "error", "ffmpeg 可执行，但没有检测到 libopus 编码器",
            ffmpeg=ffmpeg, ffprobe=ffprobe,
        )
    version = subprocess.run(
        [ffmpeg, "-version"], capture_output=True, text=True, timeout=10,
    )
    first_line = version.stdout.splitlines()[0] if version.stdout else "ffmpeg 可用"
    return make_result(
        "ffmpeg", "ok", first_line, ffmpeg=ffmpeg, ffprobe=ffprobe,
        libopus=True,
    )


def check_directories(download_dir: Path) -> dict:
    parent = nearest_existing_path(download_dir)
    writable = os.access(parent, os.W_OK)
    status = "ok" if writable else "error"
    message = f"数据目录可写：{parent}" if writable else f"数据目录不可写：{parent}"
    return make_result(
        "directories", status, message,
        download_dir=str(download_dir), parent_exists=parent.exists(), writable=writable,
    )


def nearest_existing_path(path: Path) -> Path:
    candidate = path.resolve()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def check_disk(download_dir: Path) -> dict:
    target = nearest_existing_path(download_dir)
    usage = shutil.disk_usage(target)
    raw_reserve = os.environ.get(
        "AUDIOSPIDER_MIN_DISK_FREE_BYTES", str(20 * 1024 * 1024 * 1024),
    )
    try:
        reserve = int(raw_reserve)
        if reserve <= 0:
            raise ValueError
    except ValueError:
        return make_result(
            "disk", "error",
            "AUDIOSPIDER_MIN_DISK_FREE_BYTES 必须是正整数",
            configured_value=raw_reserve,
        )
    status = "ok" if usage.free >= reserve else "error"
    message = f"可用空间 {usage.free / 1024**3:.1f} GiB"
    if status == "error":
        message += f"，低于安全水位 {reserve / 1024**3:.1f} GiB"
    return make_result(
        "disk", status, message,
        total_bytes=usage.total, free_bytes=usage.free, reserve_bytes=reserve,
    )


def check_database(db_path: Path) -> dict:
    if not db_path.exists():
        return make_result(
            "database", "warn", f"数据库尚未创建：{db_path}", exists=False,
        )
    try:
        uri = db_path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        required = {"audio_urls", "crawl_checkpoints"}
        missing = sorted(required - tables)
        counts = {}
        if "audio_urls" in tables:
            counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM audio_urls GROUP BY status"
                )
            }
        connection.close()
        if integrity != "ok" or missing:
            return make_result(
                "database", "error", "数据库结构或完整性异常",
                exists=True, integrity=integrity, missing_tables=missing,
                status_counts=counts,
            )
        return make_result(
            "database", "ok", f"SQLite 完整，记录数 {sum(counts.values())}",
            exists=True, integrity=integrity, status_counts=counts,
        )
    except sqlite3.Error as exc:
        return make_result(
            "database", "error", f"无法只读打开数据库：{exc}", exists=True,
        )


def collect_checks(db_path: Path, download_dir: Path) -> list[dict]:
    return [
        check_python(),
        check_packages(),
        check_ffmpeg(),
        check_directories(download_dir),
        check_disk(download_dir),
        check_database(db_path),
    ]


def print_human(checks: list[dict]) -> None:
    icons = {"ok": "通过", "warn": "提醒", "error": "失败"}
    print("\nAudioSpider 环境诊断\n" + "=" * 60)
    for item in checks:
        print(f"[{icons[item['status']]}] {item['name']}: {item['message']}")
    print("=" * 60)
    errors = sum(item["status"] == "error" for item in checks)
    warnings = sum(item["status"] == "warn" for item in checks)
    print(f"结论：{errors} 个失败，{warnings} 个提醒\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="检查 AudioSpider 的 Python、依赖、ffmpeg、磁盘和数据库",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--strict", action="store_true", help="将提醒也视为失败")
    parser.add_argument("--db", type=Path, default=BASE_DIR / "audiospider.db")
    parser.add_argument("--download-dir", type=Path, default=BASE_DIR / "downloads")
    args = parser.parse_args()

    checks = collect_checks(args.db, args.download_dir)
    if args.json:
        print(json.dumps({"checks": checks}, ensure_ascii=False, indent=2))
    else:
        print_human(checks)
    failed = any(item["status"] == "error" for item in checks)
    warned = any(item["status"] == "warn" for item in checks)
    return 1 if failed or (args.strict and warned) else 0


if __name__ == "__main__":
    raise SystemExit(main())
