import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web

from background import decode_metadata
import spiders.bilibili as bilibili_module
from spiders.bilibili import BilibiliSpider
from spiders.xiaoyuzhou import XiaoyuzhouSpider


class SpiderMetadataTests(unittest.TestCase):
    def test_xiaoyuzhou_next_data_becomes_rich_record(self):
        spider = XiaoyuzhouSpider()
        records = spider._records_from_podcast_data({
            "pid": "podcast-1",
            "title": "Example Podcast",
            "author": "Example Author",
            "description": "Podcast description",
            "image": {"picUrl": "https://cdn.example/podcast.jpg"},
            "podcasters": [{
                "uid": "user-1", "nickname": "Host", "bio": "Bio",
                "avatar": {"picture": {"picUrl": "https://cdn.example/host.jpg"}},
            }],
            "episodes": [{
                "eid": "episode-1",
                "title": "Episode title",
                "description": "Intro\n00:01 Chapter",
                "duration": 120,
                "pubDate": "2026-09-09T00:00:00Z",
                "enclosure": {"url": "https://cdn.example/audio.m4a"},
                "image": {"picUrl": "https://cdn.example/episode.jpg"},
                "labels": ["technology"],
                "transcript": {"mediaId": "audio.m4a"},
            }],
        })
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.author, "Example Author")
        self.assertEqual(record.description, "Intro\n00:01 Chapter")
        self.assertEqual(record.webpage_url, "https://www.xiaoyuzhoufm.com/episode/episode-1")
        self.assertEqual(record.cover_url, "https://cdn.example/episode.jpg")
        metadata = decode_metadata(record.metadata_json)
        self.assertEqual(metadata["transcript_status"], "reference_only")
        self.assertEqual(metadata["common"]["people"][0]["name"], "Host")
        self.assertEqual(
            metadata["source_data"]["xiaoyuzhou"]["transcript_reference"],
            {"mediaId": "audio.m4a"},
        )


class BilibiliMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app = web.Application()
        app.router.add_get("/player", self.player)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        self.port = self.site._server.sockets[0].getsockname()[1]
        self.previous_url = bilibili_module.PLAYER_URL
        bilibili_module.PLAYER_URL = f"http://127.0.0.1:{self.port}/player"

    async def asyncTearDown(self):
        bilibili_module.PLAYER_URL = self.previous_url
        await self.runner.cleanup()

    async def player(self, request):
        if request.query.get("bvid") == "BVauth":
            return web.json_response({
                "code": 0,
                "data": {
                    "need_login_subtitle": True,
                    "subtitle": {"subtitles": []},
                },
            })
        return web.json_response({
            "code": 0,
            "data": {"subtitle": {"subtitles": [{
                "subtitle_url": "//aisubtitle.hdslb.com/subtitle.json?token=temporary",
                "lan": "zh-CN",
                "lan_doc": "中文（自动生成）",
                "type": 1,
                "ai_type": 0,
                "ai_status": 2,
            }]}}
        })

    async def test_public_subtitle_is_declared_as_platform_asset(self):
        async with aiohttp.ClientSession() as session:
            rows = await BilibiliSpider()._get_subtitles(session, "BV1test", 1)
        self.assertEqual(len(rows), 1)
        self.assertNotIn("url", rows[0])
        self.assertEqual(
            rows[0]["url_redacted"],
            "https://aisubtitle.hdslb.com/subtitle.json",
        )
        self.assertEqual(rows[0]["text_source"], "platform_auto")
        self.assertEqual(rows[0]["kind"], "automatic")
        self.assertEqual(rows[0]["selected_by_rule"], "bilibili_type_ai")
        self.assertEqual(rows[0]["ai_status"], 2)

    async def test_login_gated_caption_is_not_a_fake_transcript_asset(self):
        async with aiohttp.ClientSession() as session:
            inventory = await BilibiliSpider()._get_subtitle_inventory(session, "BVauth", 1)
        self.assertEqual(inventory["status"], "auth_required")
        self.assertEqual(inventory["assets"], [])

    async def test_collection_emits_video_bundle_without_resolving_dash_audio(self):
        spider = BilibiliSpider()
        spider.limiter.acquire = AsyncMock()
        spider._get_video_info = AsyncMock(return_value={
            "aid": 170001,
            "title": "测试稿件",
            "desc": "背景资料",
            "tname": "知识",
            "owner": {"mid": 42, "name": "UP 主", "face": "https://img.example/u.jpg"},
            "copyright": 1,
            "rights": {"download": 1},
            "stat": {"view": 123},
            "pages": [{"cid": 7001, "page": 2, "part": "第二集", "duration": 321}],
        })
        spider._get_subtitle_inventory = AsyncMock(return_value={
            "status": "not_provided_publicly",
            "need_login_subtitle": False,
            "assets": [],
        })
        # If collection still attempts to resolve the expiring DASH audio URL,
        # this test must fail immediately.
        spider._get_audio_url = AsyncMock(side_effect=AssertionError("DASH audio lookup is forbidden"))

        with patch("spiders.bilibili.random_delay", new=AsyncMock()):
            records = await spider._extract_video_records(
                object(), "BV1xx411c7mD", "测试稿件", "中文播客",
            )

        self.assertEqual(len(records), 1)
        record = records[0]
        canonical = "https://www.bilibili.com/video/BV1xx411c7mD?p=2"
        self.assertEqual(record.url, canonical)
        self.assertEqual(record.webpage_url, canonical)
        self.assertEqual(record.file_format, "mp4")
        self.assertEqual(record.artifact_kind, "video_bundle")
        self.assertEqual(record.source_id, "BV1xx411c7mD_p2")
        self.assertTrue(record.job_key.startswith("BV1xx411c7mD-p2-c7001-"))
        spider._get_audio_url.assert_not_awaited()

        metadata = decode_metadata(record.metadata_json)
        source = metadata["source_data"]["bilibili"]
        self.assertEqual(source["artifact_kind"], "video_bundle")
        self.assertEqual(source["bvid"], "BV1xx411c7mD")
        self.assertEqual(source["cid"], 7001)
        self.assertEqual(source["page"], 2)
        task = source["download_task"]
        self.assertEqual(task["cid"], 7001)
        self.assertEqual(task["page"], 2)
        self.assertEqual(task["canonical_url"], canonical)
        self.assertEqual(task["content_language"], "zh")
        self.assertEqual(task["parts"], [2])
        self.assertEqual(task["caption_policy"]["mode"], "all_matching_public_tracks")
        self.assertFalse(task["caption_policy"]["require_caption"])
        self.assertEqual(task["rights"]["status"], "needs_review")
        self.assertEqual(task["ai_generation"]["status"], "unknown")
        self.assertIsNone(task["speaker_count"])
        self.assertEqual(task["speaker_count_status"], "needs_review")
        self.assertNotIn("token=temporary", record.metadata_json)

    async def test_collection_uses_stable_source_ids_for_each_part(self):
        spider = BilibiliSpider()
        spider.limiter.acquire = AsyncMock()
        spider._get_video_info = AsyncMock(return_value={
            "title": "多 P 稿件",
            "pages": [
                {"cid": 9001, "page": 1, "part": "P1", "duration": 10},
                {"cid": 9002, "page": 3, "part": "P3", "duration": 20},
            ],
        })
        spider._get_subtitle_inventory = AsyncMock(return_value={
            "status": "unknown", "need_login_subtitle": None, "assets": [],
        })
        spider._get_audio_url = AsyncMock(side_effect=AssertionError("must not resolve DASH audio"))

        with patch("spiders.bilibili.random_delay", new=AsyncMock()):
            records = await spider._extract_video_records(
                object(), "BV1xx411c7mD", "多 P 稿件", "中文播客",
            )

        self.assertEqual(
            [record.source_id for record in records],
            ["BV1xx411c7mD_p1", "BV1xx411c7mD_p3"],
        )
        self.assertEqual(
            [record.url for record in records],
            [
                "https://www.bilibili.com/video/BV1xx411c7mD?p=1",
                "https://www.bilibili.com/video/BV1xx411c7mD?p=3",
            ],
        )
        spider._get_audio_url.assert_not_awaited()

    async def test_existing_bilibili_audio_keeps_legacy_artifact_identity(self):
        spider = BilibiliSpider()
        spider.limiter.acquire = AsyncMock()
        spider._get_video_info = AsyncMock(return_value={
            "title": "历史稿件",
            "pages": [{"cid": 8101, "page": 4, "part": "P4", "duration": 44}],
        })
        spider._get_subtitle_inventory = AsyncMock(return_value={
            "status": "auth_required", "need_login_subtitle": True, "assets": [],
        })
        old_audio_url = "https://xy.example.com/expiring-audio.m4a?deadline=1"

        records = await spider.enrich_existing(object(), [{
            "url": old_audio_url,
            "source_id": "BV1xx411c7mD_p4",
            "title": "历史 P4",
            "file_format": "m4a",
            "duration": 44,
        }])

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.url, old_audio_url)
        self.assertEqual(record.file_format, "m4a")
        self.assertEqual(record.artifact_kind, "audio")
        self.assertEqual(record.language, "zh")
        metadata = decode_metadata(record.metadata_json)
        source = metadata["source_data"]["bilibili"]
        self.assertEqual(source["caption_inventory"]["status"], "auth_required")
        self.assertEqual(source["download_task"]["parts"], [4])


if __name__ == "__main__":
    unittest.main()
