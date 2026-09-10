import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import youtube_dataset as dataset


class YoutubeDatasetTests(unittest.TestCase):
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
        self.assertTrue(interview.accepts_duration(25.5 * 60))
        self.assertTrue(interview.accepts_duration(60.6 * 60))
        self.assertFalse(interview.accepts_duration(25.5 * 60 - 0.001))
        self.assertTrue(screen.accepts_duration(0.418))
        self.assertTrue(screen.accepts_duration(29.888))

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
