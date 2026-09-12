# Copyright (c) 2026 Hao Yin. All rights reserved.

"""B站爬虫 — 采集可下载的分 P 视频 bundle 任务。

策略：
1. 搜索 API 按关键词翻页找语音类长视频（max_search_pages）
2. 搜到一页后立刻解析该页视频的分 P 与字幕元数据（边搜边解析）
3. 每个视频解析完立刻通过 on_batch 增量入库（合集分P多时更安全）
4. 解析当前页时预取下一页搜索结果（asyncio 流水线，共用限速器）

注意：采集阶段只入库稳定的 BV 分 P 页面 URL；DASH 视频/音频
签名 URL 留给下载器在实际传输时获取，不入库。
"""

import asyncio
from pathlib import Path
import re
from collections.abc import Callable
from datetime import datetime, timezone

import aiohttp

from anti_crawler import random_delay, RateLimiter
from background import encode_metadata, metadata_envelope, plain_text
from bilibili_proxy import get_bilibili_proxy, proxy_request_kwargs
from bilibili_subtitles import classify_subtitle_inventory, sanitize_subtitle_track
from bilibili_dataset import (
    build_jobs,
    build_job_key,
    caption_language_matches_content,
    default_caption_languages,
    load_manifest,
)
from config import SPIDER_CONFIGS
from spiders.base import BaseSpider
from storage import AudioRecord

# 默认关键词兜底；实际以 config.py → bilibili.search_keywords 为准
SEARCH_KEYWORDS = [
    "有声书 合集", "有声书 全集", "听书 合集",
    "评书 单田芳", "评书 袁阔成", "评书 田连元",
    "相声 郭德纲", "相声 德云社", "相声 合集",
    "演讲 TED 中文", "脱口秀 合集",
    "广播剧 全集", "朗读 名著", "百家讲坛",
]

BILIBILI_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://www.bilibili.com",
}

SEARCH_URL = "https://api.bilibili.com/x/web-interface/search/all/v2"
PAGELIST_URL = "https://api.bilibili.com/x/player/pagelist"
VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
PLAYER_URL = "https://api.bilibili.com/x/player/v2"


