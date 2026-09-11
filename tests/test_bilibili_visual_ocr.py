import io
import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bilibili_visual_ocr as visual


MEDIA_HASH = "a" * 64
BBOX = (0.25, 0.70, 0.75, 0.82)


def detection(text="你好，世界", confidence=0.95, bbox=BBOX):
    return visual.OcrDetection(text=text, confidence=confidence, bbox_norm=bbox)


def observation(index, timestamp, *detections):
    return visual.FrameObservation(index, timestamp, tuple(detections))


def quality(cues, confidence=None, **overrides):
    result = {
        "frames_sampled": 2,
        "frames_with_candidates": 2,
        "detections_accepted": 2,
        "detections_filtered_low_confidence": 0,
        "static_tracks_filtered": 0,
        "cues_emitted": len(cues),
        "mean_confidence": confidence,
    }
    result.update(overrides)
    return result


def engine_metadata():
    return {
        "name": "fake-ocr",
        "version": "1.0.0",
        "model": "fake-zh",
        "model_version": "2026.09",
    }


def document_with_one_cue(config=None):
    config = config or visual.VisualOcrConfig(sample_fps=2.0)
    cues = [visual.VisualOcrCue(0.0, 1.0, "你好，世界", 0.95, 2, BBOX)]
    return visual.build_visual_ocr_document(
        cues=cues,
        config=config,
        engine_metadata=engine_metadata(),
        media_sha256=MEDIA_HASH,
        media_duration_seconds=2.0,
        quality=quality(cues, 0.95),
    )


def minimal_png():
    def chunk(kind, payload=b""):
        return struct.pack(">I4s", len(payload), kind) + payload + b"\0\0\0\0"

    return visual.PNG_SIGNATURE + chunk(b"IHDR") + chunk(b"IEND")


class FakeProcess:
    def __init__(self, payload, returncode=0):
        self.stdout = io.BytesIO(payload)
        self.stderr = io.BytesIO()
        self.returncode = None
        self._final_returncode = returncode
        self.killed = False

    def wait(self):
        self.returncode = self._final_returncode
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class ConfigAndAdapterTests(unittest.TestCase):
    def test_config_rejects_unsafe_or_unbounded_values(self):
        with self.assertRaises(ValueError):
            visual.VisualOcrConfig(sample_fps=0)
        with self.assertRaises(ValueError):
            visual.VisualOcrConfig(region_normalized=(0, 0.5, 2, 1))
        with self.assertRaises(ValueError):
            visual.VisualOcrConfig(profile="../../unsafe")
        with self.assertRaises(ValueError):
            visual.VisualOcrConfig(min_confidence=float("nan"))

    def test_callable_engine_exposes_only_strict_metadata(self):
        frame = visual.SampledFrame(0, 0.0, minimal_png())
        engine = visual.CallableOcrEngine(
            lambda current: [{
                "text": str(current.index), "confidence": 0.9,
                "bbox_norm": BBOX,
            }],
            **engine_metadata(),
        )
        self.assertEqual(engine.metadata, engine_metadata())
        self.assertEqual(list(engine.recognize(frame))[0]["text"], "0")
        mutated = engine.metadata
        mutated["name"] = "changed"
        self.assertEqual(engine.metadata["name"], "fake-ocr")

    def test_callable_engine_rejects_unsafe_metadata(self):
        with self.assertRaises(ValueError):
            visual.CallableOcrEngine(
                lambda _frame: [], name="fake ocr", version="1", model="m",
                model_version="1",
            )

    def test_text_normalization_is_deterministic_and_bounded(self):
        self.assertEqual(visual.clean_ocr_text("ＡＢＣ\r\n  你好\t世界 "), "ABC\n你好 世界")
        self.assertEqual(visual.clean_ocr_text("\0\n\t"), "")
        with self.assertRaises(ValueError):
            visual.clean_ocr_text("12345", maximum=4)


