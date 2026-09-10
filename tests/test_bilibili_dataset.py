import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bilibili_dataset import (
    BilibiliClient,
    _content_range_start,
    _download_stream,
    _extract_wav,
    _file_record,
    _media_summary,
    _subprocess_env,
    audit_dataset,
    bilibili_video_id,
    caption_language_matches_content,
    build_jobs,
    select_dash_streams,
    select_caption_tracks,
    validate_bundle,
    validate_manifest,
)
from bilibili_subtitles import parse_subtitle_document, render_subtitle_text, render_subtitle_vtt
from youtube_dataset import write_json_atomic


class ManifestTests(unittest.TestCase):
    def test_caption_language_must_match_content_language(self):
        self.assertTrue(caption_language_matches_content("en", "en-US"))
        self.assertTrue(caption_language_matches_content("zh", "zh-Hans"))
        self.assertTrue(caption_language_matches_content("yue", "zh-Hant"))
        self.assertFalse(caption_language_matches_content("zh", "en"))

    def test_manifest_rejects_cross_language_caption_fallback(self):
        with self.assertRaises(ValueError):
            validate_manifest({"items": [{
                "bvid": "BV1xx411c7mD",
                "content_language": "zh",
                "languages": ["zh-Hans", "en"],
            }]})

    def test_bv_id_and_canonical_url_are_accepted(self):
        expected = "BV1xx411c7mD"
        self.assertEqual(bilibili_video_id(expected), expected)
        self.assertEqual(
            bilibili_video_id(f"https://www.bilibili.com/video/{expected}?p=2"),
            expected,
        )

    def test_unsafe_or_ambiguous_urls_are_rejected(self):
        for value in (
            "http://www.bilibili.com/video/BV1xx411c7mD",
            "https://user:secret@www.bilibili.com/video/BV1xx411c7mD",
            "https://evil.example/video/BV1xx411c7mD",
            "https://www.bilibili.com/video/not-a-bv",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bilibili_video_id(value)

    def test_manifest_is_bounded_and_normalized(self):
        items = validate_manifest({"items": [{
            "url": "https://www.bilibili.com/video/BV1xx411c7mD",
            "parts": [2, 1, 2],
            "languages": ["zh-Hans", "ai-zh"],
            "content_language": "zh",
            "max_height": 720,
            "require_caption": True,
            "rights": {"status": "needs_review"},
            "ai_generation": {"status": "unknown", "evidence": []},
        }]})
        self.assertEqual(items[0]["bvid"], "BV1xx411c7mD")
        self.assertEqual(items[0]["parts"], [2, 1])
        self.assertEqual(items[0]["max_height"], 720)
        self.assertTrue(items[0]["require_caption"])

    def test_url_page_is_inferred_and_conflicts_are_rejected(self):
        item = validate_manifest({"items": [{
            "url": "https://www.bilibili.com/video/BV1xx411c7mD?p=3",
        }]})[0]
        self.assertEqual(item["parts"], [3])
        with self.assertRaises(ValueError):
            validate_manifest({"items": [{
                "url": "https://www.bilibili.com/video/BV1xx411c7mD?p=3",
                "parts": [2],
            }]})

    def test_manifest_rejects_invalid_limits_and_duplicate_bv(self):
        invalid = [
            {"items": []},
            {"items": [{"bvid": "BV1xx411c7mD", "parts": [0]}]},
            {"items": [{"bvid": "BV1xx411c7mD", "max_height": 9999}]},
            {"items": [
                {"bvid": "BV1xx411c7mD"}, {"bvid": "BV1xx411c7mD"},
            ]},
        ]
        for document in invalid:
            with self.subTest(document=document), self.assertRaises(ValueError):
                validate_manifest(document)


class JobAndCaptionTests(unittest.TestCase):
    def test_build_jobs_selects_parts_and_stable_keys(self):
        item = validate_manifest({"items": [{
            "bvid": "BV1xx411c7mD", "parts": [2],
        }]})[0]
        view = {"pages": [
            {"page": 1, "cid": 11, "part": "one", "duration": 10},
            {"page": 2, "cid": 22, "part": "two", "duration": 20},
        ]}
        jobs = build_jobs(item, view)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["cid"], 22)
        self.assertIn("BV1xx411c7mD-p2-c22", jobs[0]["job_key"])

    def test_all_matching_caption_tracks_are_retained_and_ranked(self):
        tracks = select_caption_tracks([
            {"id": 1, "lan": "zh-Hans", "type": 0,
             "author": {"mid": 9, "name": "UP"},
             "subtitle_url": "//aisubtitle.hdslb.com/manual.json?token=x"},
            {"id": 2, "lan": "ai-zh", "type": 1,
             "subtitle_url": "//aisubtitle.hdslb.com/auto.json?token=y"},
            {"id": 3, "lan": "en-US", "type": 0,
             "subtitle_url": "//aisubtitle.hdslb.com/en.json?token=z"},
        ], ["zh-Hans", "ai-zh"])
        self.assertEqual([row["kind"] for row in tracks], ["manual", "automatic"])
        self.assertNotIn("token", tracks[0]["url_redacted"])

    def test_dash_selection_prefers_height_then_avc_and_best_audio(self):
        def stream(**values):
            return {"baseUrl": "https://cn-sz-v-1.bilivideo.com/file?token=x", **values}

        video, audio = select_dash_streams({
            "video": [
                stream(id=80, height=1080, codecid=7, bandwidth=900),
                stream(id=64, height=720, codecid=12, bandwidth=1200),
                stream(id=64, height=720, codecid=7, bandwidth=1000),
            ],
            "audio": [stream(id=30216, bandwidth=64000), stream(id=30280, bandwidth=192000)],
        }, 720)
        self.assertEqual(video["codecid"], 7)
        self.assertEqual(video["height"], 720)
        self.assertEqual(audio["id"], 30280)

    def test_dash_selection_rejects_untrusted_cdn(self):
        with self.assertRaises(ValueError):
            select_dash_streams({
                "video": [{"height": 720, "baseUrl": "https://evil.example/v"}],
                "audio": [{"bandwidth": 1, "baseUrl": "https://evil.example/a"}],
            }, 720)

    def test_content_range_parser_is_strict(self):
        self.assertEqual(_content_range_start("bytes 12-99/100"), 12)
        self.assertIsNone(_content_range_start("bytes */100"))
        self.assertIsNone(_content_range_start("garbage"))


