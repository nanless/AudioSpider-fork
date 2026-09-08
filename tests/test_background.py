import hashlib
import json
import os
import tempfile
import unittest

import aiohttp
from aiohttp import web

import background


class BackgroundTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        app = web.Application()
        app.router.add_get("/cover.jpg", self.cover)
        app.router.add_get("/transcript.vtt", self.transcript)
        app.router.add_get("/source.html", self.source_html)
        app.router.add_get("/bad", self.bad)
        app.router.add_get("/bad.html", self.bad)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        self.port = self.site._server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        await self.runner.cleanup()
        self.temp.cleanup()

    async def cover(self, _request):
        return web.Response(body=b"fake-jpeg", content_type="image/jpeg")

    async def transcript(self, _request):
        return web.Response(text="WEBVTT\n\n00:00.000 --> 00:01.000\nhello\n",
                            content_type="text/vtt")

    async def bad(self, _request):
        return web.Response(body=b"binary", content_type="application/x-executable")

    async def source_html(self, _request):
        return web.Response(
            text='<article>Public text</article><script>steal()</script>',
            content_type="text/html",
        )

    def item(self):
        base = f"http://127.0.0.1:{self.port}"
        metadata = background.metadata_envelope(
            "test",
            common={"description_html": "<p>Hello <b>world</b></p>"},
            assets={"transcripts": [{
                "url": base + "/transcript.vtt", "type": "text/vtt",
                "language": "en", "text_source": "platform",
            }]},
        )
        return {
            "url": base + "/audio.wav?token=secret",
            "source": "test",
            "source_id": "one",
            "title": "Example",
            "cover_url": base + "/cover.jpg?signature=secret",
            "metadata_json": background.encode_metadata(metadata),
        }

    async def test_persists_description_cover_and_transcript(self):
        audio = os.path.join(self.temp.name, "audio.wav")
        with open(audio, "wb") as output:
            output.write(b"audio")
        async with aiohttp.ClientSession() as session:
            result = await background.persist_background(
                session, audio, self.item(), mode="all", allow_private_network=True,
            )
        self.assertEqual(len(result["description_files"]), 2)
        self.assertEqual([x["status"] for x in result["assets"]], ["saved", "saved"])
        self.assertTrue(os.path.exists(os.path.splitext(audio)[0] + ".cover.jpg"))
        self.assertTrue(os.path.exists(os.path.splitext(audio)[0] + ".transcript.1.vtt"))

    async def test_private_assets_are_blocked_by_default(self):
        audio = os.path.join(self.temp.name, "audio.wav")
        with open(audio, "wb") as output:
            output.write(b"audio")
        async with aiohttp.ClientSession() as session:
            result = await background.persist_background(session, audio, self.item())
        self.assertTrue(result["assets"])
        self.assertTrue(all(x["status"] == "failed" for x in result["assets"]))

    async def test_rejects_unsupported_asset_type(self):
        audio = os.path.join(self.temp.name, "audio.wav")
        with open(audio, "wb") as output:
            output.write(b"audio")
        base = f"http://127.0.0.1:{self.port}"
        item = {
            "metadata_json": background.encode_metadata(background.metadata_envelope(
                "test", assets={"source_texts": [{"url": base + "/bad"}]},
            ))
        }
        async with aiohttp.ClientSession() as session:
            result = await background.persist_background(
                session, audio, item, mode="all", allow_private_network=True,
            )
        self.assertEqual(result["assets"][0]["status"], "failed")
        self.assertIn("unsupported content type", result["assets"][0]["error"])

    async def test_rejects_executable_even_when_url_looks_like_html(self):
        audio = os.path.join(self.temp.name, "audio.wav")
        with open(audio, "wb") as output:
            output.write(b"audio")
        base = f"http://127.0.0.1:{self.port}"
        item = {
            "metadata_json": background.encode_metadata(background.metadata_envelope(
                "test", assets={"source_texts": [{"url": base + "/bad.html"}]},
            ))
        }
        async with aiohttp.ClientSession() as session:
            result = await background.persist_background(
                session, audio, item, mode="all", allow_private_network=True,
            )
        self.assertEqual(result["assets"][0]["status"], "failed")

    async def test_downloaded_html_is_sanitized_before_persistence(self):
        audio = os.path.join(self.temp.name, "audio.wav")
        with open(audio, "wb") as output:
            output.write(b"audio")
        base = f"http://127.0.0.1:{self.port}"
        item = {
            "metadata_json": background.encode_metadata(background.metadata_envelope(
                "test", assets={"source_texts": [{"url": base + "/source.html"}]},
            ))
        }
        async with aiohttp.ClientSession() as session:
            result = await background.persist_background(
                session, audio, item, mode="all", allow_private_network=True,
            )
        saved = result["assets"][0]
        self.assertEqual(saved["status"], "saved")
        with open(saved["path"], encoding="utf-8") as source:
            html = source.read()
        self.assertIn("Public text", html)
        self.assertNotIn("script", html)
        self.assertTrue(os.path.exists(saved["plain_text_path"]))

    async def test_rejects_asset_above_byte_limit(self):
        audio = os.path.join(self.temp.name, "audio.wav")
        with open(audio, "wb") as output:
            output.write(b"audio")
        item = self.item()
        previous = background.MAX_BACKGROUND_ASSET_BYTES
        background.MAX_BACKGROUND_ASSET_BYTES = 4
        try:
            async with aiohttp.ClientSession() as session:
                result = await background.persist_background(
                    session, audio, item, mode="all", allow_private_network=True,
                )
        finally:
            background.MAX_BACKGROUND_ASSET_BYTES = previous
        self.assertTrue(all(asset["status"] == "failed" for asset in result["assets"]))

    def test_sidecar_redacts_signed_urls_recursively(self):
        audio = os.path.join(self.temp.name, "audio.wav")
        with open(audio, "wb") as output:
            output.write(b"audio")
        item = self.item()
        metadata_path = background.save_sidecar(audio, item, "abc")
        with open(metadata_path, encoding="utf-8") as source:
            saved = json.load(source)
        serialized = json.dumps(saved, ensure_ascii=False)
        self.assertNotIn("secret", serialized)
        self.assertTrue(saved["original_url_query_redacted"])

    def test_sidecar_redacts_query_and_preserves_rich_metadata(self):
        audio = os.path.join(self.temp.name, "audio.wav")
        with open(audio, "wb") as output:
            output.write(b"audio")
        item = self.item()
        path = background.save_sidecar(audio, item, hashlib.sha256(b"audio").hexdigest())
        with open(path, encoding="utf-8") as source:
            data = json.load(source)
        self.assertNotIn("token", data["original_url"])
        self.assertNotIn("signature", data["cover_url"])
        self.assertTrue(data["original_url_query_redacted"])
        self.assertEqual(data["schema_version"], 2)
        self.assertEqual(data["background_metadata"]["schema_version"], 1)
        self.assertEqual(
            data["background_metadata"]["transcript_status"], "provided"
        )

    def test_plain_text_removes_markup(self):
        self.assertEqual(background.plain_text("<p>A &amp; <b>B</b></p>"), "A & B")

    def test_sanitize_html_removes_active_content(self):
        cleaned = background.sanitize_html(
            '<p onclick="steal()">safe<img src="javascript:steal()" '
            'onerror="steal()"><a href="data:text/html,bad">bad</a></p>'
            '<script>steal()</script><iframe src="https://bad.example"></iframe>'
        )
        self.assertIn("safe", cleaned)
        self.assertNotIn("onclick", cleaned)
        self.assertNotIn("onerror", cleaned)
        self.assertNotIn("script", cleaned)
        self.assertNotIn("iframe", cleaned)
        self.assertNotIn("javascript:", cleaned)
        self.assertNotIn("data:text", cleaned)

    def test_error_redaction_removes_signed_query(self):
        error = background._redact_error(
            "Cannot fetch https://cdn.example/audio.vtt?token=top-secret&expires=1"
        )
        self.assertEqual(error, "Cannot fetch https://cdn.example/audio.vtt")

    def test_redact_url_preserves_ipv6_brackets(self):
        self.assertEqual(
            background.redact_url("http://[2001:db8::1]:8080/file?q=secret"),
            "http://[2001:db8::1]:8080/file",
        )

    def test_bilibili_json_transcript_becomes_plain_text(self):
        value = {"body": [
            {"from": 0, "to": 1, "content": "first"},
            {"from": 1, "to": 2, "content": "second"},
        ]}
        self.assertEqual(background._json_transcript_text(value), "first\nsecond")


if __name__ == "__main__":
    unittest.main()