class EnvironmentAndFfmpegTests(unittest.TestCase):
    def test_subprocess_environment_drops_credentials_and_every_proxy(self):
        source = {
            "PATH": "/usr/bin", "LANG": "C.UTF-8",
            "BILIBILI_COOKIE": "secret", "HTTP_PROXY": "proxy",
            "https_proxy": "proxy", "SERVICE_TOKEN": "secret",
            "AWS_SECRET_ACCESS_KEY": "secret", "PODCAST_INDEX_KEY": "secret",
        }
        cleaned = visual.safe_subprocess_environment(source)
        self.assertEqual(cleaned, {"PATH": "/usr/bin", "LANG": "C.UTF-8"})

    def test_ffmpeg_command_uses_no_shell_and_records_sampling_policy(self):
        config = visual.VisualOcrConfig(sample_fps=3.0)
        command = visual.build_ffmpeg_command(Path("video name.mp4"), config)
        self.assertIsInstance(command, list)
        self.assertIn("video name.mp4", command)
        self.assertIn("image2pipe", command)
        filters = command[command.index("-vf") + 1]
        self.assertIn("fps=3.0", filters)
        self.assertIn("crop=", filters)

    def test_ffmpeg_frame_iterator_parses_concatenated_png_and_cleans_env(self):
        captured = {}
        process = FakeProcess(minimal_png() + minimal_png())

        def factory(command, **kwargs):
            captured["command"] = command
            captured.update(kwargs)
            return process

        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "source.mp4"
            video.write_bytes(b"video")
            frames = list(visual.iter_ffmpeg_frames(
                video,
                visual.VisualOcrConfig(sample_fps=2.0),
                popen_factory=factory,
                environment={"PATH": "/usr/bin", "BILIBILI_COOKIE": "secret"},
            ))
        self.assertEqual([frame.timestamp for frame in frames], [0.0, 0.5])
        self.assertNotIn("BILIBILI_COOKIE", captured["env"])
        self.assertEqual(captured["env"]["PATH"], "/usr/bin")
        self.assertFalse(process.killed)

    def test_ffmpeg_iterator_rejects_malformed_or_failed_stream(self):
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "source.mp4"
            video.write_bytes(b"video")
            with self.assertRaisesRegex(ValueError, "not PNG"):
                list(visual.iter_ffmpeg_frames(
                    video, visual.VisualOcrConfig(),
                    popen_factory=lambda *_args, **_kwargs: FakeProcess(b"not-png!"),
                ))
            with self.assertRaisesRegex(RuntimeError, "exit status 7"):
                list(visual.iter_ffmpeg_frames(
                    video, visual.VisualOcrConfig(),
                    popen_factory=lambda *_args, **_kwargs: FakeProcess(
                        minimal_png(), returncode=7
                    ),
                ))


