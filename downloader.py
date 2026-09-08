# Copyright (c) 2026 Hao Yin. All rights reserved.

"""批量下载器：连接池复用、并发可调、断点续传、进度条、内容指纹去重、自动格式转换"""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse, unquote

import aiohttp
import aiofiles

from anti_crawler import build_headers, random_delay, get_aio_proxy
from config import (
    CHUNK_SIZE,
    DOWNLOAD_DIR,
    DOWNLOAD_LEASE_SECONDS,
    DOWNLOAD_TIMEOUT,
    MAX_CONCURRENT_DOWNLOADS,
    MAX_DOWNLOAD_BYTES,
    MIN_FREE_DISK_BYTES,
    MAX_RETRIES,
    RETRY_BACKOFF,
)
from network_safety import UnsafeURLError, safe_get
from storage import Storage

logger = logging.getLogger(__name__)


class DownloadValidationError(RuntimeError):
    """Raised when a response cannot be accepted as a bounded audio file."""

REFERER_MAP = {
    "xyzcdn.net": "https://www.xiaoyuzhoufm.com/",
    "xmcdn.com": "https://www.ximalaya.com/",
    "ximalaya.com": "https://www.ximalaya.com/",
    "cos.tx.xmcdn.com": "https://www.ximalaya.com/",
    "archive.org": "https://archive.org/",
    "bilivideo.cn": "https://www.bilibili.com/",
    "bilivideo.com": "https://www.bilibili.com/",
    "akamaized.net": "https://www.bilibili.com/",
}

BILIBILI_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://www.bilibili.com",
}


def safe_filename(url: str, title: str = "", fmt: str = "", source_id: str = "") -> str:
    """Build a single safe path component with a collision-resistant suffix."""
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
    raw = title or unquote(urlparse(url).path.rsplit("/", 1)[-1])
    # Decoding can introduce separators (for example %2F), so normalize both
    # POSIX and Windows separators before accepting any remote-derived text.
    raw = raw.replace("\\", "/").rsplit("/", 1)[-1]
    raw = os.path.splitext(raw)[0] if raw else "audio"
    name = re.sub(r'[^\w\s\-.\u4e00-\u9fff]', '_', raw)
    name = re.sub(r'_+', '_', name).strip('._ ')[:80] or "audio"
    safe_id = re.sub(r'[^\w\-]', '', source_id)[:32] if source_id else ""
    suffix = f"{safe_id}_{url_hash}" if safe_id else url_hash
    extension = re.sub(r"[^a-zA-Z0-9]", "", fmt.lower())[:8] or "mp3"
    return f"{name}_{suffix}.{extension}"


def _safe_join(root: str, filename: str) -> str:
    root_real = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root_real, filename))
    if os.path.commonpath([root_real, target]) != root_real:
        raise ValueError(f"download path escapes root: {filename!r}")
    return target


def _audio_info(filepath: str) -> dict:
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name,sample_rate,channels",
             "-of", "json", filepath],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return {}
        streams = json.loads(proc.stdout).get("streams", [])
        return streams[0] if streams else {}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {}


def _guess_referer(url: str) -> str:
    host = urlparse(url).netloc.lower()
    for domain, ref in REFERER_MAP.items():
        if domain in host:
            return ref
    return urlparse(url).scheme + "://" + urlparse(url).netloc + "/"


