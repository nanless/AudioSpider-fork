# Copyright (c) 2026 Hao Yin. All rights reserved.

"""小宇宙播客爬虫 - 中文播客第一平台

发现逻辑：
1. 访问播客主页，从页面 JSON-LD / 内嵌数据中提取所有单集的 m4a CDN 直链
2. 支持按播客 URL 列表 + 分类发现页扩展
3. CDN (media.xyzcdn.net) 无鉴权，直接下载
"""

import re
import json
import aiohttp
from bs4 import BeautifulSoup

from anti_crawler import build_headers, random_delay, RateLimiter
from background import encode_metadata, metadata_envelope, plain_text, sanitize_html
from config import SPIDER_CONFIGS
from spiders.base import BaseSpider
from storage import AudioRecord


class XiaoyuzhouSpider(BaseSpider):
    name = "xiaoyuzhou"

    def __init__(self):
        super().__init__()
        cfg = SPIDER_CONFIGS[self.name]
        self.base_url = cfg["base_url"]
        self.cdn_domain = cfg["cdn_domain"]
        self.discover_urls = cfg["discover_urls"]
        self.max_eps = cfg["max_episodes_per_podcast"]
        self.limiter = RateLimiter(rate=0.3, burst=2)

    async def crawl(self, on_batch=None) -> list[AudioRecord]:
        self.logger.info("开始爬取小宇宙播客...")
        records = []
        seen_urls = set()

        async with aiohttp.ClientSession() as session:
            podcast_paths = list(self.discover_urls)
            discovered = await self._discover_podcasts(session)
            podcast_paths.extend(discovered)

            for path in dict.fromkeys(podcast_paths):
                await self.limiter.acquire()
                eps = await self._parse_podcast_page(session, path)
                for r in eps:
                    if r.url not in seen_urls:
                        seen_urls.add(r.url)
                        records.append(r)
                if eps and on_batch is not None:
                    on_batch(eps)
                await random_delay(1.5, 3.0)

        self.logger.info(f"小宇宙 共发现 {len(records)} 个语音文件")
        return records

    async def _discover_podcasts(self, session: aiohttp.ClientSession) -> list[str]:
        """从小宇宙首页/发现页抓取更多播客链接"""
        paths = set()
        try:
            for page_url in [self.base_url, f"{self.base_url}/explore"]:
                async with session.get(page_url,
                                       headers=build_headers(self.base_url),
                                       timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        continue
                    html = await resp.text()
                    for m in re.finditer(r'"/podcast/([a-f0-9]{24})"', html):
                        paths.add(f"/podcast/{m.group(1)}")
                await random_delay(1.0, 2.0)
        except Exception as e:
            self.logger.warning(f"发现页抓取失败: {e}")
        self.logger.info(f"小宇宙: 自动发现 {len(paths)} 个播客")
        return list(paths)

    async def _parse_podcast_page(self, session: aiohttp.ClientSession,
                                   path: str) -> list[AudioRecord]:
        url = self.base_url + path if not path.startswith("http") else path
        podcast_id = path.rsplit("/", 1)[-1]
        records = []
        try:
            async with session.get(url, headers=build_headers(self.base_url),
                                   timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    return []
                html = await resp.text()

                podcast_title = ""
                soup = BeautifulSoup(html, "lxml")
                h1 = soup.find("h1")
                if h1:
                    podcast_title = h1.get_text(strip=True)

                next_script = soup.find("script", id="__NEXT_DATA__")
                if next_script and next_script.string:
                    try:
                        next_data = json.loads(next_script.string)
                        podcast = next_data["props"]["pageProps"]["podcast"]
                        rich_records = self._records_from_podcast_data(podcast)
                        if rich_records:
                            return rich_records[:self.max_eps]
                    except (KeyError, TypeError, json.JSONDecodeError) as exc:
                        self.logger.debug(f"小宇宙 __NEXT_DATA__ 解析回退: {exc}")

                entries = re.findall(
                    r'\{"eid":"([^"]+)".*?"title":"((?:[^"\\]|\\.)*)".*?'
                    r'"enclosure":\{[^}]*"url":"(https://media\.xyzcdn\.net/[^"]+\.m4a)"',
                    html,
                )

                if not entries:
                    m4a_urls = re.findall(
                        rf'"(https://{re.escape(self.cdn_domain)}/[^"]+\.m4a)"', html
                    )
                    titles = re.findall(r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
                    for i, m4a_url in enumerate(m4a_urls[:self.max_eps]):
                        title = titles[i] if i < len(titles) else ""
                        record = self._make_record(url=m4a_url, title=title, file_format="m4a")
                        record.category = "播客"
                        record.language = "zh"
                        record.speaker = podcast_title
                        record.source_id = m4a_url.rsplit("/", 1)[-1].split(".")[0]
                        records.append(record)
                    return records

                for eid, title, m4a_url in entries[:self.max_eps]:
                    try:
                        title = json.loads(f'"{title}"')
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass
                    record = self._make_record(url=m4a_url, title=title, file_format="m4a")
                    record.category = "播客"
                    record.language = "zh"
                    record.speaker = podcast_title
                    record.source_id = eid
                    records.append(record)

        except Exception as e:
            self.logger.error(f"小宇宙播客解析失败 {path}: {e}")
        return records

    def _records_from_podcast_data(self, podcast: dict) -> list[AudioRecord]:
        records = []
        podcast_title = podcast.get("title", "")
        author = podcast.get("author", "")
        podcast_description = podcast.get("description", "")
        podcast_cover = (podcast.get("image") or {}).get("picUrl", "")
        podcasters = [
            {
                "uid": person.get("uid", ""),
                "name": person.get("nickname", ""),
                "bio": person.get("bio", ""),
                "image": ((person.get("avatar") or {}).get("picture") or {}).get("picUrl", ""),
            }
            for person in podcast.get("podcasters", [])
        ]
        for episode in podcast.get("episodes", []):
            media_url = (episode.get("enclosure") or {}).get("url", "")
            if not media_url:
                media_url = (((episode.get("media") or {}).get("source") or {}).get("url", ""))
            if not media_url:
                continue
            description = episode.get("description", "") or episode.get("shownotes", "") or ""
            cover = (episode.get("image") or {}).get("picUrl", "") or podcast_cover
            eid = episode.get("eid", "")
            record = self._make_record(
                url=media_url, title=episode.get("title", ""), file_format="m4a",
            )
            record.duration = int(episode.get("duration") or 0)
            record.category = "播客"
            record.language = "zh"
            record.speaker = podcast_title
            record.source_id = eid or media_url.rsplit("/", 1)[-1].split(".")[0]
            record.published_at = str(episode.get("pubDate") or "")
            record.webpage_url = f"{self.base_url}/episode/{eid}" if eid else ""
            record.description = plain_text(description)
            record.author = author
            record.cover_url = cover
            record.metadata_json = encode_metadata(metadata_envelope(
                "xiaoyuzhou",
                common={
                    "description": record.description,
                    "description_html": sanitize_html(description),
                    "webpage_url": record.webpage_url,
                    "author": author,
                    "cover_url": cover,
                    "podcast_title": podcast_title,
                    "podcast_description": plain_text(podcast_description),
                    "people": podcasters,
                    "categories": episode.get("labels", []) or podcast.get("topicLabels", []),
                },
                source_data={
                    "podcast_id": podcast.get("pid", ""),
                    "episode_id": eid,
                    "pay_type": episode.get("payType"),
                    "sponsors": episode.get("sponsors", []),
                    "transcript_reference": episode.get("transcript", {}),
                    "play_count": episode.get("playCount"),
                    "comment_count": episode.get("commentCount"),
                    "favorite_count": episode.get("favoriteCount"),
                },
                transcript_status=(
                    "reference_only" if episode.get("transcript") else None
                ),
            ))
            records.append(record)
        return records
