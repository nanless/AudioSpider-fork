import asyncio
import hashlib
import json
import math
import os
import struct
import tempfile
import unittest
import wave

from aiohttp import web

import downloader
from downloader import Downloader
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

    def storage(self):
        return Storage(os.path.join(self.temp.name, f"{id(self)}.db"))

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


if __name__ == "__main__":
    unittest.main()