class ObservationAndMergeTests(unittest.TestCase):
    def test_detection_run_keeps_constant_size_aggregates(self):
        run = visual._DetectionRun.create(0.0, detection(confidence=0.8))
        for index in range(1, 10_000):
            run.add(float(index), detection(confidence=1.0))

        self.assertEqual(run.observation_count, 10_000)
        self.assertAlmostEqual(run.confidence_total, 9_999.8)
        for observed, expected in zip(run.bbox, BBOX):
            self.assertAlmostEqual(observed, expected)
        self.assertFalse(hasattr(run, "bboxes"))
        self.assertFalse(hasattr(run, "confidences"))

    def test_observe_frames_filters_low_confidence_and_sorts_detections(self):
        frames = [visual.SampledFrame(0, 0.0, minimal_png())]
        engine = visual.CallableOcrEngine(
            lambda _frame: [
                {"text": "低", "confidence": 0.2, "bbox_norm": BBOX},
                {"text": "第二行", "confidence": 0.9,
                 "bbox_norm": (0.2, 0.8, 0.8, 0.9)},
                {"text": "第一行", "confidence": 0.9,
                 "bbox_norm": (0.2, 0.6, 0.8, 0.7)},
            ],
            **engine_metadata(),
        )
        observations, stats = visual.observe_frames(
            frames, engine, visual.VisualOcrConfig(min_confidence=0.8)
        )
        self.assertEqual(
            [item.text for item in observations[0].detections],
            ["第一行", "第二行"],
        )
        self.assertEqual(stats["detections_accepted"], 2)
        self.assertEqual(stats["detections_filtered_low_confidence"], 1)

    def test_observe_frames_maps_roi_boxes_back_to_full_source_frame(self):
        frame = visual.SampledFrame(0, 0.0, minimal_png())
        engine = visual.CallableOcrEngine(
            lambda _frame: [{
                "text": "字幕", "confidence": 0.95,
                "bbox_norm": (0.25, 0.20, 0.75, 0.80),
            }],
            **engine_metadata(),
        )
        observations, _ = visual.observe_frames(
            [frame], engine,
            visual.VisualOcrConfig(
                region_normalized=(0.10, 0.50, 0.90, 1.00)
            ),
        )
        self.assertEqual(
            observations[0].detections[0].bbox_norm,
            (0.30, 0.60, 0.70, 0.90),
        )

    def test_merge_tracks_fuzzy_variants_and_uses_best_supported_text(self):
        config = visual.VisualOcrConfig(
            sample_fps=2.0, min_consecutive_frames=2,
            fuzzy_text_threshold=0.70,
        )
        cues, stats = visual.merge_frame_observations([
            observation(0, 0.0, detection("你好世界", 0.90)),
            observation(1, 0.5, detection("你好世畀", 0.99)),
            observation(2, 1.0),
            observation(3, 1.5),
        ], config, 2.0)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0].text, "你好世畀")
        self.assertEqual((cues[0].start, cues[0].end), (0.0, 1.0))
        self.assertEqual(cues[0].observations, 2)
        self.assertEqual(stats["cues_emitted"], 1)

    def test_merge_filters_static_overlay_but_keeps_dynamic_caption(self):
        config = visual.VisualOcrConfig(
            sample_fps=1.0, min_consecutive_frames=2,
            static_text_min_seconds=5.0, static_text_min_coverage=0.5,
        )
        observations = []
        for index in range(10):
            items = [detection("频道水印", 0.99, (0.02, 0.05, 0.20, 0.12))]
            if index in (2, 3):
                items.append(detection("动态字幕", 0.94, BBOX))
            observations.append(observation(index, float(index), *items))
        cues, stats = visual.merge_frame_observations(observations, config, 10.0)
        self.assertEqual([cue.text for cue in cues], ["动态字幕"])
        self.assertEqual(stats["static_tracks_filtered"], 1)

    def test_merge_rejects_out_of_order_or_out_of_media_observations(self):
        config = visual.VisualOcrConfig(sample_fps=2.0)
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            visual.merge_frame_observations([
                observation(0, 0.5), observation(1, 0.5),
            ], config, 2.0)
        with self.assertRaisesRegex(ValueError, "exceeds media"):
            visual.merge_frame_observations([
                observation(0, 3.0),
            ], config, 2.0)


