#!/usr/bin/env python3
# Copyright (c) 2026 AudioSpider Contributors.

"""以严格上限真实探测单个来源，不污染正式数据库。"""

import argparse
import asyncio
import json
import logging
from pathlib import Path
import tempfile
import time
from urllib.parse import urlsplit


SOURCES = ("podcast_rss", "librivox", "xiaoyuzhou", "ximalaya", "bilibili")


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def database_snapshot(storage, source: str):
    """返回（全库记录数，当前来源记录）。

    当 --db 指向正式库时，不能用其他来源的旧记录把本次空结果误报为成功。
    """
    connection = storage._get_conn()
    total = connection.execute("SELECT COUNT(*) FROM audio_urls").fetchone()[0]
    rows = connection.execute(
        "SELECT * FROM audio_urls WHERE source=? ORDER BY id", (source,),
    ).fetchall()
    return total, rows


def configure_spider(args):
    if args.source == "podcast_rss":
        from spiders.podcast_rss import PodcastRSSSpider

        spider = PodcastRSSSpider()
        spider.feeds = spider.feeds[:args.feeds]
        spider.max_eps = args.episodes
        return spider

    if args.source == "librivox":
        from spiders.librivox import LibriVoxSpider

        spider = LibriVoxSpider()
        spider.max_items = args.books
        spider.max_tracks_per_book = args.episodes
        return spider

    if args.source == "xiaoyuzhou":
        from spiders.xiaoyuzhou import XiaoyuzhouSpider

        spider = XiaoyuzhouSpider()
        spider.discover_urls = spider.discover_urls[:args.feeds]
        spider.max_eps = args.episodes
        if not args.include_discovery:
            async def no_discovery(_session):
                return []
            spider._discover_podcasts = no_discovery
        return spider

    if args.source == "ximalaya":
        import spiders.ximalaya as module

        module.SEED_TRACK_IDS = module.SEED_TRACK_IDS[:args.seeds]
        module.PROBE_RANGE = args.probe_range
        module.MAX_TOTAL_PROBES = args.probes
        spider = module.XimalayaSpider()
        spider.max_tracks = args.episodes
        return spider

    from spiders.bilibili import BilibiliSpider

    spider = BilibiliSpider()
    spider.keywords = args.keywords or ["有声书 合集"]
    spider.max_search_pages = args.search_pages
    spider.max_videos_per_keyword = args.videos
    spider.max_pages_per_video = args.parts
    return spider


async def run_probe(args) -> dict:
    from storage import Storage

    if args.db:
        db_path = args.db.resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        root = Path(tempfile.mkdtemp(prefix=f"audiospider-probe-{args.source}.", dir="/tmp"))
        db_path = root / "probe.db"
    storage = Storage(str(db_path))
    inserted = 0
    backfilled = 0
    batches = 0

    def on_batch(records):
        nonlocal inserted, backfilled, batches
        added, updated = storage.add_urls_batch(records)
        inserted += added
        backfilled += updated
        batches += 1

    spider = configure_spider(args)
    started = time.monotonic()
    try:
        returned = await asyncio.wait_for(
            spider.crawl(on_batch=on_batch), timeout=args.timeout,
        )
        error = ""
    except Exception as exc:
        returned = []
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.monotonic() - started

    database_records, rows = database_snapshot(storage, args.source)
    samples = []
    for row in rows[:args.samples]:
        parsed = urlsplit(row["url"])
        samples.append({
            "title": row["title"][:100],
            "media_host": parsed.hostname or "",
            "scheme": parsed.scheme,
            "format": row["file_format"],
            "duration": row["duration"],
            "language": row["language"],
            "category": row["category"],
            "source_id": row["source_id"][:100],
            "published_at": row["published_at"],
        })
    invalid_urls = sum(
        1 for row in rows
        if urlsplit(row["url"]).scheme not in {"http", "https"}
        or not urlsplit(row["url"]).hostname
    )
    return {
        "source": args.source,
        "result": "error" if error else "ok" if rows else "empty",
        "elapsed_seconds": round(elapsed, 3),
        "returned_records": len(returned),
        "callback_batches": batches,
        "inserted_records": inserted,
        "database_records": database_records,
        "source_records": len(rows),
        "backfilled_records": backfilled,
        "invalid_media_urls": invalid_urls,
        "error": error,
        "samples": samples,
        "database": str(db_path),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="小规模真实测试一个来源；默认写临时数据库且不下载音频",
    )
    parser.add_argument("--source", required=True, choices=SOURCES)
    parser.add_argument("--feeds", type=positive_int, default=1,
                        help="固定 RSS/小宇宙最多测试几个源，默认 1")
    parser.add_argument("--episodes", type=positive_int, default=5,
                        help="每个 RSS/节目/书最多取几集或章，默认 5")
    parser.add_argument("--books", type=positive_int, default=1,
                        help="LibriVox 最多测试几本书，默认 1")
    parser.add_argument("--seeds", type=positive_int, default=2,
                        help="喜马拉雅使用几个种子，默认 2")
    parser.add_argument("--probes", type=positive_int, default=6,
                        help="喜马拉雅总探测上限，默认 6")
    parser.add_argument("--probe-range", type=positive_int, default=1,
                        help="喜马拉雅相邻 ID 范围，默认 1")
    parser.add_argument("--keywords", nargs="*",
                        help="B站测试关键词；默认使用“有声书 合集”")
    parser.add_argument("--search-pages", type=positive_int, default=1,
                        help="B站每关键词搜索页数，默认 1")
    parser.add_argument("--videos", type=positive_int, default=2,
                        help="B站每关键词视频数，默认 2")
    parser.add_argument("--parts", type=positive_int, default=3,
                        help="B站每视频分P数，默认 3")
    parser.add_argument("--include-discovery", action="store_true",
                        help="小宇宙额外访问首页/发现页")
    parser.add_argument("--timeout", type=positive_int, default=120,
                        help="整个探针超时秒数，默认 120")
    parser.add_argument("--samples", type=positive_int, default=3,
                        help="输出几条脱敏样例，默认 3")
    parser.add_argument("--db", type=Path,
                        help="可选输出数据库；默认写 /tmp 临时数据库")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    summary = asyncio.run(run_probe(args))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["result"] == "error" else 2 if summary["result"] == "empty" else 0


if __name__ == "__main__":
    raise SystemExit(main())
