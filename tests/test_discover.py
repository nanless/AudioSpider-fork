import unittest

from aiohttp import ClientSession, web

from discover import FeedFetchError, filter_feeds_by_host, parse_rss_feed


class DiscoverTests(unittest.IsolatedAsyncioTestCase):
    def test_excluded_feed_hosts_remain_out_of_current_batch(self):
        feeds = [
            {"feed_url": "https://anchor.fm/show/rss"},
            {"feed_url": "https://feeds.example.test/rss"},
        ]
        self.assertEqual(
            filter_feeds_by_host(feeds, ["anchor.fm"]),
            [{"feed_url": "https://feeds.example.test/rss"}],
        )

    async def asyncSetUp(self):
        self.app = web.Application()
        self.app.router.add_get("/feed.xml", self.feed)
        self.app.router.add_get("/failure.xml", self.failure)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        self.port = self.site._server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def feed(self, _request):
        xml = """<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
        <channel><title>Local</title><language>zh-CN</language>
        <itunes:author>Author</itunes:author><itunes:image href="https://media.example.test/show.jpg"/>
        <item><title>Episode</title><guid>episode-1</guid>
        <link>https://media.example.test/episode</link><description>Show notes</description>
        <pubDate>Tue, 08 Sep 2026 00:00:00 +0000</pubDate>
        <enclosure url="https://media.example.test/e.mp3" length="12" type="audio/mpeg" />
        </item></channel></rss>"""
        return web.Response(text=xml, content_type="application/rss+xml")

    async def failure(self, _request):
        return web.Response(status=503)

    async def test_valid_feed_parses(self):
        async with ClientSession() as session:
            rows = await parse_rss_feed(
                session, f"http://127.0.0.1:{self.port}/feed.xml",
                allow_private_network=True,
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].source_id, "episode-1")
        self.assertEqual(rows[0].language, "zh")
        self.assertEqual(rows[0].description, "Show notes")
        self.assertEqual(rows[0].author, "Author")
        self.assertEqual(rows[0].webpage_url, "https://media.example.test/episode")
        self.assertTrue(rows[0].metadata_json)

    async def test_fetch_failure_is_not_an_empty_success(self):
        async with ClientSession() as session:
            with self.assertRaises(FeedFetchError):
                await parse_rss_feed(
                    session, f"http://127.0.0.1:{self.port}/failure.xml",
                    allow_private_network=True,
                )


if __name__ == "__main__":
    unittest.main()
