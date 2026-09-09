#!/usr/bin/env python3
"""Audit and deterministically enrich historical AudioSpider metadata."""

import argparse
import asyncio
from collections import Counter
import json
import logging
from pathlib import Path
import re
from urllib.parse import urlsplit

import aiohttp

from anti_crawler import random_delay
from background import decode_metadata
from storage import Storage


SOURCES = ("podcast_rss", "librivox", "xiaoyuzhou", "ximalaya", "bilibili")
_BILIBILI_ID = re.compile(r"^(BV[0-9A-Za-z]+)_p([1-9][0-9]*)$")


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def bilibili_target(source_id: str) -> tuple[str, int] | None:
    match = _BILIBILI_ID.fullmatch(source_id or "")
    return (match.group(1), int(match.group(2))) if match else None


def xiaoyuzhou_podcast_paths(rows: list[dict]) -> list[str]:
    paths = set()
    for row in rows:
        parsed = urlsplit(row.get("url", ""))
        if parsed.hostname != "media.xyzcdn.net":
            continue
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2:
            paths.add(f"/podcast/{parts[0]}")
    return sorted(paths)


def archive_identifiers(rows: list[dict]) -> set[str]:
    identifiers = set()
    for row in rows:
        parts = [part for part in urlsplit(row.get("url", "")).path.split("/") if part]
        if len(parts) >= 2 and parts[0] in {"download", "details"}:
            identifiers.add(parts[1])
    return identifiers


def audit_records(rows: list[dict]) -> dict:
    coverage = Counter()
    transcript_status = Counter()
    by_source: dict[str, dict] = {}
    unresolved_records = []
    for row in rows:
        source = row.get("source", "unknown")
        bucket = by_source.setdefault(source, {
            "coverage": Counter(), "transcript_status": Counter(),
        })
        bucket["coverage"]["total"] += 1
        metadata = decode_metadata(row.get("metadata_json", ""))
        for field in ("description", "author", "cover_url", "webpage_url"):
            if row.get(field):
                coverage[field] += 1
                bucket["coverage"][field] += 1
        if not metadata:
            coverage["unresolved"] += 1
            bucket["coverage"]["unresolved"] += 1
            unresolved_records.append({
                "source": source,
                "source_id": row.get("source_id", ""),
                "title": row.get("title", "")[:160],
            })
            continue
        coverage["metadata"] += 1
        bucket["coverage"]["metadata"] += 1
        source_data = metadata.get("source_data", {})
        has_source_data = any(bool(value) for value in source_data.values())
        filled = sum(bool(row.get(field)) for field in (
            "description", "author", "cover_url", "webpage_url",
        ))
        if has_source_data and filled >= 3:
            coverage["rich"] += 1
            bucket["coverage"]["rich"] += 1
        else:
            coverage["partial"] += 1
            bucket["coverage"]["partial"] += 1
        status = metadata.get("transcript_status", "unknown")
        transcript_status[status] += 1
        bucket["transcript_status"][status] += 1

    def render_bucket(bucket: dict) -> dict:
        counts = bucket["coverage"]
        return {
            "total": counts["total"],
            "metadata": counts["metadata"],
            "rich": counts["rich"],
            "partial": counts["partial"],
            "unresolved": counts["unresolved"],
            "fields": {
                field: counts[field]
                for field in ("description", "author", "cover_url", "webpage_url")
            },
            "transcript_status": dict(sorted(bucket["transcript_status"].items())),
        }
    return {
        "total": len(rows),
        "metadata": coverage["metadata"],
        "rich": coverage["rich"],
        "partial": coverage["partial"],
        "unresolved": coverage["unresolved"],
        "fields": {
            field: coverage[field]
            for field in ("description", "author", "cover_url", "webpage_url")
        },
        "transcript_status": dict(sorted(transcript_status.items())),
        "sources": {
            source: render_bucket(bucket)
            for source, bucket in sorted(by_source.items())
        },
        "unresolved_records": unresolved_records,
    }


def _matching_candidates(rows: list[dict], candidates: list) -> list:
    urls = {row.get("url", "") for row in rows}
    stable_ids = {
        (row.get("source", ""), row.get("source_id", ""))
        for row in rows if row.get("source_id")
    }
    return [
        candidate for candidate in candidates
        if candidate.url in urls
        or (candidate.source, candidate.source_id) in stable_ids
    ]


