#!/usr/bin/env python3
# Copyright (c) 2026 Hao Yin. All rights reserved.

"""音频下载器 — 从数据库取待下载 URL 并批量下载

用法:
    python main.py                           # 下载 50 条
    python main.py --limit 200               # 下载 200 条
    python main.py --source xiaoyuzhou       # 仅下载指定来源
    python main.py --category 播客           # 仅下载指定分类
    python main.py --language zh             # 仅下载中文音频
    python main.py --language en             # 仅下载英文音频
    python main.py --per-source --limit 20   # 每个来源各下载 20 条
    python main.py --per-category --limit 10 # 每个分类各下载 10 条
    python main.py --workers 10              # 10 并发下载
    python main.py --format original         # 保留原始格式，跳过 ffmpeg 转 opus
    python main.py --loop                    # 持续消费下载
    python main.py --retry-failed            # 重试所有失败的 URL
    python main.py --retry-failed --source bilibili  # 只重试指定来源的失败 URL
    python main.py stats                     # 查看统计 + 已下载文件
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler

from config import (
    DOWNLOAD_LEASE_SECONDS,
    LOG_DIR,
    MAX_BATCH_SIZE,
    MAX_DOWNLOAD_WORKERS,
)
from storage import Storage
from downloader import Downloader


def setup_logging():
    log_file = f"{LOG_DIR}/download_{datetime.now():%Y%m%d_%H%M%S}.log"
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


def _bounded_positive(maximum: int):
    def parse(value: str) -> int:
        number = int(value)
        if not 1 <= number <= maximum:
            raise argparse.ArgumentTypeError(f"必须在 1..{maximum} 之间")
        return number
    return parse


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def main():
    parser = argparse.ArgumentParser(description="AudioSpider 音频下载器")
    parser.add_argument("action", nargs="?", default="download",
                        choices=["download", "stats", "fix-meta", "background"],
                        help="download=下载(默认), stats=统计, "
                             "fix-meta=补生成JSON, background=补背景信息/资产")
    parser.add_argument("--source", default=None, help="仅下载指定来源 (如 podcast_rss, bilibili)")
    parser.add_argument("--category", default=None, help="仅下载指定分类 (如 播客, 有声书)")
    parser.add_argument("--language", default=None, help="仅下载指定语种 (如 zh, en)")
    grouping = parser.add_mutually_exclusive_group()
    grouping.add_argument("--per-source", action="store_true", help="每个来源各下载 --limit 条")
    grouping.add_argument("--per-category", action="store_true", help="每个分类各下载 --limit 条")
    parser.add_argument("--limit", type=_bounded_positive(MAX_BATCH_SIZE), default=50,
                        help=f"单批下载数量(默认50, 最大{MAX_BATCH_SIZE})")
    parser.add_argument("--workers", type=_bounded_positive(MAX_DOWNLOAD_WORKERS), default=None,
                        help=f"并发下载数(默认4, 最大{MAX_DOWNLOAD_WORKERS})")
    parser.add_argument("--format", choices=["opus", "original"], default="opus",
                        help="保存格式: opus=ffmpeg转opus(默认), original=保留原始格式(省CPU)")
    parser.add_argument(
        "--background", choices=["none", "metadata", "all"], default="all",
        help="背景信息: none=不保存, metadata=仅JSON/描述, "
             "all=再下载公开封面/字幕/章节/原文(默认)",
    )
    parser.add_argument("--retry-failed", action="store_true",
                        help="将所有 failed 状态重置为 pending 并重新下载")
    parser.add_argument("--loop", action="store_true", help="持续循环消费下载")
    parser.add_argument("--interval", type=_positive_int, default=60, help="循环间隔秒数(默认60)")
    parser.add_argument("--since", default=None,
                        help="仅下载发布时间 >= 此日期的 (如 2024-01-01)")
    parser.add_argument("--before", default=None,
                        help="仅下载发布时间 <= 此日期的 (如 2024-12-31)")

    args = parser.parse_args()
    setup_logging()
    storage = Storage()

    if args.action == "stats":
        storage.show_stats()
        return

    if args.action == "fix-meta":
        from downloader import fix_meta
        fix_meta(storage)
        return

    if args.action == "background":
        downloader = Downloader(
            storage, max_workers=args.workers, convert=False,
            background_mode=args.background,
        )
        asyncio.run(downloader.refresh_background(args.limit, args.source))
        return

    if args.retry_failed:
        logger = logging.getLogger("download")
        dl = Downloader(storage, max_workers=args.workers,
                        convert=args.format == "opus",
                        background_mode=args.background)
        failed_items = storage.claim_failed(
            limit=args.limit,
            source=args.source,
            worker_id=dl.worker_id,
            lease_seconds=DOWNLOAD_LEASE_SECONDS,
        )
        if not failed_items:
            scope = f"来源={args.source}" if args.source else "全部来源"
            logger.info(f"没有失败的 URL 需要重试（{scope}）")
            return
        scope = f"来源={args.source}" if args.source else "全部来源"
        logger.info(f"准备重试 {len(failed_items)} 条失败的 URL（{scope}）")

        async def retry():
            return await dl.download_all(items=failed_items)
        asyncio.run(retry())
        storage.show_stats()
        return

    dl_kwargs = dict(
        limit=args.limit,
        source=args.source,
        category=args.category,
        language=args.language,
        per_source=args.per_source,
        per_category=args.per_category,
        published_since=args.since,
        published_before=args.before,
    )

    convert = args.format == "opus"

    async def download_once():
        dl = Downloader(
            storage, max_workers=args.workers, convert=convert,
            background_mode=args.background,
        )
        return await dl.download_all(**dl_kwargs)

    async def download_loop():
        logger = logging.getLogger("download")
        round_num = 0
        while True:
            round_num += 1
            logger.info(f"\n=== 下载轮次 {round_num} ===")
            dl = Downloader(
                storage, max_workers=args.workers, convert=convert,
                background_mode=args.background,
            )
            stats = await dl.download_all(**dl_kwargs)
            storage.show_stats()
            if stats["success"] == 0 and stats["failed"] == 0:
                logger.info(f"本轮无新下载, 等待 {args.interval} 秒...")
            await asyncio.sleep(args.interval)

    if args.loop:
        asyncio.run(download_loop())
    else:
        asyncio.run(download_once())
        storage.show_stats()


if __name__ == "__main__":
    main()
