import unittest

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
        self.assertEqual(
            rows[0]["url"],
            "https://aisubtitle.hdslb.com/subtitle.json?token=temporary",
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


if __name__ == "__main__":
    unittest.main()
