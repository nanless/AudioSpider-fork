"""Artifact-specific workers behind the shared SQLite download queue.

This module is deliberately not a second queue or user-facing acquisition
pipeline.  ``Downloader`` calls it only after ``Storage.claim_pending`` has
claimed a ``video_bundle`` row.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
import queue
import re
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp

from background import decode_metadata
from bilibili_proxy import get_bilibili_proxy
from bilibili_dataset import (
    BilibiliClient,
    build_jobs as build_bilibili_jobs,
    download_job as download_bilibili_job,
    validate_bundle as validate_bilibili_bundle,
    validate_manifest as validate_bilibili_manifest,
)
from youtube_dataset import (
    _validate_bundle as validate_youtube_bundle,
    download_item as download_youtube_item,
    validate_manifest as validate_youtube_manifest,
)
from youtube_auth import redact_cookie_values


SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")
YOUTUBE_DOWNLOAD_TIMEOUT = int(os.environ.get("AUDIOSPIDER_YOUTUBE_DOWNLOAD_TIMEOUT", "14400"))
if YOUTUBE_DOWNLOAD_TIMEOUT <= 0:
    raise ValueError("AUDIOSPIDER_YOUTUBE_DOWNLOAD_TIMEOUT must be positive")
BILIBILI_JOB_ATTEMPTS = int(os.environ.get("AUDIOSPIDER_BILIBILI_JOB_ATTEMPTS", "3"))
BILIBILI_RETRY_BACKOFF_SECONDS = float(
    os.environ.get("AUDIOSPIDER_BILIBILI_RETRY_BACKOFF_SECONDS", "10")
)
if not 1 <= BILIBILI_JOB_ATTEMPTS <= 10:
    raise ValueError("AUDIOSPIDER_BILIBILI_JOB_ATTEMPTS must be between 1 and 10")
if not 0 <= BILIBILI_RETRY_BACKOFF_SECONDS <= 600:
    raise ValueError("AUDIOSPIDER_BILIBILI_RETRY_BACKOFF_SECONDS must be in [0, 600]")


def _safe_component(value: str, label: str) -> str:
    if not isinstance(value, str) or not SAFE_COMPONENT.fullmatch(value):
        raise ValueError(f"unsafe {label}: {value!r}")
    if value in {".", ".."}:
        raise ValueError(f"unsafe {label}: {value!r}")
    return value


def _source_data(item: dict[str, Any], source: str) -> dict[str, Any]:
    envelope = decode_metadata(item.get("metadata_json", ""))
    data = (envelope.get("source_data") or {}).get(source)
    if not isinstance(data, dict):
        raise ValueError(f"{source} video task is missing source_data")
    return data


def bundle_fingerprint(bundle: Path) -> tuple[int, str]:
    """Return total payload bytes and a stable closure hash for a bundle."""

    total = 0
    digest = hashlib.sha256()
    paths = sorted(
        path for path in bundle.rglob("*")
        if path.is_file() and path.name != "failure.json"
    )
    for path in paths:
        relative = path.relative_to(bundle).as_posix()
        file_hash = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                file_hash.update(chunk)
        size = path.stat().st_size
        total += size
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(file_hash.hexdigest().encode("ascii"))
        digest.update(b"\n")
    if not paths:
        raise ValueError("video bundle has no files")
    return total, digest.hexdigest()


def _result(bundle: Path, *, reused: bool) -> dict[str, Any]:
    primary = bundle / "source.mp4"
    sidecar = bundle / "metadata.json"
    if not primary.is_file() or not sidecar.is_file():
        raise ValueError("video bundle lacks source.mp4 or metadata.json")
    total, bundle_hash = bundle_fingerprint(bundle)
    return {
        "bundle_path": str(bundle),
        "local_path": str(primary),
        "file_format": "mp4",
        "file_size": total,
        "content_hash": bundle_hash,
        "reused": reused,
    }


def _youtube_download_worker(
    job: dict, output_root: str, destination: str,
    auth_cookies: list[dict[str, Any]], result_queue,
) -> None:
    saved_stdout = saved_stderr = None
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    sink = None
    try:
        for name in (
            "BILIBILI_COOKIE", "YOUTUBE_COOKIE_JSON", "AUDIOSPIDER_BILIBILI_PROXY",
            "PODCAST_INDEX_KEY", "PODCAST_INDEX_SECRET",
        ):
            os.environ.pop(name, None)
        # Third-party extractors and PO-token plugins must not be able to write
        # authenticated diagnostics directly to inherited logs.  The parent
        # receives only the explicitly redacted queue result below.
        if auth_cookies:
            original_stdout.flush()
            original_stderr.flush()
            sink = open(os.devnull, "w", encoding="utf-8")
            saved_stdout = os.dup(1)
            saved_stderr = os.dup(2)
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
            sys.stdout = sink
            sys.stderr = sink
        bundle = download_youtube_item(
            job, Path(output_root), destination=Path(destination),
            auth_cookies=auth_cookies,
        )
        result_queue.put(("ok", str(bundle)))
    except BaseException as exc:
        safe_error = redact_cookie_values(str(exc), auth_cookies)
        result_queue.put(("error", type(exc).__name__, safe_error[:2000]))
    finally:
        if sink is not None:
            # Logging handlers may retain the original TextIOWrapper. Flush
            # every Python buffer while fd 1/2 still point at /dev/null.
            sink.flush()
            original_stdout.flush()
            original_stderr.flush()
        if saved_stdout is not None:
            os.dup2(saved_stdout, 1)
            os.close(saved_stdout)
        if saved_stderr is not None:
            os.dup2(saved_stderr, 2)
            os.close(saved_stderr)
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        if sink is not None:
            sink.close()


def _stop_youtube_worker(process) -> None:
    """Always reap a credential-bearing worker, escalating to kill if needed."""

    if not process.is_alive():
        process.join(timeout=0)
        return
    process.terminate()
    process.join(timeout=10)
    if process.is_alive():
        process.kill()
        process.join(timeout=5)
    if process.is_alive():
        raise RuntimeError("YouTube download worker could not be stopped")


def _run_youtube_download_bounded(
    job: dict, output_root: Path, destination: Path,
    timeout_seconds: int = YOUTUBE_DOWNLOAD_TIMEOUT,
    *, auth_cookies: list[dict[str, Any]] | None = None,
) -> Path:
    """Run yt-dlp in a killable process; partial staging remains resumable."""

    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue(maxsize=1)
    process = context.Process(
        target=_youtube_download_worker,
        args=(job, str(output_root), str(destination), auth_cookies or [], result_queue),
    )
    started = False
    try:
        process.start()
        started = True
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"YouTube download exceeded {timeout_seconds}s hard limit"
                )
            try:
                result = result_queue.get(timeout=min(0.5, remaining))
                break
            except queue.Empty:
                if process.is_alive():
                    continue
                process.join(timeout=1)
                try:
                    result = result_queue.get_nowait()
                    break
                except queue.Empty as exc:
                    raise RuntimeError(
                        f"YouTube download worker exited without a result "
                        f"(exitcode={process.exitcode})"
                    ) from exc
        process.join(timeout=10)
        if result[0] != "ok":
            raise RuntimeError(f"YouTube download failed: {result[1]}: {result[2]}")
        return Path(result[1])
    finally:
        try:
            if started:
                _stop_youtube_worker(process)
        finally:
            result_queue.close()
            result_queue.join_thread()


async def _download_youtube(
    item: dict[str, Any], output_root: Path,
    *, auth_cookies: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    data = _source_data(item, "youtube")
    raw_job = data.get("job")
    if not isinstance(raw_job, dict):
        raise ValueError("YouTube queue record is missing its validated job")
    job = validate_youtube_manifest({"items": [raw_job]})[0]
    if job["video_id"] != item.get("source_id"):
        raise ValueError("YouTube queue identity does not match manifest job")
    if item.get("job_key") and item["job_key"] != job["job_key"]:
        raise ValueError("YouTube queue job_key does not match manifest job")
    destination = (
        output_root
        / _safe_component(job["video_id"], "video_id")
        / _safe_component(job["job_key"], "job_key")
    )
    reused = (destination / "metadata.json").is_file()
    bundle = await asyncio.to_thread(
        _run_youtube_download_bounded, job, output_root, destination,
        auth_cookies=auth_cookies,
    )
    validate_youtube_bundle(bundle / "metadata.json")
    return _result(bundle, reused=reused)


async def _download_bilibili(
    item: dict[str, Any], session: Any, output_root: Path,
    *, bilibili_auth_cookie: str = "",
) -> dict[str, Any]:
    data = _source_data(item, "bilibili")
    task = data.get("download_task")
    if not isinstance(task, dict):
        raise ValueError("Bilibili queue record is missing download_task")
    caption_policy = task.get("caption_policy") or {}
    raw_manifest = {
        "bvid": task.get("bvid"),
        "parts": task.get("parts") or [task.get("page")],
        "max_parts": task.get("max_parts", 1),
        "max_height": task.get("max_height", 720),
        "max_duration_seconds": task.get("max_duration_seconds", 4 * 3600),
        "languages": caption_policy.get("languages") or [],
        "require_caption": bool(caption_policy.get("require_caption", False)),
        "visual_ocr_fallback": bool(
            caption_policy.get("visual_ocr_fallback", False)
        ),
        "visual_ocr_profile": caption_policy.get("visual_ocr_profile"),
        "content_language": task.get("content_language") or item.get("language") or "zh",
        "program": item.get("title") or "",
        "rights": {"status": (task.get("rights") or {}).get("status", "needs_review")},
        "ai_generation": task.get("ai_generation") or {"status": "unknown", "evidence": []},
        "speaker_count": task.get("speaker_count"),
        "speaker_count_status": task.get("speaker_count_status", "needs_review"),
        "source_revision": task.get("source_revision", "current"),
        "batch_id": task.get("batch_id", ""),
        "content_kind": task.get("content_kind", ""),
        "dataset_category": task.get("dataset_category", ""),
        "selection_slot": task.get("selection_slot", ""),
        "candidate_metadata": task.get("candidate_metadata") or {},
    }
    manifest_item = validate_bilibili_manifest({"items": [raw_manifest]})[0]
    client = BilibiliClient(
        session, auth_cookie=bilibili_auth_cookie, proxy=get_bilibili_proxy()
    )
    view = await client.view(manifest_item["bvid"])
    jobs = build_bilibili_jobs(manifest_item, view)
    if len(jobs) != 1:
        raise ValueError("Bilibili queue row must resolve to exactly one part")
    job = jobs[0]
    stored_cid = task.get("cid")
    if type(stored_cid) is not int or stored_cid <= 0 or job["cid"] != stored_cid:
        raise ValueError(
            "Bilibili source revision changed: stored CID no longer matches current part"
        )
    expected_source_id = f"{job['bvid']}_p{job['part']}"
    if expected_source_id != item.get("source_id"):
        raise ValueError("Bilibili queue identity does not match resolved part")
    if item.get("job_key") and item["job_key"] != job["job_key"]:
        raise ValueError("Bilibili queue job_key does not match current part policy")
    destination = (
        output_root
        / _safe_component(expected_source_id, "source_id")
        / _safe_component(job["job_key"], "job_key")
    )
    reused = (destination / "metadata.json").is_file()
    bundle = await download_bilibili_job(
        client, job, view, output_root, destination=destination
    )
    validate_bilibili_bundle(bundle / "metadata.json")
    return _result(bundle, reused=reused)


async def download_video_bundle(
    item: dict[str, Any], session: Any, output_root: Path,
    *, bilibili_auth_cookie: str = "",
    youtube_auth_cookies: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Materialize one claimed video bundle below a shared source/category root."""

    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    source = item.get("source")
    if source == "youtube":
        return await _download_youtube(
            item, root, auth_cookies=youtube_auth_cookies,
        )
    if source == "bilibili":
        for attempt in range(1, BILIBILI_JOB_ATTEMPTS + 1):
            try:
                return await _download_bilibili(
                    item, session, root,
                    bilibili_auth_cookie=bilibili_auth_cookie,
                )
            except Exception as exc:
                if attempt >= BILIBILI_JOB_ATTEMPTS or not _retryable_bilibili_error(exc):
                    raise
                await asyncio.sleep(BILIBILI_RETRY_BACKOFF_SECONDS * attempt)
    raise ValueError(f"video_bundle is unsupported for source {source!r}")


def _retryable_bilibili_error(exc: Exception) -> bool:
    """Retry only transient transport/rate-limit failures, never policy errors."""
    if isinstance(exc, (asyncio.TimeoutError, aiohttp.ClientError)):
        return True
    if not isinstance(exc, RuntimeError):
        return False
    message = str(exc)
    return any(marker in message for marker in (
        "all DASH CDN candidates failed",
        "Bilibili HTTP 429",
        "Bilibili HTTP 500",
        "Bilibili HTTP 502",
        "Bilibili HTTP 503",
        "Bilibili HTTP 504",
        "code=-412",
    ))