class DocumentAndRenderTests(unittest.TestCase):
    def test_document_has_explicit_visual_provenance_and_timing_uncertainty(self):
        document = document_with_one_cue()
        self.assertEqual(document["text_source"], "visual_ocr")
        self.assertEqual(document["extraction_kind"], "automatic")
        self.assertEqual(document["caption_authorship"], "unknown")
        self.assertEqual(document["human_review_status"], "unreviewed")
        self.assertEqual(
            document["sampling"]["timestamp_basis"],
            "ffmpeg_fps_filter_output_index",
        )
        self.assertEqual(document["sampling"]["timestamp_uncertainty_seconds"], 0.5)
        self.assertIsNone(visual.validate_visual_ocr_document(
            document, media_sha256=MEDIA_HASH,
            media_duration_seconds=2.0, content_language="zh",
        ))

    def test_rendering_is_deterministic(self):
        document = document_with_one_cue()
        expected_vtt = (
            "WEBVTT\n\n1\n00:00:00.000 --> 00:00:01.000\n"
            "你好,世界\n"
        )
        self.assertEqual(visual.render_visual_ocr_vtt(document), expected_vtt)
        self.assertEqual(visual.render_visual_ocr_vtt(document), expected_vtt)
        self.assertEqual(visual.render_visual_ocr_text(document), "你好,世界\n")

    def test_validator_rejects_provenance_hash_language_and_timing_tampering(self):
        cases = []
        document = document_with_one_cue()
        changed = json.loads(json.dumps(document))
        changed["text_source"] = "platform_manual"
        cases.append((changed, "provenance"))
        changed = json.loads(json.dumps(document))
        changed["source_media"]["sha256"] = "b" * 64
        cases.append((changed, "source_media"))
        changed = json.loads(json.dumps(document))
        changed["cues"][0]["end"] = 3.0
        cases.append((changed, "values"))
        changed = json.loads(json.dumps(document))
        changed["sampling"]["timestamp_uncertainty_seconds"] = 9.0
        cases.append((changed, "timestamp uncertainty"))
        for changed, pattern in cases:
            with self.subTest(pattern=pattern), self.assertRaisesRegex(ValueError, pattern):
                visual.validate_visual_ocr_document(
                    changed, media_sha256=MEDIA_HASH,
                    media_duration_seconds=2.0, content_language="zh",
                )
        with self.assertRaisesRegex(ValueError, "language"):
            visual.validate_visual_ocr_document(
                document, media_sha256=MEDIA_HASH,
                media_duration_seconds=2.0, content_language="en",
            )

    def test_no_stable_text_document_has_no_fake_vtt_or_txt(self):
        config = visual.VisualOcrConfig(sample_fps=2.0)
        engine = visual.CallableOcrEngine(
            lambda _frame: [], **engine_metadata()
        )
        frames = [
            visual.SampledFrame(0, 0.0, minimal_png()),
            visual.SampledFrame(1, 0.5, minimal_png()),
        ]
        document, vtt, text = visual.extract_visual_ocr(
            Path("unused.mp4"), engine, config,
            media_sha256=MEDIA_HASH, media_duration_seconds=1.0,
            frame_source=frames,
        )
        self.assertEqual(document["status"], "no_stable_text_detected")
        self.assertEqual(document["cues"], [])
        self.assertIsNone(vtt)
        self.assertIsNone(text)

    def test_validator_rejects_noncanonical_order_and_quality(self):
        document = document_with_one_cue()
        second = dict(document["cues"][0])
        second.update({"start": 0.0, "end": 0.5, "text": "更早排序"})
        document["cues"].append(second)
        document["cue_count"] = 2
        document["quality"]["cues_emitted"] = 2
        document["quality"]["mean_confidence"] = 0.95
        with self.assertRaisesRegex(ValueError, "ordered"):
            visual.validate_visual_ocr_document(
                document, media_sha256=MEDIA_HASH,
                media_duration_seconds=2.0, content_language="zh",
            )


class PreparePayloadContractTests(unittest.TestCase):
    def test_prepare_downloaded_returns_json_vtt_txt_strings(self):
        config = visual.VisualOcrConfig(sample_fps=2.0)
        engine = visual.CallableOcrEngine(
            lambda _frame: [detection()], **engine_metadata()
        )
        frames = [
            visual.SampledFrame(0, 0.0, minimal_png()),
            visual.SampledFrame(1, 0.5, minimal_png()),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "source.mp4"
            video.write_bytes(b"video")
            with mock.patch.object(
                visual, "_probe_video_duration", return_value=1.0
            ), mock.patch.object(
                visual, "iter_ffmpeg_frames", side_effect=lambda *_args: iter(frames)
            ):
                first = visual.prepare_visual_ocr_payloads(
                    video, config, engine=engine
                )
                second = visual.prepare_visual_ocr_payloads(
                    video, config, engine=engine
                )
        self.assertEqual(first, second)
        self.assertEqual(set(first), {"document", "payloads"})
        self.assertEqual(set(first["payloads"]), {"json", "vtt", "txt"})
        self.assertTrue(all(
            isinstance(value, str) for value in first["payloads"].values()
        ))
        self.assertEqual(json.loads(first["payloads"]["json"]), first["document"])

    def test_prepare_no_text_returns_json_only_and_requires_engine(self):
        config = visual.VisualOcrConfig(sample_fps=2.0)
        engine = visual.CallableOcrEngine(
            lambda _frame: [], **engine_metadata()
        )
        frames = [visual.SampledFrame(0, 0.0, minimal_png())]
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "source.mp4"
            video.write_bytes(b"video")
            with self.assertRaisesRegex(RuntimeError, "must be supplied"):
                visual.prepare_visual_ocr_payloads(video, config)
            with mock.patch.object(
                visual, "_probe_video_duration", return_value=1.0
            ), mock.patch.object(
                visual, "iter_ffmpeg_frames", return_value=iter(frames)
            ):
                result = visual.prepare_visual_ocr_payloads(
                    video, config, engine=engine
                )
        self.assertEqual(set(result["payloads"]), {"json"})
        self.assertEqual(
            result["document"]["status"], "no_stable_text_detected"
        )


if __name__ == "__main__":
    unittest.main()
