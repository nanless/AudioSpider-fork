import asyncio
import hashlib
import json
import math
import os
import struct
import tempfile
import unittest
import wave
from unittest import mock
from unittest import mock

import aiohttp
from aiohttp import web

import downloader
import background
from downloader import Downloader, _consume_bilibili_cookie
from storage import AudioRecord, Storage


def make_wav(path: str):
    rate = 24000
    with wave.open(path, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        for index in range(rate // 10):
            sample = int(8000 * math.sin(2 * math.pi * 440 * index / rate))
            output.writeframesraw(struct.pack("<h", sample))


class DownloaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.wav_path = os.path.join(self.temp.name, "tone.wav")
        make_wav(self.wav_path)
        with open(self.wav_path, "rb") as source:
            self.wav_bytes = source.read()

        app = web.Application()
        app.router.add_get("/tone.wav", self.audio)
        app.router.add_get("/not-audio", self.html)
        app.router.add_get("/bad-asset", self.bad_asset)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        self.port = self.site._server.sockets[0].getsockname()[1]
        self.old_download_dir = downloader.DOWNLOAD_DIR
        downloader.DOWNLOAD_DIR = os.path.join(self.temp.name, "downloads")

    async def asyncTearDown(self):
        downloader.DOWNLOAD_DIR = self.old_download_dir
        await self.runner.cleanup()
        self.temp.cleanup()

    async def audio(self, _request):
        return web.Response(body=self.wav_bytes, content_type="audio/wav")

    async def html(self, _request):
        return web.Response(text="not audio", content_type="text/html")

    async def bad_asset(self, _request):
        return web.Response(body=b"binary", content_type="application/x-executable")

    def storage(self):
        return Storage(os.path.join(self.temp.name, f"{id(self)}.db"))

    def test_cookie_is_consumed_once_and_ignored_without_gate(self):
        with mock.patch.dict(
            os.environ, {"BILIBILI_COOKIE": "SESSDATA=sentinel"}
        ):
            self.assertEqual(_consume_bilibili_cookie(False), "")
            self.assertNotIn("BILIBILI_COOKIE", os.environ)

    def test_cookie_is_consumed_once_with_explicit_gate(self):
        with mock.patch.dict(
            os.environ, {"BILIBILI_COOKIE": "SESSDATA=sentinel"}
        ):
            self.assertEqual(
                _consume_bilibili_cookie(True), "SESSDATA=sentinel"
            )
            self.assertNotIn("BILIBILI_COOKIE", os.environ)

    def test_cookie_rejects_newlines_before_any_request(self):
        with mock.patch.dict(
            os.environ, {"BILIBILI_COOKIE": "SESSDATA=x\nCookie:y"}
        ):
            with self.assertRaisesRegex(ValueError, "newline"):
                _consume_bilibili_cookie(True)
            self.assertNotIn("BILIBILI_COOKIE", os.environ)

    async def test_end_to_end_download_conversion_and_metadata(self):
        storage = self.storage()
        url = f"http://127.0.0.1:{self.port}/tone.wav"
        storage.add_url(AudioRecord(
            url=url, source="test", title="Tone", file_format="wav",
            source_id="tone-1", language="en", category="speech",
        ))
        stats = await Downloader(
            storage, max_workers=1, convert=True,
            allow_private_network=True, min_disk_free_bytes=0,
        ).download_all(limit=1)
        self.assertEqual(stats["success"], 1)
        row = storage._get_conn().execute(
            "SELECT * FROM audio_urls WHERE url=?", (url,),
        ).fetchone()
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["file_format"], "opus")
        self.assertTrue(os.path.isfile(row["local_path"]))
        with open(row["local_path"], "rb") as audio:
            final_hash = hashlib.sha256(audio.read()).hexdigest()
        self.assertEqual(row["content_hash"], final_hash)
        meta_path = os.path.splitext(row["local_path"])[0] + ".json"
        with open(meta_path, encoding="utf-8") as source:
            meta = json.load(source)
        self.assertEqual(meta["file_format"], "opus")
        self.assertEqual(meta["content_hash"], final_hash)
        self.assertEqual(meta["content_hash_algorithm"], "sha256")

    async def test_rejects_html_response(self):
        storage = self.storage()
        url = f"http://127.0.0.1:{self.port}/not-audio"
        storage.add_url(AudioRecord(url=url, source="test", title="Bad"))
        stats = await Downloader(
            storage, max_workers=1, convert=False,
            allow_private_network=True, min_disk_free_bytes=0,
        ).download_all(limit=1)
        self.assertEqual(stats["failed"], 1)

    async def test_blocks_private_media_url_by_default(self):
        storage = self.storage()
        url = f"http://127.0.0.1:{self.port}/tone.wav"
        storage.add_url(AudioRecord(
            url=url, source="test", title="Private destination",
            file_format="wav",
        ))
        stats = await Downloader(
            storage, max_workers=1, convert=False, min_disk_free_bytes=0,
        ).download_all(limit=1)
        self.assertEqual(stats["failed"], 1)
        row = storage._get_conn().execute(
            "SELECT status FROM audio_urls WHERE url=?", (url,),
        ).fetchone()
        self.assertEqual(row["status"], "failed")

    async def test_rejects_response_above_limit(self):
        storage = self.storage()
        url = f"http://127.0.0.1:{self.port}/tone.wav"
        storage.add_url(AudioRecord(url=url, source="test", title="Too Large"))
        stats = await Downloader(
            storage, max_workers=1, convert=False,
            allow_private_network=True, max_download_bytes=100,
            min_disk_free_bytes=0,
        ).download_all(limit=1)
        self.assertEqual(stats["failed"], 1)
        self.assertFalse(any(
            name.endswith(".part")
            for _, _, files in os.walk(downloader.DOWNLOAD_DIR)
            for name in files
        ))

    async def test_background_asset_failure_does_not_fail_audio(self):
        storage = self.storage()
        url = f"http://127.0.0.1:{self.port}/tone.wav"
        metadata = background.metadata_envelope(
            "test", assets={"transcripts": [{
                "url": f"http://127.0.0.1:{self.port}/bad-asset",
                "type": "text/vtt",
            }]},
        )
        storage.add_url(AudioRecord(
            url=url, source="test", title="Tone", file_format="wav",
            metadata_json=background.encode_metadata(metadata),
        ))
        stats = await Downloader(
            storage, max_workers=1, convert=False,
            allow_private_network=True, min_disk_free_bytes=0,
        ).download_all(limit=1)
        row = storage._get_conn().execute(
            "SELECT status, local_path FROM audio_urls WHERE url=?", (url,),
        ).fetchone()
        self.assertEqual(stats["success"], 1)
        self.assertEqual(row["status"], "done")
        meta_path = os.path.splitext(row["local_path"])[0] + ".json"
        with open(meta_path, encoding="utf-8") as source:
            sidecar = json.load(source)
        self.assertEqual(sidecar["background_files"]["assets"][0]["status"], "failed")

    async def test_video_bundle_uses_shared_queue_and_source_category_root(self):
        storage = self.storage()
        url = "https://www.youtube.com/watch?v=u7TwqpWiY5s"
        storage.add_url(AudioRecord(
            url=url, source="youtube", title="Interview", file_format="mp4",
            source_id="u7TwqpWiY5s", category="访谈",
            artifact_kind="video_bundle", metadata_json="{}",
        ))
        bundle = os.path.join(
            downloader.DOWNLOAD_DIR, "youtube", "访谈", "u7TwqpWiY5s", "job"
        )
        result = {
            "local_path": os.path.join(bundle, "source.mp4"),
            "bundle_path": bundle,
            "file_format": "mp4", "file_size": 123,
            "content_hash": "a" * 64, "reused": False,
        }
        with mock.patch(
            "downloader.download_video_bundle",
            new=mock.AsyncMock(return_value=result),
        ) as worker:
            stats = await Downloader(
                storage, max_workers=1, convert=True, min_disk_free_bytes=0,
            ).download_all(limit=1)

        self.assertEqual(stats["success"], 1)
        output_root = worker.await_args.args[2]
        self.assertEqual(
            output_root,
            os.path.join(downloader.DOWNLOAD_DIR, "youtube", "访谈"),
        )
        self.assertEqual(worker.await_args.kwargs["bilibili_auth_cookie"], "")
        row = storage._get_conn().execute(
            "SELECT * FROM audio_urls WHERE url=?", (url,),
        ).fetchone()
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["artifact_kind"], "video_bundle")
        self.assertEqual(row["bundle_path"], bundle)
        self.assertEqual(row["local_path"], result["local_path"])

    async def test_bilibili_session_has_no_persistent_cookie_jar(self):
        storage = self.storage()
        url = "https://www.bilibili.com/video/BV1xx411c7mD"
        storage.add_url(AudioRecord(
            url=url, source="bilibili", title="Interview", file_format="mp4",
            source_id="BV1xx411c7mD_p1", category="访谈",
            artifact_kind="video_bundle", job_key="job", metadata_json="{}",
        ))
        bundle = os.path.join(
            downloader.DOWNLOAD_DIR, "bilibili", "访谈", "BV1xx411c7mD_p1", "job"
        )
        result = {
            "local_path": os.path.join(bundle, "source.mp4"),
            "bundle_path": bundle,
            "file_format": "mp4", "file_size": 123,
            "content_hash": "a" * 64, "reused": False,
        }

        async def inspect_session(_item, session, _root, **_kwargs):
            self.assertIsInstance(session.cookie_jar, aiohttp.DummyCookieJar)
            self.assertEqual(
                _kwargs["bilibili_auth_cookie"], "SESSDATA=sentinel"
            )
            return result

        with mock.patch.dict(
            os.environ, {"BILIBILI_COOKIE": "SESSDATA=sentinel"}
        ), mock.patch(
            "downloader.download_video_bundle", side_effect=inspect_session,
        ):
            stats = await Downloader(
                storage, max_workers=1, min_disk_free_bytes=0,
                allow_bilibili_cookie=True,
            ).download_all(limit=1)
            self.assertNotIn("BILIBILI_COOKIE", os.environ)

        self.assertEqual(stats["success"], 1)

    async def test_long_batch_renews_claims_until_downloads_finish(self):
        storage = self.storage()
        storage.add_url(AudioRecord(
            url="https://example.test/lease", source="test", file_format="wav",
        ))
        downloader_instance = Downloader(
            storage, max_workers=1, convert=False,
            allow_private_network=True, min_disk_free_bytes=0,
        )
        renewed = asyncio.Event()
        event_loop = asyncio.get_running_loop()
        original_renew = storage.renew_claims

        def renew(record_ids, worker_id, lease_seconds):
            result = original_renew(record_ids, worker_id, lease_seconds)
            event_loop.call_soon_threadsafe(renewed.set)
            return result

        async def wait_for_heartbeat(*_args):
            await asyncio.wait_for(renewed.wait(), timeout=1)

        with (
            mock.patch.object(storage, "renew_claims", side_effect=renew) as renew_mock,
            mock.patch.object(
                downloader_instance, "_lease_heartbeat_interval", return_value=0.01,
            ),
            mock.patch.object(
                downloader_instance, "_download_one", side_effect=wait_for_heartbeat,
            ),
        ):
            await downloader_instance.download_all(limit=1)

        self.assertGreaterEqual(renew_mock.call_count, 1)
        self.assertEqual(renew_mock.call_args.args[1], downloader_instance.worker_id)

if __name__ == "__main__":
    unittest.main()
