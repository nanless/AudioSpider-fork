import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from background import encode_metadata, metadata_envelope
import media_artifacts


class MediaArtifactTests(unittest.IsolatedAsyncioTestCase):
    async def test_youtube_worker_stays_under_shared_output_root(self):
        job = {
            "url": "https://www.youtube.com/watch?v=u7TwqpWiY5s",
            "video_id": "u7TwqpWiY5s", "profile": "youtube_interviews",
            "job_key": "u7TwqpWiY5s-youtube_interviews-en-deadbeef0000",
        }
        item = {
            "source": "youtube", "source_id": "u7TwqpWiY5s",
            "metadata_json": encode_metadata(metadata_envelope(
                "youtube", source_data={"job": job}
            )),
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "downloads" / "youtube" / "访谈"
            expected = root / job["video_id"] / job["job_key"]

            def fake_download(_job, _root, *, destination):
                self.assertEqual(destination, expected)
                destination.mkdir(parents=True)
                (destination / "source.mp4").write_bytes(b"video")
                (destination / "metadata.json").write_text("{}", encoding="utf-8")
                return destination

            with mock.patch(
                "media_artifacts.validate_youtube_manifest", return_value=[job]
            ), mock.patch(
                "media_artifacts._run_youtube_download_bounded",
                side_effect=lambda queued, base, destination: fake_download(
                    queued, base, destination=destination
                ),
            ), mock.patch("media_artifacts.validate_youtube_bundle"):
                result = await media_artifacts.download_video_bundle(item, object(), root)

        self.assertEqual(result["bundle_path"], str(expected))
        self.assertEqual(result["local_path"], str(expected / "source.mp4"))
        self.assertFalse(result["reused"])

    async def test_unknown_video_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                await media_artifacts.download_video_bundle(
                    {"source": "unknown"}, object(), Path(temp)
                )

    async def test_bilibili_current_part_must_keep_collected_cid(self):
        task = {
            "bvid": "BV1xx411c7mD", "page": 1, "parts": [1], "cid": 111,
            "caption_policy": {
                "visual_ocr_fallback": True,
                "visual_ocr_profile": "bilibili_zh_burned_in_v1",
            },
            "rights": {"status": "needs_review"},
        }
        item = {
            "source": "bilibili", "source_id": "BV1xx411c7mD_p1",
            "artifact_kind": "video_bundle",
            "metadata_json": encode_metadata(metadata_envelope(
                "bilibili", source_data={"download_task": task}
            )),
        }
        client = mock.Mock()
        client.view = mock.AsyncMock(return_value={"pages": []})
        proxy = "http://127.0.0.1:18443"
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ, {"AUDIOSPIDER_BILIBILI_PROXY": proxy}
        ), mock.patch(
            "media_artifacts.BilibiliClient", return_value=client
        ) as client_class, mock.patch(
            "media_artifacts.validate_bilibili_manifest", return_value=[{"bvid": task["bvid"]}]
        ) as validator, mock.patch(
            "media_artifacts.build_bilibili_jobs",
            return_value=[{"bvid": task["bvid"], "part": 1, "cid": 222, "job_key": "job"}],
        ), mock.patch(
            "media_artifacts.download_bilibili_job", new=mock.AsyncMock()
        ) as downloader_worker:
            with self.assertRaisesRegex(ValueError, "stored CID"):
                await media_artifacts.download_video_bundle(item, object(), Path(temp))
        downloader_worker.assert_not_awaited()
        self.assertEqual(client_class.call_args.kwargs["proxy"], proxy)
        queued_item = validator.call_args.args[0]["items"][0]
        self.assertTrue(queued_item["visual_ocr_fallback"])
        self.assertEqual(
            queued_item["visual_ocr_profile"],
            "bilibili_zh_burned_in_v1",
        )

    async def test_bilibili_transient_failure_refreshes_job_and_retries(self):
        expected = {
            "bundle_path": "/tmp/bundle",
            "local_path": "/tmp/bundle/source.mp4",
            "file_size": 1,
            "content_hash": "hash",
            "reused": False,
        }
        with (
            tempfile.TemporaryDirectory() as temp,
            mock.patch.object(media_artifacts, "BILIBILI_JOB_ATTEMPTS", 3),
            mock.patch.object(media_artifacts, "BILIBILI_RETRY_BACKOFF_SECONDS", 0),
            mock.patch(
                "media_artifacts._download_bilibili",
                new=mock.AsyncMock(side_effect=[
                    asyncio.TimeoutError(),
                    RuntimeError("all DASH CDN candidates failed: timeout"),
                    expected,
                ]),
            ) as worker,
            mock.patch("media_artifacts.asyncio.sleep", new=mock.AsyncMock()) as sleep,
        ):
            result = await media_artifacts.download_video_bundle(
                {"source": "bilibili"}, object(), Path(temp),
                allow_bilibili_cookie=True,
            )

        self.assertEqual(result, expected)
        self.assertEqual(worker.await_count, 3)
        self.assertEqual(sleep.await_count, 2)

    async def test_bilibili_policy_failure_is_not_retried(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            mock.patch.object(media_artifacts, "BILIBILI_JOB_ATTEMPTS", 3),
            mock.patch(
                "media_artifacts._download_bilibili",
                new=mock.AsyncMock(side_effect=ValueError("stored CID changed")),
            ) as worker,
        ):
            with self.assertRaisesRegex(ValueError, "stored CID"):
                await media_artifacts.download_video_bundle(
                    {"source": "bilibili"}, object(), Path(temp),
                )
        worker.assert_awaited_once()

    async def test_bundle_fingerprint_is_path_and_content_deterministic(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            for root in (Path(first), Path(second)):
                (root / "nested").mkdir()
                (root / "source.mp4").write_bytes(b"video")
                (root / "nested" / "captions.txt").write_text("hello\n", encoding="utf-8")
            observed = media_artifacts.bundle_fingerprint(Path(first))
            self.assertEqual(observed, media_artifacts.bundle_fingerprint(Path(second)))
            (Path(second) / "nested" / "captions.txt").write_text("changed\n", encoding="utf-8")
            self.assertNotEqual(observed, media_artifacts.bundle_fingerprint(Path(second)))


if __name__ == "__main__":
    unittest.main()
