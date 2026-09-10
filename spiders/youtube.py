# Copyright (c) 2026 Hao Yin. All rights reserved.

"""YouTube manifest adapter for the shared AudioSpider collection queue.

This adapter only discovers and validates jobs.  Media is downloaded later by
``main.py`` from the same SQLite queue used by every other source.
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import queue
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from background import encode_metadata, metadata_envelope, plain_text
from config import BASE_DIR, SPIDER_CONFIGS
from spiders.base import BaseSpider
from storage import AudioRecord
from youtube_dataset import (
    caption_availability_status,
    inspect_item,
    validate_manifest,
)
from youtube_dataset import CaptionSelection


PROFILE_CATEGORY = {
    "youtube_interviews": "访谈",
    "youtube_screen_clips": "影视",
}


def _inspect_worker(item: dict, result_queue) -> None:
    """Child-process entry point so a blocked extractor can be terminated."""

    try:
        for name in ("BILIBILI_COOKIE", "PODCAST_INDEX_KEY", "PODCAST_INDEX_SECRET"):
            os.environ.pop(name, None)
        info, caption = inspect_item(item)
        caption_status = caption_availability_status(
            info, item.get("languages") or [], caption
        )
        public_info = {
            key: info.get(key) for key in (
                "id", "title", "description", "duration", "channel", "uploader",
                "channel_id", "uploader_id", "upload_date",
            )
        }
        public_info["caption_status"] = caption_status
        if isinstance(public_info.get("description"), str):
            public_info["description"] = public_info["description"][:100_000]
        result_queue.put(("ok", public_info, asdict(caption) if caption else None))
    except Exception as exc:
        result_queue.put(("error", type(exc).__name__, str(exc)[:2000]))


def _bounded_inspect_item(item: dict, timeout_seconds: int):
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue(maxsize=1)
    process = context.Process(target=_inspect_worker, args=(item, result_queue))
    process.start()
    try:
        try:
            result = result_queue.get(timeout=timeout_seconds)
        except queue.Empty as exc:
            process.terminate()
            process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
            raise TimeoutError(
                f"YouTube inspect exceeded {timeout_seconds}s hard limit"
            ) from exc
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        if result[0] != "ok":
            raise RuntimeError(f"YouTube inspect failed: {result[1]}: {result[2]}")
        caption = CaptionSelection(**result[2]) if result[2] else None
        return result[1], caption
    finally:
        result_queue.close()
        result_queue.join_thread()


class YoutubeSpider(BaseSpider):
    """Register complete YouTube parents with strict or best-effort captions."""

    name = "youtube"

    def __init__(self):
        super().__init__()
        config = SPIDER_CONFIGS.get(self.name, {})
        raw_path = str(config.get("manifest_path") or "config/youtube_sources.initial.json")
        path = Path(raw_path)
        self.manifest_path = path if path.is_absolute() else Path(BASE_DIR) / path
        self.max_items = int(config.get("max_items", 100))
        self.inspect_timeout_seconds = int(config.get("inspect_timeout_seconds", 120))
        if self.max_items <= 0:
            raise ValueError("youtube.max_items must be positive")
        if self.inspect_timeout_seconds <= 0:
            raise ValueError("youtube.inspect_timeout_seconds must be positive")

    def _load_items(self) -> list[dict]:
        if not self.manifest_path.is_file():
            raise FileNotFoundError(f"YouTube manifest not found: {self.manifest_path}")
        raw = self.manifest_path.read_bytes()
        if len(raw) > 10 * 1024 * 1024:
            raise ValueError("YouTube manifest exceeds 10 MiB")
        return validate_manifest(json.loads(raw.decode("utf-8")))[: self.max_items]

    async def crawl(self, on_batch=None) -> list[AudioRecord]:
        records: list[AudioRecord] = []
        items = self._load_items()
        self.logger.info(
            "YouTube 清单发现 %d 个完整母视频任务，逐条核验同语言字幕策略",
            len(items),
        )
        for index, item in enumerate(items, 1):
            try:
                info, caption = await asyncio.to_thread(
                    _bounded_inspect_item, item, self.inspect_timeout_seconds
                )
                record = self._record(item, info, caption)
                if on_batch is None:
                    records.append(record)
                else:
                    on_batch([record])
                self.logger.info(
                    "YouTube [%d/%d] 已入队 %s（字幕=%s）",
                    index, len(items), item["video_id"],
                    "downloadable" if caption else info.get("caption_status", "missing"),
                )
            except Exception as exc:
                self.logger.warning(
                    "YouTube [%d/%d] 拒绝 %s: %s",
                    index, len(items), item.get("video_id", "unknown"), exc,
                )
        return records

    def _record(self, item: dict, info: dict, caption: CaptionSelection | None) -> AudioRecord:
        upload_date = str(info.get("upload_date") or "")
        published_at = ""
        if len(upload_date) == 8 and upload_date.isdigit():
            published_at = datetime.strptime(upload_date, "%Y%m%d").replace(
                tzinfo=timezone.utc
            ).isoformat()
        safe_caption = ({
            "status": "available",
            "required": bool(item.get("require_caption", True)),
            **{key: value for key, value in asdict(caption).items() if key != "url"},
        } if caption else {
            "status": str(info.get("caption_status") or "missing"),
            "required": bool(item.get("require_caption", True)),
            "kind": None,
            "text_source": None,
            "track_language": None,
        })
        metadata = metadata_envelope(
            "youtube",
            common={
                "description": plain_text(str(info.get("description") or "")),
                "webpage_url": item["url"],
                "author": str(info.get("channel") or info.get("uploader") or ""),
                "categories": [PROFILE_CATEGORY[item["profile"]]],
            },
            source_data={
                "artifact_kind": "video_bundle",
                "job": item,
                "inspection": {
                    "title": str(info.get("title") or ""),
                    "duration_seconds": float(info.get("duration") or 0),
                    "channel": str(info.get("channel") or info.get("uploader") or ""),
                    "channel_id": str(info.get("channel_id") or info.get("uploader_id") or ""),
                    "upload_date": upload_date,
                    "caption": safe_caption,
                },
            },
            transcript_status="provided" if caption else safe_caption["status"],
            text_source=caption.text_source if caption else "none",
        )
        return AudioRecord(
            url=item["url"],
            source=self.name,
            title=str(info.get("title") or item["video_id"]),
            file_format="mp4",
            duration=int(float(info.get("duration") or 0)),
            language=item["content_language"],
            category=PROFILE_CATEGORY[item["profile"]],
            speaker=str(info.get("channel") or info.get("uploader") or ""),
            webpage_url=item["url"],
            description=plain_text(str(info.get("description") or "")),
            author=str(info.get("channel") or info.get("uploader") or ""),
            metadata_json=encode_metadata(metadata),
            artifact_kind="video_bundle",
            job_key=item["job_key"],
            source_id=item["video_id"],
            published_at=published_at,
        )
