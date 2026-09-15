#!/usr/bin/env python3
"""Fail closed if an authorized YouTube cookie value reached persisted data."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import DB_PATH  # noqa: E402
from youtube_auth import cookie_secret_variants, read_cookie_payload  # noqa: E402


FINAL_BINARY_MEDIA_SUFFIXES = {
    ".aac", ".avi", ".flac", ".gif", ".jpeg", ".jpg", ".m4a",
    ".m4v", ".mkv", ".mov", ".mp3", ".mp4", ".ogg", ".opus",
    ".png", ".webm", ".webp", ".wav",
}
SCAN_CHUNK_BYTES = 1024 * 1024
MAX_SIDECAR_BYTES = 16 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _secret_variants(cookies: list[dict]) -> list[bytes]:
    """Return common reversible encodings without ever reporting their values."""

    return [variant.encode("utf-8") for variant in cookie_secret_variants(cookies)]


def _stream_contains(path: Path, needles: list[bytes]) -> bool:
    overlap = max((len(needle) for needle in needles), default=1) - 1
    tail = b""
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(SCAN_CHUNK_BYTES)
            if not chunk:
                return False
            payload = tail + chunk
            if any(needle in payload for needle in needles):
                return True
            tail = payload[-overlap:] if overlap else b""


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _safe_path_label(relative: str, needles: list[bytes]) -> str:
    encoded = relative.encode("utf-8", errors="replace")
    if any(needle in encoded for needle in needles):
        digest = base64.urlsafe_b64encode(
            hashlib.sha256(encoded).digest()[:9]
        ).decode("ascii").rstrip("=")
        return f"[redacted-path:{digest}]"
    return relative


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(SCAN_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _media_suffix(path: Path) -> str:
    suffixes = [suffix.lower() for suffix in path.suffixes]
    if len(suffixes) >= 2 and suffixes[-1] == ".part":
        return suffixes[-2]
    return suffixes[-1] if suffixes else ""


def _has_binary_media_signature(path: Path, media_suffix: str) -> bool:
    """Conservatively recognize a media container before excluding its bytes."""

    with path.open("rb") as stream:
        header = stream.read(16)
    if media_suffix in {".mp4", ".m4a", ".m4v", ".mov"}:
        return len(header) >= 8 and header[4:8] == b"ftyp"
    if media_suffix == ".wav":
        return header.startswith(b"RIFF") and header[8:12] == b"WAVE"
    if media_suffix == ".avi":
        return header.startswith(b"RIFF") and header[8:11] == b"AVI"
    if media_suffix == ".flac":
        return header.startswith(b"fLaC")
    if media_suffix in {".ogg", ".opus"}:
        return header.startswith(b"OggS")
    if media_suffix in {".webm", ".mkv"}:
        return header.startswith(b"\x1aE\xdf\xa3")
    if media_suffix == ".mp3":
        return header.startswith(b"ID3") or (
            len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0
        )
    if media_suffix == ".aac":
        return len(header) >= 2 and header[0] == 0xFF and header[1] & 0xF6 == 0xF0
    if media_suffix == ".png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if media_suffix in {".jpg", ".jpeg"}:
        return header.startswith(b"\xff\xd8\xff")
    if media_suffix == ".gif":
        return header.startswith((b"GIF87a", b"GIF89a"))
    if media_suffix == ".webp":
        return header.startswith(b"RIFF") and header[8:12] == b"WEBP"
    return False


def _validated_declared_binary_media(root: Path) -> set[Path]:
    """Trust only sidecar-declared media whose complete closure validates."""

    trusted: set[Path] = set()
    for sidecar in root.rglob("metadata.json"):
        relative_parts = sidecar.relative_to(root).parts
        if ".git" in relative_parts or sidecar.is_symlink() or not sidecar.is_file():
            continue
        try:
            if sidecar.stat().st_size > MAX_SIDECAR_BYTES:
                continue
            document = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        files = document.get("files") if isinstance(document, dict) else None
        if not isinstance(files, dict):
            continue
        bundle = sidecar.parent.resolve()
        for record in files.values():
            if not isinstance(record, dict) or not isinstance(record.get("path"), str):
                continue
            relative = Path(record["path"])
            if relative.is_absolute() or ".." in relative.parts:
                continue
            candidate = bundle / relative
            if candidate.is_symlink():
                continue
            target = candidate.resolve()
            media_suffix = _media_suffix(target)
            if (
                media_suffix not in FINAL_BINARY_MEDIA_SUFFIXES
                or not target.is_file()
                or (target.parent != bundle and bundle not in target.parents)
            ):
                continue
            expected_bytes = record.get("bytes")
            expected_hash = record.get("sha256")
            if (
                isinstance(expected_bytes, bool)
                or not isinstance(expected_bytes, int)
                or expected_bytes < 0
                or not isinstance(expected_hash, str)
                or not SHA256_RE.fullmatch(expected_hash)
            ):
                continue
            try:
                if (
                    target.stat().st_size == expected_bytes
                    and _has_binary_media_signature(target, media_suffix)
                    and _sha256_file(target) == expected_hash
                ):
                    trusted.add(target)
            except OSError:
                continue
    return trusted


def _sqlite_match_count(path: Path, needles: list[bytes]) -> int:
    matches = 0
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        tables = [
            str(row[0]) for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            if not str(row[0]).startswith("sqlite_")
        ]
        for table in tables:
            query = f"SELECT * FROM {_quote_identifier(table)}"
            for row in connection.execute(query):
                payload = b"\0".join(
                    value if isinstance(value, bytes)
                    else ("" if value is None else str(value)).encode("utf-8", errors="replace")
                    for value in row
                )
                if any(needle in payload for needle in needles):
                    matches += 1
    finally:
        connection.close()
    return matches


def audit(repo_root: Path, db_path: Path, cookies: list[dict]) -> dict:
    root = repo_root.resolve()
    secrets = _secret_variants(cookies)
    trusted_binary_media = _validated_declared_binary_media(root)
    file_matches: list[str] = []
    scan_errors: list[str] = []
    skipped_symlinks = 0
    excluded_binary_files = 0
    scanned_files = 0
    for path in root.rglob("*"):
        relative = str(path.relative_to(root))
        encoded_relative = relative.encode("utf-8", errors="replace")
        path_matches = any(secret in encoded_relative for secret in secrets)
        safe_relative = _safe_path_label(relative, secrets)
        if path_matches and safe_relative not in file_matches:
            file_matches.append(safe_relative)
        if ".git" in path.relative_to(root).parts:
            continue
        if path.is_symlink():
            skipped_symlinks += 1
            continue
        if not path.is_file():
            continue
        # A media-looking suffix is not evidence.  Exclude bytes only after a
        # sidecar record, safe relative path, byte count, SHA-256 and container
        # signature all agree; every unknown/untrusted file is streamed.
        if path.resolve() in trusted_binary_media:
            excluded_binary_files += 1
            continue
        try:
            # Scan bytes even when the path already matched.  This also covers
            # SQLite freelist/slack bytes; the primary DB is inspected logically
            # across every table below as an independent check.
            if _stream_contains(path, secrets) and safe_relative not in file_matches:
                file_matches.append(safe_relative)
        except OSError as exc:
            scan_errors.append(f"{safe_relative}:{type(exc).__name__}")
            continue
        scanned_files += 1

    db_matches = 0
    try:
        db_matches = _sqlite_match_count(db_path, secrets)
    except (OSError, sqlite3.Error) as exc:
        scan_errors.append(f"{db_path.name}:{type(exc).__name__}")

    if scan_errors:
        status = "error"
    elif file_matches or db_matches:
        status = "failed"
    else:
        status = "clean"
    return {
        "schema_version": 2,
        "status": status,
        "scanned_candidate_files": scanned_files,
        "excluded_binary_files": excluded_binary_files,
        "skipped_symlinks": skipped_symlinks,
        "scan_error_count": len(scan_errors),
        "scan_errors": scan_errors,
        "file_match_count": len(file_matches),
        "file_matches": file_matches,
        "database_match_count": db_matches,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--db", type=Path, default=Path(DB_PATH))
    args = parser.parse_args()
    try:
        report = audit(args.repo, args.db, read_cookie_payload(sys.stdin.buffer))
    except Exception as exc:
        print(json.dumps({
            "schema_version": 1,
            "status": "error",
            "error_type": type(exc).__name__,
        }, ensure_ascii=False))
        return 3
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "clean" else (3 if report["status"] == "error" else 2)


if __name__ == "__main__":
    raise SystemExit(main())