class MetadataBackfiller:
    def __init__(self, storage: Storage, handlers: dict | None = None):
        self.storage = storage
        self.handlers = handlers or {
            "podcast_rss": self._podcast_rss,
            "librivox": self._librivox,
            "xiaoyuzhou": self._xiaoyuzhou,
            "ximalaya": self._ximalaya,
            "bilibili": self._bilibili,
        }

    async def run(self, *, source: str | None = None, limit: int = 10_000,
                  missing_only: bool = False) -> dict:
        all_before = self.storage.get_metadata_records(limit, source)
        targets = self.storage.get_metadata_records(limit, source, missing_only)
        grouped = {}
        for row in targets:
            grouped.setdefault(row["source"], []).append(row)
        results = {}
        for source_name, rows in grouped.items():
            handler = self.handlers.get(source_name)
            if handler is None:
                results[source_name] = {
                    "targets": len(rows), "fetched": 0, "matched": 0,
                    "updated": 0, "error": "unsupported source",
                }
                continue
            try:
                candidates = await handler(rows)
                matched = _matching_candidates(rows, candidates)
                updated = self.storage.enrich_records(matched)
                results[source_name] = {
                    "targets": len(rows), "fetched": len(candidates),
                    "matched": len(matched), "updated": updated, "error": "",
                }
            except Exception as exc:
                results[source_name] = {
                    "targets": len(rows), "fetched": 0, "matched": 0,
                    "updated": 0,
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                }
        all_after = self.storage.get_metadata_records(limit, source)
        return {
            "before": audit_records(all_before),
            "after": audit_records(all_after),
            "sources": results,
        }

    async def _podcast_rss(self, rows: list[dict]) -> list:
        from spiders.podcast_rss import PodcastRSSSpider

        spider = PodcastRSSSpider()
        spider.max_eps = max(spider.max_eps, len(rows), 200)
        return await spider.crawl()

    async def _librivox(self, rows: list[dict]) -> list:
        from spiders.librivox import LibriVoxSpider

        spider = LibriVoxSpider()
        spider.max_items = max(spider.max_items, 50)
        spider.max_tracks_per_book = max(spider.max_tracks_per_book, len(rows), 200)
        wanted = archive_identifiers(rows)
        records = []
        async with aiohttp.ClientSession() as session:
            books = await spider._fetch_books(session)
            for book in books:
                candidates = {
                    part for key in ("url_iarchive", "url_rss")
                    for part in [urlsplit(book.get(key, "")).path.rstrip("/").split("/")[-1]]
                    if part
                }
                if wanted and not wanted.intersection(candidates):
                    continue
                await spider.limiter.acquire()
                records.extend(await spider._fetch_book_tracks(session, book))
        return records

    async def _xiaoyuzhou(self, rows: list[dict]) -> list:
        from spiders.xiaoyuzhou import XiaoyuzhouSpider

        spider = XiaoyuzhouSpider()
        spider.max_eps = max(spider.max_eps, len(rows), 200)
        records = []
        async with aiohttp.ClientSession() as session:
            for path in xiaoyuzhou_podcast_paths(rows):
                await spider.limiter.acquire()
                records.extend(await spider._parse_podcast_page(session, path))
                await random_delay(0.5, 1.0)
        return records

    async def _ximalaya(self, rows: list[dict]) -> list:
        from spiders.ximalaya import XimalayaSpider

        spider = XimalayaSpider()
        records = []
        async with aiohttp.ClientSession() as session:
            for row in rows:
                track_id = row.get("source_id", "")
                if not track_id:
                    continue
                await spider.limiter.acquire()
                record = await spider._get_track_audio(session, track_id)
                if record:
                    records.append(record)
        return records

    async def _bilibili(self, rows: list[dict]) -> list:
        from spiders.bilibili import BilibiliSpider

        spider = BilibiliSpider()
        async with aiohttp.ClientSession(headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.bilibili.com",
        }) as session:
            return await spider.enrich_existing(session, rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="审计并按稳定来源 ID 回填正式库历史详细信息",
    )
    parser.add_argument("--db", type=Path, default=Path("audiospider.db"))
    parser.add_argument("--source", choices=SOURCES)
    parser.add_argument("--limit", type=positive_int, default=10_000)
    parser.add_argument("--missing-only", action="store_true",
                        help="只请求 metadata_json 为空的记录")
    parser.add_argument("--audit-only", action="store_true",
                        help="只输出覆盖率，不访问来源或写数据库")
    parser.add_argument("--report", type=Path,
                        help="可选：把同一 JSON 结果写入文件")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    storage = Storage(str(args.db.resolve()))
    if args.audit_only:
        report = {
            "audit": audit_records(storage.get_metadata_records(args.limit, args.source))
        }
    else:
        report = asyncio.run(MetadataBackfiller(storage).run(
            source=args.source, limit=args.limit, missing_only=args.missing_only,
        ))
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
