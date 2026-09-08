#!/usr/bin/env python3
# Copyright (c) 2026 Hao Yin. All rights reserved.

"""URL 搜集器 — 负责发现和扩充待下载的语音 URL 池

每次需要扩充时直接运行：
    python collect.py                         # 运行全部爬虫
    python collect.py --spiders xiaoyuzhou    # 仅指定爬虫
    python collect.py --loop --interval 3600  # 每小时自动搜集一次
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler

from config import LOG_DIR, SPIDER_CONFIGS
from storage import Storage

from spiders.xiaoyuzhou import XiaoyuzhouSpider
from spiders.ximalaya import XimalayaSpider
from spiders.librivox import LibriVoxSpider
from spiders.podcast_rss import PodcastRSSSpider
from spiders.bilibili import BilibiliSpider

ALL_SPIDERS = [
    XiaoyuzhouSpider,
    PodcastRSSSpider,
    XimalayaSpider,
    LibriVoxSpider,
    BilibiliSpider,
]


def setup_logging():
    log_file = f"{LOG_DIR}/collect_{datetime.now():%Y%m%d_%H%M%S}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            RotatingFileHandler(
                log_file, maxBytes=50 * 1024 * 1024, backupCount=5,
                encoding="utf-8",
            ),
        ],
    )


def _flush_records(storage: Storage, records: list, logger: logging.Logger) -> tuple[int, int]:
    """去重后批量入库，返回 (新增条数, 回填 published_at 条数)。"""
    if not records:
        return 0, 0
    new_records = []
    for r in records:
        if r.source_id and storage.source_id_exists(r.source, r.source_id):
            continue
        if storage.url_exists(r.url):
            continue
        new_records.append(r)

    added = 0
    if new_records:
        added, _ = storage.add_urls_batch(new_records)
    # Always backfill existing rows too, even when the same batch has new URLs.
    backfilled = storage.backfill_published(records)
    return added, backfilled


async def do_crawl(storage: Storage, spider_names: list[str] | None = None):
    logger = logging.getLogger("collect")
    logger.info("=" * 60)
    logger.info("开始搜集语音 URL...")
    logger.info("=" * 60)

    total_new = 0
    for SpiderClass in ALL_SPIDERS:
        spider_name = getattr(SpiderClass, "name", "")
        if not SPIDER_CONFIGS.get(spider_name, {}).get("enabled", True):
            logger.info(f"跳过已禁用爬虫: {spider_name}")
            continue
        spider = SpiderClass()
        if spider_names and spider.name not in spider_names:
            continue

        logger.info(f"\n>>> 启动爬虫: {spider.name}")
        try:
            # 增量入库：爬虫每产出一批（如 B站每解析完一页）就立刻写库
            incremental = {"used": False, "added": 0, "backfilled": 0}

            def on_batch(batch):
                incremental["used"] = True
                added, backfilled = _flush_records(storage, batch, logger)
                incremental["added"] += added
                incremental["backfilled"] += backfilled
                if added or backfilled:
                    logger.info(
                        f"    [{spider.name}] 增量入库 +{added}"
                        + (f", 回填 published_at {backfilled}" if backfilled else "")
                    )

            records = await spider.crawl(on_batch=on_batch)

            if incremental["used"]:
                total_new += incremental["added"]
                logger.info(
                    f"<<< {spider.name}: 增量新增 {incremental['added']} 个"
                    + (
                        f", 回填 published_at {incremental['backfilled']}"
                        if incremental["backfilled"] else ""
                    )
                )
            elif records:
                added, backfilled = _flush_records(storage, records, logger)
                total_new += added
                if added:
                    logger.info(
                        f"<<< {spider.name}: 发现 {len(records)} 个, 新增 {added} 个"
                        + (f", 回填 published_at {backfilled}" if backfilled else "")
                    )
                elif backfilled:
                    logger.info(
                        f"<<< {spider.name}: 发现 {len(records)} 个, "
                        f"全部已存在, 回填 published_at {backfilled}"
                    )
                else:
                    logger.info(f"<<< {spider.name}: 发现 {len(records)} 个, 全部已存在")
            else:
                logger.info(f"<<< {spider.name}: 未发现音频")
        except Exception as e:
            logger.error(f"<<< {spider.name} 异常: {e}", exc_info=True)

    logger.info(f"\n搜集完成, 本轮新增 {total_new} 条 URL")
    storage.show_stats()
    return total_new


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def main():
    parser = argparse.ArgumentParser(description="AudioSpider URL 搜集器")
    parser.add_argument("--spiders", nargs="*", default=None,
                        choices=["xiaoyuzhou", "ximalaya", "librivox",
                                 "podcast_rss", "bilibili"],
                        help="指定爬虫：xiaoyuzhou ximalaya librivox "
                             "podcast_rss bilibili")
    parser.add_argument("--loop", action="store_true", help="持续循环搜集")
    parser.add_argument("--interval", type=_positive_int, default=3600, help="循环间隔(秒)")

    args = parser.parse_args()
    setup_logging()
    storage = Storage()
    logger = logging.getLogger("collect")

    if args.loop:
        async def loop():
            round_num = 0
            while True:
                round_num += 1
                logger.info(f"\n{'#' * 60}")
                logger.info(f"# 第 {round_num} 轮搜集 — {datetime.now():%Y-%m-%d %H:%M:%S}")
                logger.info(f"{'#' * 60}")
                await do_crawl(storage, args.spiders)
                logger.info(f"等待 {args.interval} 秒...")
                await asyncio.sleep(args.interval)
        asyncio.run(loop())
    else:
        asyncio.run(do_crawl(storage, args.spiders))


if __name__ == "__main__":
    main()
