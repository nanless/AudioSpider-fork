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
    _consume_bilibili_cookie,
    _download_stream,
    _extract_wav,
    _file_record,
    _media_summary,
    _subprocess_env,
    _validate_inventory_attempts,
    audit_dataset,
    bilibili_video_id,
    caption_timeline_rejection,
    prepare_caption_payloads,
    redact_bilibili_error,
    download_job,
    caption_language_matches_content,
    build_jobs,
    build_job_key,
    resolve_caption_inventory,
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
        self.assertFalse(items[0]["visual_ocr_fallback"])
        self.assertEqual(
            items[0]["visual_ocr_profile"], "bilibili-visual-ocr-zh-v1"
        )

    def test_visual_ocr_policy_is_validated_and_changes_only_opt_in_job_keys(self):
        legacy = validate_manifest({"items": [{
            "bvid": "BV1xx411c7mD", "content_language": "zh",
        }]})[0]
        enabled = validate_manifest({"items": [{
            "bvid": "BV1xx411c7mD", "content_language": "zh",
            "visual_ocr_fallback": True,
            "visual_ocr_profile": "bilibili-visual-ocr-zh-v1",
        }]})[0]
        self.assertNotEqual(build_job_key(legacy, 1, 22), build_job_key(enabled, 1, 22))
        legacy_without_new_fields = dict(legacy)
        legacy_without_new_fields.pop("visual_ocr_fallback")
        legacy_without_new_fields.pop("visual_ocr_profile")
        self.assertEqual(
            build_job_key(legacy, 1, 22),
            build_job_key(legacy_without_new_fields, 1, 22),
        )
        for invalid in ("", "spaces are unsafe", "../escape"):
            with self.subTest(profile=invalid), self.assertRaises(ValueError):
                validate_manifest({"items": [{
                    "bvid": "BV1xx411c7mD", "visual_ocr_profile": invalid,
                }]})

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
    def test_caption_timeline_rejection_is_numeric_and_bounded(self):
        valid = [mock.Mock(end=8.0), mock.Mock(end=12.0)]
        invalid = [mock.Mock(end=12.25), mock.Mock(end=25.0)]
        self.assertIsNone(caption_timeline_rejection(valid, 10.0))
        self.assertEqual(caption_timeline_rejection(invalid, 10.0), {
            "reason": "caption_exceeds_media_duration",
            "media_duration_seconds": 10.0,
            "maximum_cue_end_seconds": 25.0,
            "overrun_seconds": 15.0,
            "tolerance_seconds": 2.0,
            "rule_version": "bilibili-caption-timeline-v1",
        })

    def test_caption_timeline_exact_tolerance_is_accepted(self):
        self.assertIsNone(
            caption_timeline_rejection([mock.Mock(end=12.0)], 10.0)
        )
        self.assertIsNotNone(
            caption_timeline_rejection([mock.Mock(end=12.000001)], 10.0)
        )
        self.assertIsNone(
            caption_timeline_rejection([mock.Mock(end=12.0000001)], 10.0)
        )

    def test_caption_timeline_rejects_non_finite_bool_and_extreme_values(self):
        for duration, end in (
            (True, 3.0), (10.0, True), (float("nan"), 3.0),
            (10.0, float("inf")), (10.0, 700000.0),
        ):
            with self.subTest(duration=duration, end=end), self.assertRaises(ValueError):
                caption_timeline_rejection([mock.Mock(end=end)], duration)

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


class CaptionInventoryRetryTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def job():
        return {
            "bvid": "BV1xx411c7mD", "cid": 1,
            "languages": ["zh", "ai-zh"],
        }

    async def test_invalid_first_inventory_refreshes_once_and_can_recover(self):
        invalid = {
            "status": "provided", "need_login_subtitle": False,
            "tracks": [{
                "id": 1, "lan": "ai-zh", "type": 1,
                "subtitle_url": (
                    "http://aisubtitle.hdslb.com/private/path.json?auth_key=secret"
                ),
            }],
        }
        valid = {
            "status": "provided", "need_login_subtitle": False,
            "tracks": [{
                "id": 1, "lan": "ai-zh", "type": 1,
                "subtitle_url": "//aisubtitle.hdslb.com/valid.json?auth_key=secret",
            }],
        }
        client = mock.Mock()
        client.caption_inventory = mock.AsyncMock(side_effect=[invalid, valid])
        inventory, selected, status, attempts = await resolve_caption_inventory(
            client, self.job(), retry_seconds=0
        )
        self.assertIs(inventory, valid)
        self.assertEqual(status, "downloaded")
        self.assertEqual(len(selected), 1)
        self.assertEqual(client.caption_inventory.await_count, 2)
        self.assertEqual([row["raw_track_count"] for row in attempts], [1, 1])
        self.assertNotIn("auth_key", json.dumps(attempts).lower())
        self.assertNotIn("private/path", json.dumps(attempts).lower())

    async def test_invalid_first_then_empty_stays_invalid(self):
        invalid = {
            "status": "provided", "need_login_subtitle": False,
            "tracks": [{
                "lan": "ai-zh", "type": 1,
                "subtitle_url": "http://aisubtitle.hdslb.com/one.json",
            }],
        }
        empty = {
            "status": "not_provided_publicly", "need_login_subtitle": False,
            "tracks": [],
        }
        client = mock.Mock()
        client.caption_inventory = mock.AsyncMock(side_effect=[invalid, empty])
        _, selected, status, attempts = await resolve_caption_inventory(
            client, self.job(), retry_seconds=0
        )
        self.assertEqual(selected, [])
        self.assertEqual(status, "invalid_track_inventory")
        self.assertEqual([row["raw_track_count"] for row in attempts], [1, 0])
        self.assertEqual(client.caption_inventory.await_count, 2)

    async def test_empty_first_inventory_is_not_retried(self):
        empty = {
            "status": "not_provided_publicly", "need_login_subtitle": False,
            "tracks": [],
        }
        client = mock.Mock()
        client.caption_inventory = mock.AsyncMock(return_value=empty)
        _, selected, status, attempts = await resolve_caption_inventory(
            client, self.job(), retry_seconds=0
        )
        self.assertEqual(selected, [])
        self.assertEqual(status, "not_provided_publicly")
        self.assertEqual(len(attempts), 1)
        client.caption_inventory.assert_awaited_once()

    async def test_best_effort_refresh_failure_keeps_first_safe_evidence(self):
        invalid = {
            "status": "provided", "need_login_subtitle": False,
            "tracks": [{
                "lan": "ai-zh", "type": 1,
                "subtitle_url": "http://aisubtitle.hdslb.com/one.json?token=secret",
            }],
        }
        client = mock.Mock()
        client.caption_inventory = mock.AsyncMock(side_effect=[
            invalid,
            RuntimeError("https://secret.example/path?SESSDATA=secret via proxy"),
        ])
        inventory, selected, status, attempts = await resolve_caption_inventory(
            client, self.job(), retry_seconds=0
        )
        self.assertIs(inventory, invalid)
        self.assertEqual(selected, [])
        self.assertEqual(status, "invalid_track_inventory")
        self.assertEqual(attempts[-1], {
            "attempt": 2,
            "inventory_status": "refresh_failed",
            "error_type": "RuntimeError",
        })
        self.assertNotIn("secret", json.dumps(attempts).lower())
        _validate_inventory_attempts({
            "status": status,
            "need_login_subtitle": False,
            "track_count": 0,
            "tracks": [],
            "inventory_attempt_count": 2,
            "inventory_attempts": attempts,
        })

    async def test_strict_refresh_failure_is_not_downgraded(self):
        invalid = {
            "status": "provided", "need_login_subtitle": False,
            "tracks": [{
                "lan": "ai-zh", "type": 1,
                "subtitle_url": "http://aisubtitle.hdslb.com/one.json",
            }],
        }
        client = mock.Mock()
        client.caption_inventory = mock.AsyncMock(
            side_effect=[invalid, RuntimeError("refresh failed")]
        )
        with self.assertRaises(RuntimeError):
            await resolve_caption_inventory(
                client, self.job(), retry_seconds=0, strict_refresh=True
            )


class CaptionTimelineDownloadTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _job(require_caption: bool) -> dict:
        return {
            "bvid": "BV1xx411c7mD", "aid": 2, "cid": 1, "part": 1,
            "part_title": "synthetic", "duration_seconds": 10.0,
            "job_key": "BV1xx411c7mD-p1-c1-timeline",
            "content_language": "zh", "program": "test",
            "speaker_count": None, "speaker_count_status": "needs_review",
            "rights": {"status": "needs_review"},
            "ai_generation": {"status": "unknown", "evidence": []},
            "languages": ["zh", "ai-zh"], "max_height": 720,
            "max_duration_seconds": 3600.0,
            "require_caption": require_caption, "source_revision": "test",
        }

    async def _run(
        self, root: Path, require_caption: bool, *, mixed: bool = False,
    ) -> Path:
        track = {
            "url": "https://aisubtitle.hdslb.com/auto.json",
            "url_redacted": "https://aisubtitle.hdslb.com/auto.json",
            "selection_rank": 0, "id": 1, "id_str": "1",
            "track_type": 1, "language": "ai-zh", "label": "中文自动",
            "label_brief": "", "ai_type": 0, "ai_status": 0,
            "author": None, "kind": "automatic",
            "text_source": "platform_auto", "translation_kind": "normal",
            "selected_by_rule": "bilibili_type_ai",
            "rule_version": "bilibili-caption-provenance-v1",
        }
        tracks = [track]
        documents = [{"body": [{
            "from": 0.0, "to": 50.0, "content": "错配原始字幕",
        }]}]
        if mixed:
            valid = dict(track)
            valid.update({
                "url": "https://aisubtitle.hdslb.com/manual.json",
                "url_redacted": "https://aisubtitle.hdslb.com/manual.json",
                "id": 2, "id_str": "2", "track_type": 0,
                "language": "zh-Hans", "label": "中文人工",
                "author": {"mid": 9, "name": "UP"}, "kind": "manual",
                "text_source": "platform_manual",
                "selected_by_rule": "bilibili_cc_with_author",
            })
            tracks.insert(0, valid)
            documents.insert(0, {"body": [{
                "from": 0.0, "to": 8.0, "content": "有效人工字幕",
            }]})
        inventory = {
            "status": "provided", "need_login_subtitle": False,
            "tracks": [],
        }
        diagnostics = [{
            "index": index, "track_value_type": "dict",
            "id_str": selected["id_str"], "language": selected["language"],
            "track_type": selected["track_type"],
            "ai_type": selected["ai_type"],
            "ai_status": selected["ai_status"], "is_lock": None,
            "url_present": True, "url_value_type": "str",
            "url_form": "https", "url_host": "aisubtitle.hdslb.com",
            "url_port": None, "rejection_reason": "accepted",
        } for index, selected in enumerate(tracks)]
        attempts = [{
            "attempt": 1, "inventory_status": "provided",
            "need_login_subtitle": False, "raw_track_count": len(tracks),
            "selected_track_count": len(tracks), "selection_status": "downloadable",
            "tracks": diagnostics,
        }]
        client = mock.Mock(authenticated=True, session=mock.Mock())
        client.dash = mock.AsyncMock(return_value={})
        client.subtitle = mock.AsyncMock(side_effect=documents)

        async def fake_stream(_session, _stream, path, **_kwargs):
            path.write_bytes(b"representation")

        def fake_merge(_video, _audio, output):
            output.write_bytes(b"video")
            return "copy"

        def fake_extract(_video, output):
            output.write_bytes(b"audio")

        video_summary = {
            "duration_seconds": 10.0, "video_stream_count": 1,
            "audio_stream_count": 1, "video_codec": "h264",
            "width": 320, "height": 240, "audio_codec": "aac",
            "sample_rate": 48000, "channels": 2,
        }
        audio_summary = {
            "duration_seconds": 10.0, "video_stream_count": 0,
            "audio_stream_count": 1, "video_codec": None,
            "width": None, "height": None, "audio_codec": "pcm_s16le",
            "sample_rate": 16000, "channels": 1,
        }
        with mock.patch(
            "bilibili_dataset.resolve_caption_inventory",
            mock.AsyncMock(return_value=(inventory, tracks, "downloaded", attempts)),
        ), mock.patch(
            "bilibili_dataset.select_dash_streams",
            return_value=({"id": 64, "height": 240}, {"id": 30216}),
        ), mock.patch(
            "bilibili_dataset._download_stream", side_effect=fake_stream,
        ), mock.patch(
            "bilibili_dataset._merge_dash", side_effect=fake_merge,
        ), mock.patch(
            "bilibili_dataset._extract_wav", side_effect=fake_extract,
        ), mock.patch(
            "bilibili_dataset._media_summary",
            side_effect=lambda path: (
                audio_summary if path.name == "audio.wav" else video_summary
            ),
        ):
            return await download_job(
                client, self._job(require_caption), {"title": "synthetic"}, root,
                destination=(
                    root / "BV1xx411c7mD_p1"
                    / "BV1xx411c7mD-p1-c1-timeline"
                ),
            )

    async def test_best_effort_keeps_video_and_quarantines_bad_raw_caption(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = await self._run(Path(temporary), False)
            metadata = json.loads(
                (destination / "metadata.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["caption"]["status"], "invalid_timeline")
            self.assertEqual(metadata["caption"]["payload_status"], "rejected")
            self.assertEqual(metadata["caption"]["tracks"], [])
            self.assertEqual(len(list(destination.glob("*.rejected.json"))), 1)
            self.assertEqual(len(list(destination.glob("*.vtt"))), 0)

    async def test_strict_rejects_bundle_with_only_bad_timeline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "no acceptable"):
                await self._run(root, True)
            self.assertFalse(
                (root / "BV1xx411c7mD_p1" / "BV1xx411c7mD-p1-c1-timeline").exists()
            )
            self.assertTrue(
                (root / ".staging" / "BV1xx411c7mD-p1-c1-timeline" / "failure.json").is_file()
            )

    async def test_strict_mixed_tracks_keeps_valid_and_quarantines_bad(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = await self._run(Path(temporary), True, mixed=True)
            metadata = json.loads(
                (destination / "metadata.json").read_text(encoding="utf-8")
            )
        self.assertEqual(metadata["caption"]["status"], "downloaded")
        self.assertEqual(metadata["caption"]["payload_status"], "partial")
        self.assertEqual(metadata["caption"]["track_count"], 1)
        self.assertEqual(metadata["caption"]["rejected_track_count"], 1)
        self.assertEqual(metadata["caption"]["tracks"][0]["status"], "downloaded")
        self.assertEqual(
            metadata["caption"]["rejected_tracks"][0]["status"], "rejected"
        )

    async def test_duplicate_platform_track_identity_uses_unique_filenames(self):
        track = {
            "url": "https://aisubtitle.hdslb.com/a.json",
            "url_redacted": "https://aisubtitle.hdslb.com/a.json",
            "id_str": "same", "language": "ai-zh", "kind": "automatic",
        }
        client = mock.Mock()
        client.subtitle = mock.AsyncMock(side_effect=[
            {"body": [{"from": 0.0, "to": 1.0, "content": "一"}]},
            {"body": [{"from": 0.0, "to": 2.0, "content": "二"}]},
        ])
        with tempfile.TemporaryDirectory() as temporary:
            accepted, rejected = await prepare_caption_payloads(
                client, [track, dict(track)], Path(temporary), 10.0
            )
            names = [
                path.name for item in accepted for path in item["paths"].values()
            ]
        self.assertEqual(rejected, [])
        self.assertEqual(len(names), len(set(names)))


class AuditTests(unittest.TestCase):
    def test_legacy_schema_v1_caption_without_attempts_remains_valid(self):
        _validate_inventory_attempts({"status": "not_provided_publicly"})

    def test_inventory_diagnostic_text_fields_are_strict_enums(self):
        diagnostic = {
            "index": 0, "track_value_type": "dict", "id_str": "1",
            "language": "ai-zh", "track_type": 1, "ai_type": 0,
            "ai_status": 0, "is_lock": False, "url_present": True,
            "url_value_type": "str", "url_form": "scheme_relative",
            "url_host": "aisubtitle.hdslb.com", "url_port": None,
            "rejection_reason": "accepted",
        }
        base = {
            "status": "downloaded",
            "need_login_subtitle": False,
            "track_count": 1,
            "tracks": [{
                "id_str": "1", "language": "ai-zh", "track_type": 1,
                "ai_type": 0, "ai_status": 0, "is_lock": False,
            }],
            "inventory_attempt_count": 1,
            "inventory_attempts": [{
                "attempt": 1, "inventory_status": "provided",
                "need_login_subtitle": False, "raw_track_count": 1,
                "selected_track_count": 1, "selection_status": "downloadable",
                "tracks": [diagnostic],
            }],
        }
        _validate_inventory_attempts(base)
        for field in (
            "track_value_type", "url_value_type", "url_form", "rejection_reason",
        ):
            changed = json.loads(json.dumps(base))
            changed["inventory_attempts"][0]["tracks"][0][field] = "arbitrary/path?token=x"
            with self.subTest(field=field), self.assertRaises(ValueError):
                _validate_inventory_attempts(changed)

    def test_inventory_diagnostics_cross_check_top_level_caption(self):
        caption = {
            "status": "invalid_track_inventory",
            "need_login_subtitle": False,
            "track_count": 0,
            "tracks": [],
            "inventory_attempt_count": 2,
            "inventory_attempts": [{
                "attempt": 1, "inventory_status": "provided",
                "need_login_subtitle": False, "raw_track_count": 1,
                "selected_track_count": 0,
                "selection_status": "invalid_track_inventory",
                "tracks": [{
                    "index": 0, "track_value_type": "dict", "id_str": "1",
                    "language": "ai-zh", "track_type": 1, "ai_type": 0,
                    "ai_status": 0, "is_lock": False, "url_present": True,
                    "url_value_type": "str", "url_form": "http",
                    "url_host": "aisubtitle.hdslb.com", "url_port": None,
                    "rejection_reason": "unsupported_scheme",
                }],
            }, {
                "attempt": 2, "inventory_status": "not_provided_publicly",
                "need_login_subtitle": False, "raw_track_count": 0,
                "selected_track_count": 0,
                "selection_status": "not_provided_publicly", "tracks": [],
            }],
        }
        _validate_inventory_attempts(caption)
        for field, value in (
            ("status", "not_provided_publicly"),
            ("need_login_subtitle", True),
            ("track_count", 1),
        ):
            changed = json.loads(json.dumps(caption))
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                _validate_inventory_attempts(changed)

    def test_invalid_timeline_cross_checks_selected_inventory(self):
        caption = {
            "status": "invalid_timeline",
            "need_login_subtitle": False,
            "track_count": 0,
            "tracks": [],
            "payload_status": "rejected",
            "rejected_track_count": 1,
            "rejected_tracks": [{
                "id_str": "1", "language": "ai-zh", "track_type": 1,
                "ai_type": 0, "ai_status": 0, "is_lock": False,
            }],
            "inventory_attempt_count": 1,
            "inventory_attempts": [{
                "attempt": 1, "inventory_status": "provided",
                "need_login_subtitle": False, "raw_track_count": 1,
                "selected_track_count": 1, "selection_status": "downloadable",
                "tracks": [{
                    "index": 0, "track_value_type": "dict", "id_str": "1",
                    "language": "ai-zh", "track_type": 1, "ai_type": 0,
                    "ai_status": 0, "is_lock": False, "url_present": True,
                    "url_value_type": "str", "url_form": "scheme_relative",
                    "url_host": "aisubtitle.hdslb.com", "url_port": None,
                    "rejection_reason": "accepted",
                }],
            }],
        }
        _validate_inventory_attempts(caption)
        for field, value in (
            ("track_count", 1),
            ("payload_status", "partial"),
            ("rejected_track_count", 2),
        ):
            changed = json.loads(json.dumps(caption))
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                _validate_inventory_attempts(changed)

    def test_invalid_timeline_cannot_use_legacy_inventory_compatibility(self):
        with self.assertRaisesRegex(ValueError, "rejection evidence"):
            _validate_inventory_attempts({
                "status": "invalid_timeline", "track_count": 0, "tracks": [],
                "payload_status": "rejected", "rejected_track_count": 1,
                "rejected_tracks": [{"id_str": "1", "language": "ai-zh"}],
            })

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
                    "need_login_subtitle": False,
                    "inventory_attempt_count": 1,
                    "inventory_attempts": [{
                        "attempt": 1,
                        "inventory_status": "provided",
                        "need_login_subtitle": False,
                        "raw_track_count": 1,
                        "selected_track_count": 1,
                        "selection_status": "downloadable",
                        "tracks": [{
                            "index": 0,
                            "track_value_type": "dict",
                            "id_str": "1",
                            "language": "zh-Hans",
                            "track_type": 0,
                            "ai_type": 0,
                            "ai_status": 0,
                            "is_lock": False,
                            "url_present": True,
                            "url_value_type": "str",
                            "url_form": "scheme_relative",
                            "url_host": "aisubtitle.hdslb.com",
                            "url_port": None,
                            "rejection_reason": "accepted",
                        }],
                    }],
                    "tracks": [{
                        "id_str": "1", "track_type": 0,
                        "url_redacted": "https://aisubtitle.hdslb.com/manual.json",
                        "language": "zh-Hans", "label": "中文",
                        "label_brief": "", "ai_type": 0, "ai_status": 0,
                        "author": {"mid": 9, "name": "字幕作者"},
                        "is_lock": False,
                        "kind": "manual", "text_source": "platform_manual",
                        "selected_by_rule": "bilibili_cc_with_author",
                        "translation_kind": "normal",
                        "rule_version": "bilibili-caption-provenance-v1",
                        "status": "downloaded",
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
            extra = bundle / "extra"
            extra.mkdir()
            (extra / "secret.bin").write_bytes(b"unrecorded")
            with self.assertRaises(ValueError):
                validate_bundle(bundle / "metadata.json")
            (extra / "secret.bin").unlink()
            extra.rmdir()
            if hasattr(os, "mkfifo"):
                fifo = bundle / "unexpected.pipe"
                os.mkfifo(fifo)
                with self.assertRaises(ValueError):
                    validate_bundle(bundle / "metadata.json")
                fifo.unlink()
            raw_real = raw.with_suffix(".real")
            raw.rename(raw_real)
            raw.symlink_to(raw_real.name)
            with self.assertRaises(ValueError):
                validate_bundle(bundle / "metadata.json")
            raw.unlink()
            raw_real.rename(raw)
            validate_bundle(bundle / "metadata.json")
            metadata = json.loads((bundle / "metadata.json").read_text(encoding="utf-8"))
            metadata["caption"]["tracks"][0]["status"] = "rejected"
            write_json_atomic(bundle / "metadata.json", metadata)
            with self.assertRaises(ValueError):
                validate_bundle(bundle / "metadata.json")
            metadata["caption"]["tracks"][0]["status"] = "downloaded"
            metadata["caption"]["tracks"][0]["timeline_rejection"] = {
                "reason": "caption_exceeds_media_duration"
            }
            write_json_atomic(bundle / "metadata.json", metadata)
            with self.assertRaises(ValueError):
                validate_bundle(bundle / "metadata.json")
            metadata["caption"]["tracks"][0].pop("timeline_rejection")
            write_json_atomic(bundle / "metadata.json", metadata)
            validate_bundle(bundle / "metadata.json")
            metadata["caption"]["tracks"][0]["ai_status"] = 99
            write_json_atomic(bundle / "metadata.json", metadata)
            with self.assertRaisesRegex(ValueError, "final inventory evidence"):
                validate_bundle(bundle / "metadata.json")
            metadata["caption"]["tracks"][0]["ai_status"] = 0
            write_json_atomic(bundle / "metadata.json", metadata)
            validate_bundle(bundle / "metadata.json")
            metadata = json.loads((bundle / "metadata.json").read_text(encoding="utf-8"))
            metadata["caption"]["inventory_attempts"][0]["tracks"][0]["url_host"] = (
                "aisubtitle.hdslb.com/private?token=secret"
            )
            write_json_atomic(bundle / "metadata.json", metadata)
            with self.assertRaises(ValueError):
                validate_bundle(bundle / "metadata.json")
            metadata["caption"]["inventory_attempts"][0]["tracks"][0]["url_host"] = (
                "aisubtitle.hdslb.com"
            )
            write_json_atomic(bundle / "metadata.json", metadata)
            txt.write_text("tampered\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                validate_bundle(bundle / "metadata.json")

            rejected_track = dict(metadata["caption"]["tracks"][0])
            for path in (raw, vtt, txt):
                path.unlink()
            for key in ("caption_0_json", "caption_0_vtt", "caption_0_txt"):
                metadata["files"].pop(key)
            duration = metadata["media"]["duration_seconds"]
            rejected_document = {"body": [{
                "from": 0.0,
                "to": duration + 10.0,
                "content": "时间轴错配原始字幕",
            }]}
            rejected_raw = bundle / "captions.zh-Hans.manual.1.rejected.json"
            write_json_atomic(rejected_raw, rejected_document)
            metadata["files"]["caption_rejected_0_json"] = _file_record(
                rejected_raw, bundle
            )
            rejected_track.update({
                "status": "rejected",
                "cue_count": 1,
                "files": {"json": "caption_rejected_0_json"},
                "timeline_rejection": caption_timeline_rejection(
                    parse_subtitle_document(rejected_document), duration
                ),
            })
            metadata["acquisition_policy"]["require_caption"] = False
            metadata["caption"].update({
                "status": "invalid_timeline",
                "track_count": 0,
                "tracks": [],
                "payload_status": "rejected",
                "rejected_track_count": 1,
                "rejected_tracks": [rejected_track],
            })
            write_json_atomic(bundle / "metadata.json", metadata)
            validate_bundle(bundle / "metadata.json")
            metadata["caption"]["rejected_tracks"][0]["ai_status"] = 99
            write_json_atomic(bundle / "metadata.json", metadata)
            with self.assertRaisesRegex(ValueError, "final inventory evidence"):
                validate_bundle(bundle / "metadata.json")
            metadata["caption"]["rejected_tracks"][0]["ai_status"] = 0
            write_json_atomic(bundle / "metadata.json", metadata)
            validate_bundle(bundle / "metadata.json")
            metadata["caption"]["rejected_tracks"][0]["timeline_rejection"][
                "media_duration_seconds"
            ] += 1.0
            write_json_atomic(bundle / "metadata.json", metadata)
            with self.assertRaises(ValueError):
                validate_bundle(bundle / "metadata.json")


class AuthenticationBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def test_failure_text_redacts_signed_query_and_loopback_proxy(self):
        message = (
            "request https://cdn.bilivideo.com/a.m4s?token=secret "
            "through http://127.0.0.1:18798 failed"
        )
        redacted = redact_bilibili_error(message)
        self.assertNotIn("secret", redacted)
        self.assertNotIn("127.0.0.1", redacted)
        self.assertIn("https://cdn.bilivideo.com/a.m4s", redacted)
        self.assertIn("[bilibili-loopback-proxy]", redacted)

    def test_ambient_cookie_is_consumed_but_ignored_without_gate(self):
        with mock.patch.dict(os.environ, {"BILIBILI_COOKIE": "SESSDATA=secret"}):
            self.assertEqual(_consume_bilibili_cookie(False), "")
            self.assertNotIn("BILIBILI_COOKIE", os.environ)

    def test_cookie_is_used_only_with_explicit_gate(self):
        with mock.patch.dict(os.environ, {"BILIBILI_COOKIE": "SESSDATA=secret"}):
            self.assertEqual(
                _consume_bilibili_cookie(True), "SESSDATA=secret"
            )
            self.assertNotIn("BILIBILI_COOKIE", os.environ)

    def test_authorized_cookie_still_rejects_newlines(self):
        with mock.patch.dict(os.environ, {"BILIBILI_COOKIE": "SESSDATA=x\nCookie:y"}):
            with self.assertRaisesRegex(ValueError, "newline"):
                _consume_bilibili_cookie(True)

    def test_subprocess_environment_drops_bilibili_cookie(self):
        previous = os.environ.get("BILIBILI_COOKIE")
        previous_proxy = os.environ.get("AUDIOSPIDER_BILIBILI_PROXY")
        os.environ["BILIBILI_COOKIE"] = "SESSDATA=secret"
        os.environ["AUDIOSPIDER_BILIBILI_PROXY"] = "http://127.0.0.1:18443"
        try:
            self.assertNotIn("BILIBILI_COOKIE", _subprocess_env())
            self.assertNotIn("AUDIOSPIDER_BILIBILI_PROXY", _subprocess_env())
        finally:
            if previous is None:
                os.environ.pop("BILIBILI_COOKIE", None)
            else:
                os.environ["BILIBILI_COOKIE"] = previous
            if previous_proxy is None:
                os.environ.pop("AUDIOSPIDER_BILIBILI_PROXY", None)
            else:
                os.environ["AUDIOSPIDER_BILIBILI_PROXY"] = previous_proxy
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