class AuditTests(unittest.TestCase):
    def test_empty_dataset_is_a_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = audit_dataset(Path(temporary))
        self.assertGreater(report["failure_count"], 0)
        self.assertEqual(report["valid_bundle_count"], 0)

    def test_incomplete_staging_is_a_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".staging" / "job").mkdir(parents=True)
            (root / ".staging" / "job" / "failure.json").write_text(
                json.dumps({"status": "failed"}), encoding="utf-8"
            )
            report = audit_dataset(root)
        self.assertGreater(report["failure_count"], 0)
        self.assertEqual(report["incomplete_staging_count"], 1)

    def test_complete_caption_bundle_passes_and_text_tamper_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job_key = "BV1xx411c7mD-p1-c1-test"
            bundle = root / "parents" / "BV1xx411c7mD" / "p1" / job_key
            bundle.mkdir(parents=True)
            video = bundle / "source.mp4"
            subprocess.run([
                "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=black:s=320x240:r=25",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                str(video),
            ], check=True)
            audio = bundle / "audio.wav"
            _extract_wav(video, audio)
            document = {"body": [{"from": 0.0, "to": 0.9, "content": "人工字幕样例"}]}
            cues = parse_subtitle_document(document)
            raw = bundle / "captions.zh-Hans.manual.1.json"
            vtt = bundle / "captions.zh-Hans.manual.1.vtt"
            txt = bundle / "captions.zh-Hans.manual.1.txt"
            write_json_atomic(raw, document)
            vtt.write_text(render_subtitle_vtt(cues), encoding="utf-8")
            txt.write_text(render_subtitle_text(cues), encoding="utf-8")
            files = {
                "video": _file_record(video, bundle),
                "audio": _file_record(audio, bundle),
                "caption_0_json": _file_record(raw, bundle),
                "caption_0_vtt": _file_record(vtt, bundle),
                "caption_0_txt": _file_record(txt, bundle),
            }
            write_json_atomic(bundle / "metadata.json", {
                "schema_version": 1,
                "asset_type": "bilibili_parent",
                "source": "bilibili",
                "bvid": "BV1xx411c7mD",
                "aid": 2,
                "cid": 1,
                "part": 1,
                "source_id": "BV1xx411c7mD_p1",
                "canonical_url": "https://www.bilibili.com/video/BV1xx411c7mD?p=1",
                "job_key": job_key,
                "content_language": "zh",
                "declared_duration_seconds": _media_summary(video)["duration_seconds"],
                "source_metadata": {"title": "synthetic"},
                "source_streams": {
                    "video": {"id": 64, "height": 240},
                    "audio": {"id": 30216, "bandwidth": 64000},
                    "merge_mode": "synthetic",
                },
                "acquisition_policy": {"max_height": 720, "require_caption": True},
                "rights": {"status": "needs_review"},
                "rights_cleared": False,
                "ai_generation": {"status": "unknown", "evidence": []},
                "speaker_count": None,
                "speaker_count_status": "needs_review",
                "caption": {
                    "status": "downloaded", "track_count": 1,
                    "tracks": [{
                        "id_str": "1", "track_type": 0,
                        "language": "zh-Hans", "label": "中文",
                        "label_brief": "", "ai_type": 0, "ai_status": 0,
                        "author": {"mid": 9, "name": "字幕作者"},
                        "kind": "manual", "text_source": "platform_manual",
                        "selected_by_rule": "bilibili_cc_with_author",
                        "translation_kind": "normal",
                        "rule_version": "bilibili-caption-provenance-v1",
                        "cue_count": 1,
                        "files": {"json": "caption_0_json", "vtt": "caption_0_vtt", "txt": "caption_0_txt"},
                    }],
                },
                "media": _media_summary(video),
                "audio": _media_summary(audio),
                "files": files,
                "acquired_at": "2026-09-10T00:00:00+00:00",
                "toolchain": {"encoding_profile": "bilibili-mp4-wav16k-v1"},
            })
            validate_bundle(bundle / "metadata.json")
            txt.write_text("tampered\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                validate_bundle(bundle / "metadata.json")


class AuthenticationBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def test_subprocess_environment_drops_bilibili_cookie(self):
        previous = os.environ.get("BILIBILI_COOKIE")
        os.environ["BILIBILI_COOKIE"] = "SESSDATA=secret"
        try:
            self.assertNotIn("BILIBILI_COOKIE", _subprocess_env())
        finally:
            if previous is None:
                os.environ.pop("BILIBILI_COOKIE", None)
            else:
                os.environ["BILIBILI_COOKIE"] = previous
    async def test_cookie_is_sent_only_to_api_bilibili_com(self):
        class Response:
            status = 200
            headers = {}

            async def read(self):
                return b"{}"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        class Session:
            def __init__(self):
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Response()

        session = Session()
        proxy = "http://127.0.0.1:18443"
        client = BilibiliClient(
            session, auth_cookie="SESSDATA=secret", proxy=proxy
        )
        await client._json("https://api.bilibili.com/x/test")
        await client._json("https://aisubtitle.hdslb.com/file.json")
        self.assertEqual(
            session.calls[0][1]["headers"]["Cookie"], "SESSDATA=secret"
        )
        self.assertNotIn("Cookie", session.calls[1][1]["headers"])
        self.assertEqual(session.calls[0][1]["proxy"], proxy)
        self.assertEqual(session.calls[1][1]["proxy"], proxy)

    async def test_disabled_proxy_is_not_added_to_api_request(self):
        class Response:
            status = 200
            headers = {}

            async def read(self):
                return b"{}"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        class Session:
            def __init__(self):
                self.kwargs = None

            def get(self, _url, **kwargs):
                self.kwargs = kwargs
                return Response()

        session = Session()
        await BilibiliClient(session)._json("https://api.bilibili.com/x/test")
        self.assertNotIn("proxy", session.kwargs)

    async def test_dash_candidates_use_the_explicit_proxy(self):
        class Content:
            async def iter_chunked(self, _size):
                yield b"synthetic-media"

        class Response:
            def __init__(self, status):
                self.status = status
                self.headers = (
                    {"Content-Length": str(len(b"synthetic-media"))}
                    if status == 200 else {}
                )
                self.content = Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        class Session:
            def __init__(self):
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Response(503 if len(self.calls) == 1 else 200)

        proxy = "http://127.0.0.1:18443"
        stream = {
            "id": 64,
            "height": 720,
            "baseUrl": "https://first.bilivideo.com/video.m4s?token=one",
            "backupUrl": ["https://second.bilivideo.com/video.m4s?token=two"],
        }
        session = Session()
        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "bilibili_dataset.validate_public_http_url", new=mock.AsyncMock()
        ), mock.patch(
            "bilibili_dataset.shutil.disk_usage",
            return_value=mock.Mock(free=100 * 1024 * 1024 * 1024),
        ), mock.patch(
            "bilibili_dataset._media_summary",
            return_value={"video_stream_count": 1, "audio_stream_count": 0},
        ):
            await _download_stream(
                session,
                stream,
                Path(temporary) / "dash-video.bin",
                maximum=1024,
                expected_kind="video",
                proxy=proxy,
            )
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(all(call[1]["proxy"] == proxy for call in session.calls))
        self.assertTrue(all(call[1]["allow_redirects"] is False for call in session.calls))


if __name__ == "__main__":
    unittest.main()