class BilibiliSpider(BaseSpider):
    name = "bilibili"

    def __init__(self):
        super().__init__()
        cfg = SPIDER_CONFIGS.get(self.name, {})
        self.manifest_path = str(cfg.get("manifest_path") or "").strip()
        self.keywords = cfg.get("search_keywords", SEARCH_KEYWORDS)
        self.max_videos_per_keyword = cfg.get("max_videos_per_keyword", 10)
        self.max_pages_per_video = cfg.get("max_pages_per_video", 20)
        # 搜索结果翻页数：每页约 20 个视频，翻页越多每个关键词覆盖越广
        self.max_search_pages = cfg.get("max_search_pages", 1)
        self.max_new_records = cfg.get("max_new_records", 0)
        self.required_title_terms = cfg.get("required_title_terms", [])
        self.excluded_title_terms = cfg.get("excluded_title_terms", [])
        self.min_duration_seconds = cfg.get("min_duration_seconds", 0)
        self.max_duration_seconds = cfg.get("max_duration_seconds", 4 * 3600)
        self.content_language = str(cfg.get("content_language") or "und")
        self.visual_ocr_fallback = bool(cfg.get("visual_ocr_fallback", False))
        self.visual_ocr_profile = str(
            cfg.get("visual_ocr_profile") or "bilibili-visual-ocr-zh-v1"
        ).strip()
        self.proxy = get_bilibili_proxy()
        self.category_override = str(cfg.get("category_override") or "").strip()
        if self.category_override and self.category_override not in {
            "有声书", "播客", "相声", "评书", "演讲", "脱口秀",
            "广播剧", "新闻", "访谈", "朗读", "视频",
            "影视", "会议论坛",
        }:
            raise ValueError("bilibili.category_override is not a supported category")
        self.limiter = RateLimiter(rate=0.5, burst=3)

    async def crawl(
        self,
        on_batch: Callable[[list[AudioRecord]], tuple[int, int] | None] | None = None,
    ) -> list[AudioRecord]:
        """按正式清单或搜索解析，并通过同一个 on_batch 路径入库。"""
        if self.manifest_path:
            return await self._crawl_manifest(on_batch=on_batch)
        self.logger.info(
            f"开始爬取B站, 关键词: {len(self.keywords)} 个"
            f"（最多翻 {self.max_search_pages} 页/词，解析前 {self.max_videos_per_keyword} 个/词"
            + (f"，本轮新增上限 {self.max_new_records} 个父 BV" if self.max_new_records else "")
            + "）"
        )
        records: list[AudioRecord] = []
        total_records = 0
        new_parents = 0
        seen_urls: set[str] = set()
        seen_bvids: set[str] = set()

        async with aiohttp.ClientSession(headers=BILIBILI_HEADERS) as session:
            # 先访问首页获取 cookie
            try:
                async with session.get(
                    "https://www.bilibili.com",
                    timeout=aiohttp.ClientTimeout(total=10),
                    **proxy_request_kwargs(self.proxy),
                ) as response:
                    await response.read()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                self.logger.warning(f"B站 cookie 预热失败，将继续搜索: {exc}")

            for keyword in self.keywords:
                if self.max_new_records and new_parents >= self.max_new_records:
                    break
                parsed_for_keyword = 0
                keyword_records = 0
                prefetch: asyncio.Task | None = None

                try:
                    for page in range(1, self.max_search_pages + 1):
                        if self.max_new_records and new_parents >= self.max_new_records:
                            break
                        if parsed_for_keyword >= self.max_videos_per_keyword:
                            break

                        if prefetch is not None:
                            videos = await prefetch
                            prefetch = None
                        else:
                            videos = await self._search_page(session, keyword, page)

                        if not videos:
                            self.logger.info(f"搜索 \"{keyword}\" 第 {page} 页无结果，停止翻页")
                            break

                        # 过滤跨页/跨关键词已见过的 bvid
                        fresh = []
                        for bvid, title, duration_str in videos:
                            if bvid in seen_bvids:
                                continue
                            seen_bvids.add(bvid)
                            fresh.append((bvid, title, duration_str))

                        remain = self.max_videos_per_keyword - parsed_for_keyword
                        fresh = fresh[:remain]
                        self.logger.info(
                            f"搜索 \"{keyword}\" 第 {page} 页: "
                            f"{len(videos)} 个结果, 新视频 {len(fresh)} 个，开始解析"
                        )

                        # 解析当前页时预取下一页搜索（与解析重叠，共用限速器）
                        if (
                            fresh
                            and page < self.max_search_pages
                            and parsed_for_keyword + len(fresh) < self.max_videos_per_keyword
                        ):
                            prefetch = asyncio.create_task(
                                self._search_page(session, keyword, page + 1)
                            )

                        if not fresh:
                            await random_delay(1.0, 2.0)
                            continue

                        for idx, (bvid, title, duration_str) in enumerate(fresh, 1):
                            if self.max_new_records and new_parents >= self.max_new_records:
                                break
                            self.logger.info(
                                f"解析视频 [{idx}/{len(fresh)}] {bvid} "
                                f"{title[:40]}..."
                            )
                            await self.limiter.acquire()
                            page_records = await self._extract_video_records(
                                session, bvid, title, keyword
                            )
                            video_batch = [r for r in page_records if r.url not in seen_urls]
                            for record in video_batch:
                                seen_urls.add(record.url)
                            keyword_records += len(video_batch)
                            total_records += len(video_batch)
                            if on_batch is None:
                                records.extend(video_batch)
                            parsed_for_keyword += 1

                            # 每个视频解析完立刻入库（合集分P多，不能等搜完整页）
                            if video_batch and on_batch is not None:
                                result = on_batch(video_batch)
                                added = result[0] if result is not None else len(video_batch)
                                if added > 0:
                                    new_parents += 1
                                self.logger.info(
                                    f"视频 {bvid} 已入库 {len(video_batch)} 条 "
                                    f"（累计新增父 BV {new_parents} 个，发现 {total_records} 条）"
                                )
                            else:
                                if video_batch:
                                    new_parents += 1
                                self.logger.info(
                                    f"视频 {bvid} 解析完成: {len(page_records)} 条"
                                    f"（无新增可入库）"
                                )
                            await random_delay(1.0, 2.0)

                        await random_delay(1.0, 2.0)
                finally:
                    if prefetch is not None and not prefetch.done():
                        prefetch.cancel()
                        try:
                            await prefetch
                        except asyncio.CancelledError:
                            pass

                self.logger.info(
                    f"关键词 \"{keyword}\" 完成: 解析 {parsed_for_keyword} 个视频, "
                    f"发现 {keyword_records} 个视频 bundle 任务"
                )
                await random_delay(2.0, 4.0)

        self.logger.info(f"B站 共发现 {total_records} 个视频 bundle 任务")
        return records

    async def _crawl_manifest(
        self,
        on_batch: Callable[[list[AudioRecord]], tuple[int, int] | None] | None = None,
    ) -> list[AudioRecord]:
        """采集不可变清单中的精确父 BV；不会回退到实时搜索。"""

        items = load_manifest(Path(self.manifest_path))
        records: list[AudioRecord] = []
        self.logger.info("开始采集B站正式清单: %s（%d 个父 BV）", self.manifest_path, len(items))
        async with aiohttp.ClientSession(headers=BILIBILI_HEADERS) as session:
            for index, item in enumerate(items, 1):
                await self.limiter.acquire()
                batch = await self._extract_manifest_records(session, item)
                if on_batch is None:
                    records.extend(batch)
                elif batch:
                    on_batch(batch)
                self.logger.info(
                    "B站清单进度 %d/%d: %s，生成 %d 个分P任务",
                    index, len(items), item["bvid"], len(batch),
                )
                await random_delay(0.5, 1.0)
        return records

    async def _extract_manifest_records(
        self, session: aiohttp.ClientSession, item: dict,
    ) -> list[AudioRecord]:
        video_info = await self._get_video_info(session, item["bvid"])
        if not video_info:
            self.logger.warning("B站清单视频解析失败 %s: view API 无有效数据", item["bvid"])
            return []
        try:
            jobs = build_jobs(item, video_info)
        except ValueError as exc:
            self.logger.warning("B站清单视频不符合策略 %s: %s", item["bvid"], exc)
            return []
        result = []
        for job in jobs:
            await self.limiter.acquire()
            inventory = await self._get_subtitle_inventory(
                session, job["bvid"], job["cid"]
            )
            subtitles = [
                track for track in inventory["assets"]
                if caption_language_matches_content(
                    job["content_language"], track.get("language", "")
                )
            ]
            page = next(
                page for page in video_info.get("pages", [])
                if int(page.get("page") or 0) == job["part"]
            )
            title = job.get("part_title") or video_info.get("title") or job["bvid"]
            record = AudioRecord(
                url=self._canonical_page_url(job["bvid"], job["part"]),
                source=self.name,
                title=title,
                file_format="mp4",
                artifact_kind="video_bundle",
                job_key=job["job_key"],
                duration=job["duration_seconds"],
                language=job["content_language"],
                category=job["dataset_category"],
                speaker=str(job.get("program") or video_info.get("title") or "")[:30],
                source_id=f"{job['bvid']}_p{job['part']}",
            )
            self._attach_metadata(
                record, video_info, page, bvid=job["bvid"], cid=job["cid"],
                page_num=job["part"], keyword=job["dataset_category"],
                subtitles=subtitles, caption_inventory=inventory,
                manifest_job=job,
            )
            result.append(record)
        return result

    async def _search_page(self, session: aiohttp.ClientSession,
                           keyword: str, page: int) -> list[tuple[str, str, str]]:
        """搜索单页视频，返回 [(bvid, title, duration), ...]"""
        results = []
        try:
            await self.limiter.acquire()
            params = {"keyword": keyword, "page": page, "duration": 4}
            async with session.get(SEARCH_URL, params=params,
                                   timeout=aiohttp.ClientTimeout(total=15),
                                   **proxy_request_kwargs(self.proxy)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)
                for rt in data.get("data", {}).get("result", []):
                    if rt.get("result_type") != "video":
                        continue
                    for v in rt.get("data", []):
                        bvid = v.get("bvid", "")
                        if not bvid:
                            continue
                        title = re.sub(r"<[^>]+>", "", v.get("title", ""))
                        duration = v.get("duration", "")
                        results.append((bvid, title, str(duration)))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger.warning(f"B站搜索失败 \"{keyword}\" 第 {page} 页: {e}")
        return results

    async def _extract_video_records(self, session: aiohttp.ClientSession,
                                     bvid: str, video_title: str,
                                     keyword: str) -> list[AudioRecord]:
        """为视频的每个分 P 生成稳定的视频 bundle 任务。"""
        records = []
        try:
            video_info = await self._get_video_info(session, bvid)
            searchable_title = f"{video_title} {video_info.get('title', '')}"
            if self.required_title_terms and not any(
                term in searchable_title for term in self.required_title_terms
            ):
                self.logger.info(f"跳过 {bvid}: 标题不符合本轮访谈词规则")
                return []
            if self.excluded_title_terms and any(
                term in searchable_title for term in self.excluded_title_terms
            ):
                self.logger.info(f"跳过 {bvid}: 标题命中本轮排除词")
                return []
            pages = video_info.get("pages", [])
            if not pages:
                async with session.get(PAGELIST_URL, params={"bvid": bvid},
                                       timeout=aiohttp.ClientTimeout(total=10),
                                       **proxy_request_kwargs(self.proxy)) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json(content_type=None)
                    pages = data.get("data", [])

            total_parts = min(len(pages), self.max_pages_per_video)
            if total_parts >= 50:
                self.logger.info(
                    f"{bvid} 共 {len(pages)} 个分P，将解析前 {total_parts} 个"
                    f"（约需 {total_parts * 2.5 / 60:.0f} 分钟，限速中请耐心等待）"
                )

            for i, page in enumerate(pages[:self.max_pages_per_video], 1):
                cid = page.get("cid")
                part_title = page.get("part", "")
                page_num = page.get("page", 1)
                duration = page.get("duration", 0)

                if not cid:
                    continue
                if duration < self.min_duration_seconds:
                    self.logger.info(
                        f"跳过 {bvid} P{page_num}: {duration}s 小于最短时长 "
                        f"{self.min_duration_seconds}s"
                    )
                    continue
                if duration > self.max_duration_seconds:
                    self.logger.info(
                        f"跳过 {bvid} P{page_num}: {duration}s 超过最长时长 "
                        f"{self.max_duration_seconds}s"
                    )
                    continue

                await self.limiter.acquire()
                caption_inventory = await self._get_subtitle_inventory(session, bvid, cid)
                subtitles = [
                    track for track in caption_inventory["assets"]
                    if caption_language_matches_content(
                        self.content_language, track.get("language", "")
                    )
                ]

                title = f"{video_title} P{page_num}" if not part_title else part_title
                category = self.category_override or self._guess_category(keyword)

                canonical_url = self._canonical_page_url(bvid, page_num)
                record = AudioRecord(
                    url=canonical_url,
                    source=self.name,
                    title=title,
                    file_format="mp4",
                    artifact_kind="video_bundle",
                )
                record.duration = duration
                record.category = category
                record.language = self.content_language
                record.speaker = video_title[:30]
                record.source_id = f"{bvid}_p{page_num}"
                job_identity = {
                    "bvid": bvid,
                    "max_height": 720,
                    "languages": default_caption_languages(record.language),
                    "require_caption": False,
                    "visual_ocr_fallback": self.visual_ocr_fallback,
                    "visual_ocr_profile": self.visual_ocr_profile,
                    "source_revision": "current",
                }
                record.job_key = build_job_key(job_identity, page_num, cid)
                self._attach_metadata(
                    record, video_info, page, bvid=bvid, cid=cid,
                    page_num=page_num, keyword=keyword, subtitles=subtitles,
                    caption_inventory=caption_inventory,
                )
                records.append(record)

                # 大合集每隔 20P 打一次进度，避免看起来像卡住
                if total_parts >= 50 and (i % 20 == 0 or i == total_parts):
                    self.logger.info(
                        f"{bvid} 分 P 进度 {i}/{total_parts}，"
                        f"已生成 {len(records)} 条视频 bundle 任务"
                    )

                await random_delay(0.5, 1.0)

        except Exception as e:
            self.logger.warning(f"B站视频解析失败 {bvid}: {e}")
        return records

    @staticmethod
    def _canonical_page_url(bvid: str, page_num: int) -> str:
        return f"https://www.bilibili.com/video/{bvid}?p={page_num}"

    def _attach_metadata(self, record: AudioRecord, video_info: dict,
                         page: dict, *, bvid: str, cid: int,
                         page_num: int, keyword: str,
                         subtitles: list[dict],
                         caption_inventory: dict | None = None,
                         manifest_job: dict | None = None) -> AudioRecord:
        owner = video_info.get("owner") or {}
        record.webpage_url = self._canonical_page_url(bvid, page_num)
        record.description = plain_text(video_info.get("desc", ""))
        record.author = owner.get("name", "")
        record.cover_url = video_info.get("pic", "") or page.get("first_frame", "")
        published = video_info.get("pubdate")
        if published:
            record.published_at = datetime.fromtimestamp(
                int(published), tz=timezone.utc,
            ).isoformat()
        record.metadata_json = encode_metadata(metadata_envelope(
            "bilibili",
            common={
                "description": record.description,
                "webpage_url": record.webpage_url,
                "author": record.author,
                "cover_url": record.cover_url,
                "podcast_title": video_info.get("title", record.title),
                "categories": [video_info.get("tname", ""), keyword],
            },
            source_data={
                "artifact_kind": "video_bundle",
                "bvid": bvid,
                "aid": video_info.get("aid"),
                "cid": cid,
                "page": page_num,
                "part": page.get("part", ""),
                "owner": {
                    "mid": owner.get("mid"),
                    "name": owner.get("name", ""),
                    "image": owner.get("face", ""),
                },
                "copyright": video_info.get("copyright"),
                "rights": video_info.get("rights", {}),
                "stats": video_info.get("stat", {}),
                "caption_inventory": {
                    "status": (caption_inventory or {}).get("status", "unknown"),
                    "need_login_subtitle": (caption_inventory or {}).get(
                        "need_login_subtitle"
                    ),
                    "track_count": len(subtitles),
                },
                # Portable, signed-URL-free download contract.  Its field names
                # intentionally mirror bilibili_dataset.validate_manifest so a
                # downloader can recreate the exact part job at transfer time.
                "download_task": ({
                    "artifact_kind": "video_bundle",
                    "bvid": bvid,
                    "cid": cid,
                    "page": page_num,
                    "canonical_url": record.webpage_url,
                    "parts": [page_num],
                    "max_parts": 1,
                    "max_height": 720,
                    "max_duration_seconds": self.max_duration_seconds,
                    "content_language": record.language or "zh",
                    "caption_policy": {
                        "mode": "all_matching_public_tracks",
                        "languages": default_caption_languages(record.language or "und"),
                        "require_caption": False,
                        "visual_ocr_fallback": self.visual_ocr_fallback,
                        "visual_ocr_profile": self.visual_ocr_profile,
                    },
                    "rights": {
                        "status": "needs_review",
                        "rights_cleared": False,
                    },
                    "ai_generation": {"status": "unknown", "evidence": []},
                    "speaker_count": None,
                    "speaker_count_status": "needs_review",
                    "collection_policy": {
                        "keyword": keyword,
                        "required_title_terms": self.required_title_terms,
                        "excluded_title_terms": self.excluded_title_terms,
                        "min_duration_seconds": self.min_duration_seconds,
                        "max_duration_seconds": self.max_duration_seconds,
                    },
                    "source_revision": "current",
                } if manifest_job is None else {
                    "artifact_kind": "video_bundle",
                    "bvid": manifest_job["bvid"],
                    "cid": manifest_job["cid"],
                    "page": manifest_job["part"],
                    "canonical_url": record.webpage_url,
                    "parts": [manifest_job["part"]],
                    "max_parts": 1,
                    "max_height": manifest_job["max_height"],
                    "max_duration_seconds": manifest_job["max_duration_seconds"],
                    "content_language": manifest_job["content_language"],
                    "caption_policy": {
                        "mode": "all_matching_public_tracks",
                        "languages": manifest_job["languages"],
                        "require_caption": manifest_job["require_caption"],
                        "visual_ocr_fallback": manifest_job["visual_ocr_fallback"],
                        "visual_ocr_profile": manifest_job["visual_ocr_profile"],
                    },
                    "rights": manifest_job["rights"],
                    "ai_generation": manifest_job["ai_generation"],
                    "speaker_count": manifest_job["speaker_count"],
                    "speaker_count_status": manifest_job["speaker_count_status"],
                    "source_revision": manifest_job["source_revision"],
                    "batch_id": manifest_job.get("batch_id", ""),
                    "content_kind": manifest_job.get("content_kind", ""),
                    "dataset_category": manifest_job.get("dataset_category", ""),
                    "selection_slot": manifest_job.get("selection_slot", ""),
                    "candidate_metadata": manifest_job.get("candidate_metadata") or {},
                }),
            },
            assets={"transcripts": subtitles},
            transcript_status=(
                "provided" if subtitles
                else (caption_inventory or {}).get("status", "unknown")
            ),
        ))
        return record

    async def enrich_existing(self, session: aiohttp.ClientSession,
                              rows: list[dict]) -> list[AudioRecord]:
        """Fetch metadata for exact historical BV part IDs without searching."""
        grouped: dict[str, list[tuple[int, dict]]] = {}
        for row in rows:
            match = re.fullmatch(r"(BV[0-9A-Za-z]+)_p([1-9][0-9]*)", row.get("source_id", ""))
            if match:
                grouped.setdefault(match.group(1), []).append((int(match.group(2)), row))

        records = []
        for bvid, targets in grouped.items():
            await self.limiter.acquire()
            video_info = await self._get_video_info(session, bvid)
            pages = {
                int(page.get("page", index + 1)): page
                for index, page in enumerate(video_info.get("pages", []) or [])
            }
            for page_num, row in targets:
                page = pages.get(page_num)
                if not page or not page.get("cid"):
                    continue
                cid = page["cid"]
                caption_inventory = await self._get_subtitle_inventory(session, bvid, cid)
                subtitles = caption_inventory["assets"]
                artifact_kind = row.get("artifact_kind") or "audio"
                is_bundle = artifact_kind == "video_bundle"
                record = AudioRecord(
                    url=(
                        self._canonical_page_url(bvid, page_num)
                        if is_bundle else row["url"]
                    ),
                    source=self.name, title=row.get("title", ""),
                    file_format="mp4" if is_bundle else row.get("file_format", ""),
                    artifact_kind=artifact_kind,
                    job_key=row.get("job_key", ""),
                    file_size=row.get("file_size", 0), duration=row.get("duration", 0),
                    language=row.get("language") or "zh", category=row.get("category", ""),
                    speaker=row.get("speaker", ""), source_id=row.get("source_id", ""),
                    published_at=row.get("published_at", ""),
                )
                self._attach_metadata(
                    record, video_info, page, bvid=bvid, cid=cid,
                    page_num=page_num, keyword=row.get("category", ""),
                    subtitles=subtitles, caption_inventory=caption_inventory,
                )
                records.append(record)
        return records

    async def _get_video_info(self, session: aiohttp.ClientSession,
                              bvid: str) -> dict:
        try:
            async with session.get(
                VIEW_URL, params={"bvid": bvid},
                timeout=aiohttp.ClientTimeout(total=10),
                **proxy_request_kwargs(self.proxy),
            ) as response:
                if response.status != 200:
                    return {}
                payload = await response.json(content_type=None)
                return payload.get("data", {}) if payload.get("code") == 0 else {}
        except Exception as exc:
            self.logger.debug(f"获取视频背景信息失败 {bvid}: {exc}")
            return {}

    async def _get_subtitles(self, session: aiohttp.ClientSession,
                             bvid: str, cid: int) -> list[dict]:
        return (await self._get_subtitle_inventory(session, bvid, cid))["assets"]

    async def _get_subtitle_inventory(self, session: aiohttp.ClientSession,
                                      bvid: str, cid: int) -> dict:
        try:
            async with session.get(
                PLAYER_URL, params={"bvid": bvid, "cid": cid},
                timeout=aiohttp.ClientTimeout(total=10),
                **proxy_request_kwargs(self.proxy),
            ) as response:
                if response.status != 200:
                    return {
                        "status": "external_failure",
                        "need_login_subtitle": None,
                        "assets": [],
                    }
                payload = await response.json(content_type=None)
                inventory = classify_subtitle_inventory(payload.get("data"))
                subtitles = inventory["tracks"]
                results = []
                for subtitle in subtitles:
                    normalized = sanitize_subtitle_track(subtitle)
                    if normalized["url"]:
                        # 字幕 URL 可能带时效签名；队列只保留脱敏描述，
                        # 真正下载时由 bundle worker 重新获取。
                        results.append({
                            key: value for key, value in normalized.items() if key != "url"
                        })
                return {
                    "status": inventory["status"],
                    "need_login_subtitle": inventory["need_login_subtitle"],
                    "assets": results,
                }
        except Exception as exc:
            self.logger.debug(f"获取公开视频字幕失败 {bvid} cid={cid}: {exc}")
            return {"status": "external_failure", "need_login_subtitle": None, "assets": []}

    @staticmethod
    def _guess_category(keyword: str) -> str:
        if "访谈" in keyword or "专访" in keyword or "对话" in keyword:
            return "访谈"
        if "有声书" in keyword:
            return "有声书"
        if "评书" in keyword:
            return "评书"
        if "相声" in keyword:
            return "相声"
        if "演讲" in keyword or "TED" in keyword:
            return "演讲"
        if "脱口秀" in keyword:
            return "脱口秀"
        if "广播剧" in keyword:
            return "广播剧"
        if "朗读" in keyword:
            return "朗读"
        return "播客"
