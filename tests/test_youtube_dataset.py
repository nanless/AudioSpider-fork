import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import youtube_dataset as dataset


class YoutubeDatasetTests(unittest.TestCase):
    def test_manifest_caption_requirement_defaults_strict_and_changes_job_identity(self):
        base = {
            "url": "https://youtu.be/dQw4w9WgXcQ",
            "profile": "youtube_interviews",
            "content_language": "en",
            "languages": ["en"],
        }
        strict = dataset.validate_manifest({"items": [base]})[0]
        best_effort = dataset.validate_manifest({"items": [base | {"require_caption": False}]})[0]
        self.assertTrue(strict["require_caption"])
        self.assertFalse(best_effort["require_caption"])
        self.assertNotEqual(strict["job_key"], best_effort["job_key"])
        self.assertEqual(
            dataset.validate_manifest({"items": [strict]})[0]["job_key"],
            strict["job_key"],
        )
        self.assertEqual(
            dataset.validate_manifest({"items": [best_effort]})[0]["job_key"],
            best_effort["job_key"],
        )

        with self.assertRaisesRegex(ValueError, "require_caption"):
            dataset.validate_manifest({"items": [base | {"require_caption": "false"}]})

    def test_custom_destination_reuses_completed_bundle(self):
        item = {
            "profile": "youtube_interviews", "video_id": "dQw4w9WgXcQ",
            "job_key": "dQw4w9WgXcQ-youtube_interviews-en-test",
        }
        caption = dataset.CaptionSelection(
            language="en", kind="manual", text_source="platform_manual", ext="vtt"
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            destination = root / "dQw4w9WgXcQ" / item["job_key"]
            destination.mkdir(parents=True)
            sidecar = destination / "metadata.json"
            sidecar.write_text("{}", encoding="utf-8")
            with mock.patch("youtube_dataset._validate_bundle", return_value={
                "metadata": {"job_key": item["job_key"]},
            }) as validator, mock.patch(
                "youtube_dataset._import_yt_dlp",
                side_effect=AssertionError("completed destination must not redownload"),
            ):
                observed = dataset._download_item_locked(
                    item, root, {}, caption, destination=destination
                )
        self.assertEqual(observed, destination.resolve())
        validator.assert_called_once_with(sidecar.resolve())

    def test_caption_language_must_match_content_language(self):
        self.assertTrue(dataset.caption_language_matches_content("en", "en-US"))
        self.assertTrue(dataset.caption_language_matches_content("zh-Hans", "zh-CN"))
        self.assertTrue(dataset.caption_language_matches_content("yue", "zh-Hant"))
        self.assertTrue(dataset.caption_language_matches_content("ko", "ko"))
        self.assertFalse(dataset.caption_language_matches_content("en", "zh-Hans"))
        self.assertFalse(dataset.caption_language_matches_content("zh-Hans", "en"))

    def test_manifest_rejects_cross_language_caption_fallback(self):
        with self.assertRaises(ValueError):
            dataset.validate_manifest({"items": [{
                "url": "https://youtu.be/dQw4w9WgXcQ",
                "profile": "youtube_screen_clips",
                "content_language": "zh-Hans",
                "languages": ["zh-Hans", "en"],
            }]})

    def test_profile_bounds(self):
        interview = dataset.PROFILES["youtube_interviews"]
        screen = dataset.PROFILES["youtube_screen_clips"]
        screen_parent = dataset.PROFILES["youtube_screen_parents"]
        conference = dataset.PROFILES["youtube_conference_forums"]
        self.assertTrue(interview.accepts_duration(25.5 * 60))
        self.assertTrue(interview.accepts_duration(60.6 * 60))
        self.assertFalse(interview.accepts_duration(25.5 * 60 - 0.001))
        self.assertTrue(screen.accepts_duration(0.418))
        self.assertTrue(screen.accepts_duration(29.888))
        self.assertTrue(screen_parent.accepts_duration(90 * 60))
        self.assertTrue(conference.accepts_duration(3 * 60 * 60))

    def test_manifest_normalizes_classification_and_preserves_candidate_metadata(self):
        candidate_metadata = {
            "search_query": "ensemble cast full movie",
            "evidence": {"signals": ["cast interview", "multiple speakers"]},
            "unrecognized_future_field": [1, {"value": True}],
        }
        screen, conference = dataset.validate_manifest({"items": [
            {
                "url": "https://youtu.be/dQw4w9WgXcQ",
                "profile": "youtube_screen_parents",
                "dataset_category": "影视",
                "content_kind": "screen_media",
                "content_language": "en",
                "require_caption": False,
                "candidate_metadata": candidate_metadata,
            },
            {
                "url": "https://youtu.be/u7TwqpWiY5s",
                "profile": "youtube_conference_forums",
                "content_language": "en",
                "require_caption": False,
            },
        ]})
        self.assertEqual(screen["dataset_category"], "影视")
        self.assertEqual(screen["content_kind"], "screen_media")
        self.assertEqual(screen["candidate_metadata"], candidate_metadata)
        self.assertEqual(conference["dataset_category"], "会议论坛")
        self.assertEqual(conference["content_kind"], "conference_forum")

    def test_manifest_rejects_unknown_or_inconsistent_classification(self):
        base = {
            "url": "https://youtu.be/dQw4w9WgXcQ",
            "profile": "youtube_screen_parents",
            "content_language": "en",
        }
        for override in (
            {"dataset_category": "其他"},
            {"content_kind": "movie"},
            {"dataset_category": "影视", "content_kind": "conference_forum"},
        ):
            with self.subTest(override=override), self.assertRaises(ValueError):
                dataset.validate_manifest({"items": [base | override]})

    def test_legacy_profile_classification_keeps_job_identity(self):
        base = {
            "url": "https://youtu.be/dQw4w9WgXcQ",
            "profile": "youtube_interviews",
            "content_language": "en",
            "languages": ["en"],
        }
        legacy = dataset.validate_manifest({"items": [base]})[0]
        explicit = dataset.validate_manifest({"items": [base | {
            "dataset_category": "访谈",
            "content_kind": "interview_roundtable",
        }]})[0]
        self.assertEqual(legacy["dataset_category"], "访谈")
        self.assertEqual(legacy["content_kind"], "interview_roundtable")
        self.assertEqual(legacy["job_key"], explicit["job_key"])
        self.assertEqual(
            legacy["job_key"],
            "dQw4w9WgXcQ-youtube_interviews-en-fa8a0df7be67",
        )

    def test_classification_is_decoupled_from_profile_but_changes_identity(self):
        base = {
            "url": "https://youtu.be/dQw4w9WgXcQ",
            "profile": "youtube_interviews",
            "content_language": "en",
            "languages": ["en"],
        }
        default = dataset.validate_manifest({"items": [base]})[0]
        conference = dataset.validate_manifest({"items": [base | {
            "dataset_category": "会议论坛",
            "content_kind": "conference_forum",
        }]})[0]
        self.assertEqual(conference["profile"], "youtube_interviews")
        self.assertEqual(conference["dataset_category"], "会议论坛")
        self.assertNotEqual(default["job_key"], conference["job_key"])

    def test_accepts_supported_youtube_urls_only(self):
        self.assertEqual(
            dataset.youtube_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
            "dQw4w9WgXcQ",
        )
        self.assertEqual(
            dataset.youtube_video_id("https://youtu.be/dQw4w9WgXcQ?t=4"),
            "dQw4w9WgXcQ",
        )
        with self.assertRaises(ValueError):
            dataset.youtube_video_id("https://example.com/watch?v=dQw4w9WgXcQ")
        with self.assertRaises(ValueError):
            dataset.youtube_video_id("file:///etc/passwd")

    def test_validate_manifest_requires_explicit_profile(self):
        with self.assertRaises(ValueError):
            dataset.validate_manifest({"items": [{"url": "https://youtu.be/dQw4w9WgXcQ"}]})

    def test_manifest_rejects_duplicate_job_and_infinite_duration(self):
        item = {"url": "https://youtu.be/dQw4w9WgXcQ", "profile": "youtube_screen_clips"}
        with self.assertRaises(ValueError):
            dataset.validate_manifest({"items": [item, item]})
        with self.assertRaises(ValueError):
            dataset.validate_manifest({"items": [item | {"max_parent_duration_seconds": float("inf")}]})

    def test_unknown_speaker_count_is_needs_review(self):
        item = dataset.validate_manifest({"items": [{
            "url": "https://youtu.be/dQw4w9WgXcQ",
            "profile": "youtube_interviews",
            "languages": ["zh-Hans"],
        }]})[0]
        self.assertIsNone(item["speaker_count"])
        self.assertEqual(item["speaker_count_status"], "needs_review")
        self.assertEqual(item["content_language"], "zh-Hans")

    def test_verified_speaker_count_must_fit_profile(self):
        with self.assertRaises(ValueError):
            dataset.validate_manifest({"items": [{
                "url": "https://youtu.be/dQw4w9WgXcQ",
                "profile": "youtube_interviews",
                "speaker_count": 12,
                "speaker_count_status": "verified_manual",
            }]})

    def test_manual_caption_beats_automatic(self):
        info = {
            "subtitles": {"en": [{"ext": "vtt", "url": "https://cdn/sub?token=manual", "name": "English"}]},
            "automatic_captions": {"en": [{"ext": "vtt", "url": "https://cdn/auto?token=auto", "name": "English auto"}]},
        }
        selected = dataset.select_caption(info, ["en"])
        self.assertEqual(selected.kind, "manual")
        self.assertEqual(selected.text_source, "platform_manual")

    def test_automatic_caption_is_explicitly_labeled(self):
        info = {
            "subtitles": {},
            "automatic_captions": {"ja": [{"ext": "json3"}, {"ext": "vtt", "name": "Japanese"}]},
        }
        selected = dataset.select_caption(info, ["ja"])
        self.assertEqual(selected.kind, "automatic")
        self.assertEqual(selected.text_source, "platform_auto")
        self.assertEqual(selected.ext, "vtt")

    def test_language_family_fallback(self):
        info = {"subtitles": {"zh-Hant-TW": [{"ext": "vtt"}]}, "automatic_captions": {}}
        selected = dataset.select_caption(info, ["zh-Hant"])
        self.assertEqual(selected.language, "zh-Hant-TW")

    def test_redacts_query_and_fragment(self):
        self.assertEqual(
            dataset.redact_url("https://cdn.example/a.vtt?expire=1&sig=secret#x"),
            "https://cdn.example/a.vtt",
        )
        self.assertNotIn(
            "secret",
            dataset.redact_error("failed https://cdn.example/a?sig=secret\nnext"),
        )

    def test_deterministic_safe_paths(self):
        with tempfile.TemporaryDirectory() as root:
            paths = dataset.output_paths(Path(root), "youtube_interviews", "dQw4w9WgXcQ", "zh-Hans")
            self.assertTrue(str(paths["directory"]).startswith(os.path.realpath(root)))
            self.assertEqual(paths["video"].name, "source.mp4")
            self.assertEqual(paths["audio"].name, "audio.wav")
            with self.assertRaises(ValueError):
                dataset.output_paths(Path(root), "youtube_interviews", "../escape", "en")

    def test_output_paths_support_all_dataset_categories(self):
        with tempfile.TemporaryDirectory() as root:
            cases = [
                ("youtube_interviews", "访谈", "interviews"),
                ("youtube_screen_parents", "影视", "screen_sources"),
                ("youtube_conference_forums", "会议论坛", "conference_forums"),
            ]
            for profile, category, section in cases:
                with self.subTest(category=category):
                    paths = dataset.output_paths(
                        Path(root), profile, "dQw4w9WgXcQ", "en",
                        dataset_category=category,
                    )
                    self.assertEqual(paths["directory"].relative_to(Path(root).resolve()).parts[0], section)

    def test_download_options_are_bounded_and_do_not_use_cookies(self):
        options = dataset.build_download_options(Path("/tmp/job"), dataset.CaptionSelection(
            language="en", kind="automatic", text_source="platform_auto", ext="vtt", name="English",
        ))
        self.assertIn("height<=720", options["format"])
        self.assertTrue(options["noplaylist"])
        self.assertTrue(options["writeautomaticsub"])
        self.assertFalse(options["writesubtitles"])
        self.assertNotIn("cookiefile", options)
        self.assertNotIn("username", options)
        self.assertFalse(options["format"].endswith("/best"))

    def test_download_options_without_caption_do_not_request_subtitles(self):
        options = dataset.build_download_options(Path("/tmp/job"), None)
        self.assertNotIn("writesubtitles", options)
        self.assertNotIn("writeautomaticsub", options)
        self.assertNotIn("subtitleslangs", options)
        self.assertNotIn("convertsubtitles", options)

    def test_best_effort_inspection_accepts_missing_or_wrong_language_caption(self):
        base_item = dataset.validate_manifest({"items": [{
            "url": "https://youtu.be/dQw4w9WgXcQ",
            "profile": "youtube_interviews",
            "content_language": "en",
            "languages": ["en"],
            "require_caption": False,
        }]})[0]

        class FakeYDL:
            def __init__(self, _options):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def extract_info(self, _url, download=False):
                self.assert_download = download
                return self.info

            def sanitize_info(self, info):
                return info

        fake_module = mock.Mock()
        fake_module.YoutubeDL = FakeYDL

        for info, expected in [
            ({"id": "dQw4w9WgXcQ", "duration": 1800, "subtitles": {},
              "automatic_captions": {}}, "missing"),
            ({"id": "dQw4w9WgXcQ", "duration": 1800,
              "subtitles": {"fr": [{"ext": "vtt"}]},
              "automatic_captions": {}}, "no_matching_language"),
        ]:
            FakeYDL.info = info
            with self.subTest(expected=expected), mock.patch(
                "youtube_dataset._import_yt_dlp", return_value=fake_module
            ):
                observed_info, caption = dataset.inspect_item(base_item)
            self.assertIsNone(caption)
            self.assertEqual(
                dataset.caption_availability_status(
                    observed_info, base_item["languages"], caption
                ),
                expected,
            )

        strict_item = base_item | {"require_caption": True}
        FakeYDL.info = {"id": "dQw4w9WgXcQ", "duration": 1800,
                        "subtitles": {}, "automatic_captions": {}}
        with mock.patch("youtube_dataset._import_yt_dlp", return_value=fake_module), \
                self.assertRaisesRegex(ValueError, "no acceptable platform caption"):
            dataset.inspect_item(strict_item)

    def test_captionless_sidecar_has_explicit_reason_and_no_fake_text_payload(self):
        item = {
            "profile": "youtube_interviews", "video_id": "dQw4w9WgXcQ",
            "job_key": "job", "source_revision": "current",
            "content_language": "en", "languages": ["en"],
            "require_caption": False, "speaker_count": None,
            "speaker_count_status": "needs_review",
            "rights": {"status": "needs_review"},
            "ai_generation": {"status": "unknown", "evidence": []},
        }
        info = {
            "id": "dQw4w9WgXcQ", "title": "No captions", "duration": 1800,
            "subtitles": {"fr": [{"ext": "vtt"}]}, "automatic_captions": {},
        }
        files = {
            "video": {"path": "source.mp4", "bytes": 1, "sha256": "0" * 64},
            "audio": {"path": "audio.wav", "bytes": 1, "sha256": "1" * 64},
        }
        sidecar = dataset.build_parent_sidecar(
            item, info, None, files, yt_dlp_version="test"
        )
        self.assertEqual(sidecar["caption"]["status"], "no_matching_language")
        self.assertFalse(sidecar["caption"]["required"])
        self.assertIsNone(sidecar["caption"]["kind"])
        self.assertIsNone(sidecar["caption"]["text_source"])
        self.assertEqual(set(sidecar["files"]), {"video", "audio"})

    def test_captionless_download_writes_only_media_and_metadata(self):
        item = dataset.validate_manifest({"items": [{
            "url": "https://youtu.be/dQw4w9WgXcQ",
            "profile": "youtube_interviews",
            "content_language": "en",
            "languages": ["en"],
            "require_caption": False,
            "rights": {"status": "needs_review"},
        }]})[0]
        info = {
            "id": item["video_id"], "title": "No caption", "duration": 1800,
            "subtitles": {}, "automatic_captions": {},
        }
        observed_options = []

        class FakeYDL:
            def __init__(self, options):
                observed_options.append(options)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def extract_info(self, _url, download=True):
                output = Path(observed_options[-1]["outtmpl"].replace("%(ext)s", "mp4"))
                output.write_bytes(b"fake-video")
                return info

            def sanitize_info(self, value):
                return value

        fake_module = mock.Mock()
        fake_module.YoutubeDL = FakeYDL
        fake_module.version.__version__ = "test"
        video_probe = {
            "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
            "format": {"duration": "1800"},
        }
        with tempfile.TemporaryDirectory() as temp, mock.patch(
            "youtube_dataset._import_yt_dlp", return_value=fake_module
        ), mock.patch(
            "youtube_dataset._extract_wav", side_effect=lambda _video, target: target.write_bytes(b"wav")
        ), mock.patch(
            "youtube_dataset.probe_media", return_value=video_probe
        ), mock.patch("youtube_dataset._validate_bundle"):
            destination = Path(temp) / item["job_key"]
            result = dataset._download_item_locked(
                item, Path(temp), info, None, destination=destination
            )
            produced_names = sorted(path.name for path in destination.iterdir())
            metadata = json.loads(
                (destination / "metadata.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result, destination.resolve())
        self.assertNotIn("writesubtitles", observed_options[0])
        self.assertEqual(produced_names, ["audio.wav", "metadata.json", "source.mp4"])
        self.assertEqual(metadata["caption"]["status"], "missing")
        self.assertEqual(set(metadata["files"]), {"video", "audio"})

    def test_cli_accepts_repeatable_video_filter(self):
        args = dataset.build_parser().parse_args(
            ["download", "--manifest", "sources.json", "--video-id", "dQw4w9WgXcQ",
             "--video-id", "aqz-KE-bpKQ"]
        )
        self.assertEqual(args.video_id, ["dQw4w9WgXcQ", "aqz-KE-bpKQ"])
        with self.assertRaises(ValueError):
            dataset.create_clips(Path("missing"), Path("out"), max_clips=0)

    def test_clip_cli_requires_explicit_authorization_flag(self):
        with self.assertRaises(ValueError):
            dataset.main(["clip", "--parent", "missing"])
        args = dataset.build_parser().parse_args([
            "clip", "--parent", "parent", "--allow-clips",
        ])
        self.assertTrue(args.allow_clips)

    def test_atomic_json_and_hash(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "metadata.json"
            dataset.write_json_atomic(path, {"hello": "世界"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["hello"], "世界")
            self.assertEqual(len(dataset.sha256_file(path)), 64)
            self.assertFalse((Path(root) / "metadata.json.tmp").exists())

    def test_sidecar_does_not_persist_signed_caption_url(self):
        item = {
            "url": "https://youtu.be/dQw4w9WgXcQ?t=1",
            "profile": "youtube_interviews",
            "speaker_count": None,
            "speaker_count_status": "needs_review",
            "languages": ["en"],
            "content_language": "en",
            "rights": {"status": "unknown"},
        }
        info = {
            "id": "dQw4w9WgXcQ", "title": "Example", "duration": 1800,
            "description": "details https://example.test/page?token=secret",
            "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ&feature=x",
            "channel_id": "channel", "channel": "Channel",
        }
        caption = dataset.CaptionSelection(
            language="en", kind="automatic", text_source="platform_auto", ext="vtt",
            name="English", url="https://cdn.example/sub?sig=secret",
        )
        sidecar = dataset.build_parent_sidecar(item, info, caption, {}, yt_dlp_version="2026.08.19")
        serialized = json.dumps(sidecar)
        self.assertNotIn("secret", serialized)
        self.assertEqual(sidecar["caption"]["text_source"], "platform_auto")
        self.assertEqual(sidecar["speaker_count_status"], "needs_review")
        self.assertEqual(sidecar["language"], "en")

    def test_rights_need_status_and_evidence_before_clearance(self):
        item = {"rights": {"status": "creative_commons"}}
        self.assertFalse(dataset.rights_cleared(item))
        item["rights"]["evidence_url"] = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        self.assertTrue(dataset.rights_cleared(item))

    def test_manifest_rejects_unsafe_rights_evidence(self):
        with self.assertRaises(ValueError):
            dataset.validate_manifest({"items": [{
                "url": "https://youtu.be/dQw4w9WgXcQ",
                "profile": "youtube_screen_clips",
                "rights": {"status": "creative_commons", "evidence_url": "file:///etc/passwd"},
            }]})

    def test_audit_rejects_empty_dataset_and_incomplete_staging(self):
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            report = dataset.audit_dataset(root)
            self.assertGreater(report["failure_count"], 0)
            (root / ".staging" / "unfinished").mkdir(parents=True)
            report = dataset.audit_dataset(root)
            self.assertGreater(report["failure_count"], 0)
            self.assertEqual(report["incomplete_staging_count"], 1)

    def test_audit_rejects_empty_sidecar(self):
        with tempfile.TemporaryDirectory() as root_text:
            path = Path(root_text) / "screen_sources" / "id" / "en" / "job" / "metadata.json"
            path.parent.mkdir(parents=True)
            path.write_text("{}", encoding="utf-8")
            report = dataset.audit_dataset(Path(root_text))
            self.assertGreater(report["failure_count"], 0)

    def test_audit_rejects_inconsistent_caption_and_escaped_payload(self):
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            directory = root / "screen_sources" / "dQw4w9WgXcQ" / "en" / "job"
            directory.mkdir(parents=True)
            metadata = {
                "schema_version": 1,
                "asset_type": "youtube_parent",
                "profile": "youtube_screen_clips",
                "job_key": "job",
                "source_id": "dQw4w9WgXcQ",
                "duration_seconds": 10,
                "language": "en",
                "speaker_count": None,
                "speaker_count_status": "needs_review",
                "caption": {
                    "kind": "automatic", "text_source": "platform_manual",
                    "track_language": "en",
                },
                "rights": {"status": "needs_review"},
                "rights_cleared": False,
                "ai_generation": {"status": "unknown", "evidence": []},
                "files": {
                    name: {"path": "../escape", "bytes": 0, "sha256": "0" * 64}
                    for name in ("video", "audio", "caption_vtt", "transcript_txt")
                },
            }
            dataset.write_json_atomic(directory / "metadata.json", metadata)
            report = dataset.audit_dataset(root)
            self.assertEqual(report["failure_count"], 1)
            self.assertIn("caption kind", report["failures"][0]["error"])
            metadata["caption"]["text_source"] = "platform_auto"
            dataset.write_json_atomic(directory / "metadata.json", metadata)
            report = dataset.audit_dataset(root)
            self.assertEqual(report["failure_count"], 1)
            self.assertIn("escapes", report["failures"][0]["error"])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
    def test_captionless_parent_bundle_audits_without_placeholder_text(self):
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            job_key = "dQw4w9WgXcQ-youtube_interviews-en-best-effort"
            parent = root / "interviews" / "dQw4w9WgXcQ" / "en" / job_key
            parent.mkdir(parents=True)
            video = parent / "source.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=black:s=320x180:r=25:d=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=2",
                    "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    str(video),
                ],
                check=True,
            )
            audio = parent / "audio.wav"
            subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(video),
                    "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio),
                ],
                check=True,
            )
            files = {
                name: {"path": path.name, "bytes": path.stat().st_size,
                       "sha256": dataset.sha256_file(path)}
                for name, path in {"video": video, "audio": audio}.items()
            }
            dataset.write_json_atomic(parent / "metadata.json", {
                "schema_version": 1,
                "asset_type": "youtube_parent",
                "profile": "youtube_interviews",
                "job_key": job_key,
                "source_id": "dQw4w9WgXcQ",
                "duration_seconds": 2.0,
                "language": "en",
                "speaker_count": None,
                "speaker_count_status": "needs_review",
                "caption": {
                    "status": "missing", "required": False,
                    "kind": None, "text_source": None,
                    "requested_languages": ["en"], "track_language": None,
                },
                "rights": {"status": "needs_review"},
                "rights_cleared": False,
                "ai_generation": {"status": "unknown", "evidence": []},
                "files": files,
                "toolchain": {},
            })

            report = dataset.audit_dataset(root)

        self.assertEqual(report["failure_count"], 0, report["failures"])
        self.assertEqual(report["counts"]["caption_status:missing"], 1)
        self.assertNotIn("caption_kind:None", report["counts"])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
    def test_synthetic_screen_bundle_clips_and_audits(self):
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            parent = root / "screen_sources" / "dQw4w9WgXcQ" / "en" / "job"
            parent.mkdir(parents=True)
            video = parent / "source.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=black:s=320x180:r=25:d=4",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=4",
                    "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    str(video),
                ],
                check=True,
            )
            audio = parent / "audio.wav"
            subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(video),
                    "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio),
                ],
                check=True,
            )
            caption = parent / "captions.en.manual.vtt"
            caption.write_text(
                "WEBVTT\n\n00:00:00.200 --> 00:00:02.000\nhello\n\n"
                "00:00:02.000 --> 00:00:04.500\nworld\n",
                encoding="utf-8",
            )
            transcript = parent / "captions.en.manual.txt"
            transcript.write_text("legacy wrong transcript\n", encoding="utf-8")
            files = {}
            for name, path in {
                "video": video,
                "audio": audio,
                "caption_vtt": caption,
                "transcript_txt": transcript,
            }.items():
                files[name] = {
                    "path": path.name,
                    "bytes": path.stat().st_size,
                    "sha256": dataset.sha256_file(path),
                }
            dataset.write_json_atomic(
                parent / "metadata.json",
                {
                    "schema_version": 1,
                    "asset_type": "youtube_parent",
                    "profile": "youtube_screen_clips",
                    "job_key": "job",
                    "source_id": "dQw4w9WgXcQ",
                    "duration_seconds": 4.0,
                    "language": "en",
                    "speaker_count_status": "needs_review",
                    "caption": {
                        "kind": "manual", "text_source": "platform_manual",
                        "track_language": "en",
                    },
                    "rights": {"status": "needs_review"},
                    "rights_cleared": False,
                    "ai_generation": {"status": "unknown", "evidence": []},
                    "files": files,
                    "toolchain": {},
                },
            )

            repaired = dataset.repair_parent_bundle(parent)
            self.assertEqual(repaired["metadata"]["source_id"], "dQw4w9WgXcQ")
            self.assertEqual(transcript.read_text(encoding="utf-8"), "hello\nworld\n")

            clips = dataset.create_clips(parent, root, max_clips=2)
            self.assertEqual(len(clips), 1)
            for filename in ["clip.mp4", "audio.wav", "captions.vtt", "transcript.txt", "metadata.json"]:
                self.assertTrue((clips[0] / filename).is_file())
            report = dataset.audit_dataset(root)
            self.assertEqual(report["failure_count"], 0, report["failures"])
            self.assertEqual(report["clips"]["count"], 1)


if __name__ == "__main__":
    unittest.main()