async def _resolve_bilibili_url(session: aiohttp.ClientSession, source_id: str) -> str:
    """B站音频流 URL 有时效性，下载时实时获取新的"""
    m = re.match(r"(BV[\w]+)_p(\d+)", source_id)
    if not m:
        return ""
    bvid, page_num = m.group(1), int(m.group(2))

    try:
        async with session.get(
            "https://api.bilibili.com/x/player/pagelist",
            params={"bvid": bvid},
            headers=BILIBILI_HEADERS,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json(content_type=None)
            pages = data.get("data", [])
            if page_num > len(pages):
                return ""
            cid = pages[page_num - 1]["cid"]

        async with session.get(
            "https://api.bilibili.com/x/player/playurl",
            params={"bvid": bvid, "cid": cid, "fnval": 16},
            headers=BILIBILI_HEADERS,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json(content_type=None)
            audios = data.get("data", {}).get("dash", {}).get("audio", [])
            if not audios:
                return ""
            best = max(audios, key=lambda a: a.get("bandwidth", 0))
            return best.get("baseUrl", "") or best.get("base_url", "")
    except Exception as e:
        logger.debug(f"B站URL刷新失败 {source_id}: {e}")
        return ""


TARGET_FORMAT = {
    "ext": ".opus",
    "ffmpeg_args": [
        "-vn", "-ar", "24000", "-ac", "1",
        "-c:a", "libopus", "-b:a", "32k",
    ],
}


def convert_to_target(filepath: str) -> str | None:
    """将音频文件编码为单声道 Opus 32kbps，返回新路径；失败返回 None。"""
    target_ext = TARGET_FORMAT["ext"]
    base = os.path.splitext(filepath)[0]
    tmp_output = f"{base}.tmp_conv.{uuid.uuid4().hex}{target_ext}"
    final_output = base + target_ext

    try:
        cmd = ["ffmpeg", "-y", "-i", filepath, *TARGET_FORMAT["ffmpeg_args"], tmp_output]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

        if (proc.returncode != 0 or not os.path.exists(tmp_output)
                or os.path.getsize(tmp_output) == 0 or not _audio_info(tmp_output)):
            if os.path.exists(tmp_output):
                os.remove(tmp_output)
            return None

        os.replace(tmp_output, final_output)
        if filepath.lower() != final_output.lower() and os.path.exists(filepath):
            os.remove(filepath)
        return final_output

    except Exception as e:
        logger.warning(f"格式转换失败 {filepath}: {e}")
        if os.path.exists(tmp_output):
            os.remove(tmp_output)
        return None


class Downloader:
    def __init__(self, storage: Storage, max_workers: int | None = None,
                 convert: bool = True, *, allow_private_network: bool = False,
                 max_download_bytes: int = MAX_DOWNLOAD_BYTES,
                 min_disk_free_bytes: int = MIN_FREE_DISK_BYTES):
        self.storage = storage
        self.max_workers = max_workers or MAX_CONCURRENT_DOWNLOADS
        if self.max_workers <= 0:
            raise ValueError("max_workers must be positive")
        self.convert = convert
        self.allow_private_network = allow_private_network
        self.max_download_bytes = max_download_bytes
        self.min_disk_free_bytes = min_disk_free_bytes
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        self.semaphore = asyncio.Semaphore(self.max_workers)
        self.stats = {"success": 0, "failed": 0, "skipped": 0, "dup": 0}
        self._connector = None

    async def download_all(
        self,
        limit: int = 50,
        source: str | None = None,
        category: str | None = None,
        language: str | None = None,
        per_source: bool = False,
        per_category: bool = False,
        published_since: str | None = None,
        published_before: str | None = None,
        items: list[dict] | None = None,
    ):
        pending = items if items is not None else self.storage.claim_pending(
            limit=limit,
            worker_id=self.worker_id,
            lease_seconds=DOWNLOAD_LEASE_SECONDS,
            source=source,
            category=category,
            language=language,
            per_source=per_source,
            per_category=per_category,
            published_since=published_since,
            published_before=published_before,
        )
        if not pending:
            logger.info("没有待下载的音频")
            return self.stats

        total = len(pending)
        fmt_hint = "opus" if self.convert else "原始格式"
        logger.info(f"开始下载 {total} 个音频文件 (并发={self.max_workers}, 保存={fmt_hint})...")

        self._connector = aiohttp.TCPConnector(limit=self.max_workers, limit_per_host=3)
        async with aiohttp.ClientSession(connector=self._connector) as session:
            # B站需要先获取 cookie
            has_bilibili = any(item.get("source") == "bilibili" for item in pending)
            if has_bilibili:
                try:
                    async with session.get(
                        "https://www.bilibili.com",
                        headers=BILIBILI_HEADERS,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as response:
                        await response.read()
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    logger.warning(f"B站 cookie 预热失败，将继续下载: {exc}")

            tasks = [self._download_one(session, item, i + 1, total) for i, item in enumerate(pending)]
            await asyncio.gather(*tasks)

        done = self.stats["success"] + self.stats["skipped"] + self.stats["dup"]
        logger.info(
            f"下载完成 — 成功: {self.stats['success']}, 跳过: {self.stats['skipped']}, "
            f"去重: {self.stats['dup']}, 失败: {self.stats['failed']} | 总计: {done}/{total}"
        )
        return self.stats

    async def _download_one(self, session: aiohttp.ClientSession, item: dict,
                             idx: int, total: int):
        async with self.semaphore:
            progress = f"[{idx}/{total}]"
            url = item.get("url", "")
            title = item.get("title", "")
            filename = "unknown"
            filepath = ""
            part_path = ""
            try:
                fmt = item.get("file_format", "")
                source = item.get("source", "")
                source_id = item.get("source_id", "")
                category = item.get("category", "")

                if source == "bilibili" and source_id:
                    fresh_url = await _resolve_bilibili_url(session, source_id)
                    if not fresh_url:
                        raise DownloadValidationError("B站 URL 刷新失败")
                    url = fresh_url
                    await random_delay(0.5, 1.0)

                subdir = self._build_subdir(source, category)
                filename = safe_filename(item["url"], title, fmt, source_id)
                filepath = _safe_join(subdir, filename)

                if os.path.isfile(filepath) and _audio_info(filepath):
                    if self.convert and not filepath.lower().endswith(TARGET_FORMAT["ext"]):
                        converted_path = await asyncio.to_thread(convert_to_target, filepath)
                        if not converted_path:
                            raise DownloadValidationError("已有文件格式转换失败")
                        filepath = converted_path
                        filename = os.path.basename(filepath)
                    content_hash = await asyncio.to_thread(
                        Storage.compute_file_hash, filepath,
                    )
                    actual_format = os.path.splitext(filepath)[1].lstrip(".").lower()
                    duplicate = self.storage.finalize_download(
                        item["url"], filepath, actual_format,
                        os.path.getsize(filepath), content_hash,
                    )
                    if duplicate:
                        os.remove(filepath)
                        self.stats["dup"] += 1
                    else:
                        _save_meta(filepath, item, content_hash)
                        self.stats["skipped"] += 1
                    logger.info(f"{progress} - 复用已验证文件: {filepath}")
                    return

                part_path = filepath + ".part"
                await self._do_download(session, url, part_path)
                if not _audio_info(part_path):
                    if os.path.exists(part_path):
                        os.remove(part_path)
                    raise DownloadValidationError("ffprobe 无法识别下载内容为音频")
                os.replace(part_path, filepath)

                if self.convert:
                    converted_path = await asyncio.to_thread(convert_to_target, filepath)
                    if converted_path:
                        filepath = converted_path
                        filename = os.path.basename(filepath)
                    else:
                        raise DownloadValidationError("格式转换失败")

                if not _audio_info(filepath):
                    raise DownloadValidationError("最终文件无法解码")
                content_hash = await asyncio.to_thread(Storage.compute_file_hash, filepath)
                actual_format = os.path.splitext(filepath)[1].lstrip(".").lower()
                duplicate = self.storage.finalize_download(
                    item["url"], filepath, actual_format,
                    os.path.getsize(filepath), content_hash,
                )
                if duplicate:
                    os.remove(filepath)
                    self.stats["dup"] += 1
                    logger.info(f"{progress} - 内容重复: {title}")
                    return
                self.stats["success"] += 1
                size_mb = os.path.getsize(filepath) / 1024 / 1024
                _save_meta(filepath, item, content_hash)
                logger.info(f"{progress} ✓ {filename} ({size_mb:.1f}MB) → {filepath}")
            except Exception as e:
                if (part_path and isinstance(e, (UnsafeURLError, DownloadValidationError))
                        and os.path.exists(part_path)):
                    os.remove(part_path)
                self.storage.update_status(item["url"], "failed")
                self.stats["failed"] += 1
                parsed = urlparse(url)
                safe_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path[:80]}"
                logger.error(f"{progress} ✗ {filename} | {safe_url} | {e}")

    def _build_subdir(self, source: str, category: str) -> str:
        parts = [DOWNLOAD_DIR]
        if source:
            safe_source = re.sub(r'[^\w\-]', '_', source).strip('._')
            if safe_source:
                parts.append(safe_source)
        if category:
            safe_cat = re.sub(r'[^\w\u4e00-\u9fff]', '_', category).strip('_')
            if safe_cat:
                parts.append(safe_cat)
        subdir = os.path.join(*parts)
        os.makedirs(subdir, exist_ok=True)
        relative = os.path.relpath(subdir, DOWNLOAD_DIR)
        return _safe_join(DOWNLOAD_DIR, relative)

    async def _do_download(self, session: aiohttp.ClientSession, url: str,
                           part_path: str):
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                await self._download_attempt(session, url, part_path)
                await random_delay(0.2, 0.5)
                return
            except (UnsafeURLError, DownloadValidationError):
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                last_error = exc
                if attempt + 1 < MAX_RETRIES:
                    await asyncio.sleep(RETRY_BACKOFF ** attempt)
        raise DownloadValidationError(f"下载重试耗尽: {last_error}")

    async def _download_attempt(self, session: aiohttp.ClientSession, url: str,
                                part_path: str):
        os.makedirs(os.path.dirname(part_path), exist_ok=True)
        if shutil.disk_usage(os.path.dirname(part_path)).free < self.min_disk_free_bytes:
            raise DownloadValidationError("磁盘剩余空间低于安全水位")

        existing_size = os.path.getsize(part_path) if os.path.exists(part_path) else 0
        if existing_size >= self.max_download_bytes:
            raise DownloadValidationError("临时文件已达到单文件大小上限")

        headers = build_headers(_guess_referer(url))
        if existing_size:
            headers["Range"] = f"bytes={existing_size}-"

        async with safe_get(
            session,
            url,
            allow_private=self.allow_private_network,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT),
            proxy=get_aio_proxy(),
        ) as resp:
            if resp.status == 416:
                raise DownloadValidationError("HTTP 416，无法证明本地分片完整")
            if resp.status not in (200, 206):
                raise aiohttp.ClientResponseError(
                    resp.request_info,
                    resp.history,
                    status=resp.status,
                    message=f"HTTP {resp.status}",
                    headers=resp.headers,
                )
            content_type = resp.headers.get("Content-Type", "").lower()
            if content_type.startswith("text/") or "json" in content_type:
                raise DownloadValidationError(f"拒绝非音频响应类型: {content_type}")

            if resp.status == 206:
                expected_prefix = f"bytes {existing_size}-"
                content_range = resp.headers.get("Content-Range", "").lower()
                if not content_range.startswith(expected_prefix):
                    raise DownloadValidationError("Content-Range 与本地分片不一致")
                mode = "ab"
                total = existing_size
            else:
                mode = "wb"
                total = 0

            if (resp.content_length is not None
                    and total + resp.content_length > self.max_download_bytes):
                raise DownloadValidationError("Content-Length 超过单文件大小上限")

            async with aiofiles.open(part_path, mode) as output:
                async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                    total += len(chunk)
                    if total > self.max_download_bytes:
                        raise DownloadValidationError("响应体超过单文件大小上限")
                    await output.write(chunk)
            if total <= 0:
                raise DownloadValidationError("下载内容为空")


def _save_meta(filepath: str, item: dict, content_hash: str = ""):
    """在音频文件旁生成同名 .json 元信息文件"""
    meta_path = os.path.splitext(filepath)[0] + ".json"
    meta = {
        "title": item.get("title", ""),
        "source": item.get("source", ""),
        "source_id": item.get("source_id", ""),
        "original_url": item.get("url", ""),
        "file_format": os.path.splitext(filepath)[1].lstrip(".").lower(),
        "file_size": os.path.getsize(filepath) if os.path.exists(filepath) else 0,
        "duration": item.get("duration", 0),
        "language": item.get("language", ""),
        "category": item.get("category", ""),
        "speaker": item.get("speaker", ""),
        "published_at": item.get("published_at", ""),
        "acquired_at": datetime.now(timezone.utc).isoformat(),
        "content_hash": content_hash or item.get("content_hash", ""),
        "content_hash_algorithm": "sha256",
    }
    try:
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"元信息写入失败 {meta_path}: {e}")


def fix_meta(storage: Storage):
    """为已下载但没有 .json 的音频文件补生成元信息"""
    conn = storage._get_conn()
    rows = conn.execute(
        "SELECT * FROM audio_urls WHERE status='done' AND local_path != '' AND local_path NOT LIKE 'dup:%'"
    ).fetchall()

    fixed = 0
    for r in rows:
        filepath = r["local_path"]
        if not os.path.exists(filepath):
            continue
        meta_path = os.path.splitext(filepath)[0] + ".json"
        if os.path.exists(meta_path):
            continue
        item = dict(r)
        _save_meta(filepath, item)
        fixed += 1

    logger.info(f"补生成元信息: {fixed} 个文件")
    return fixed
