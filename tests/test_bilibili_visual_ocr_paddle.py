import unittest
import tempfile
from pathlib import Path
from unittest import mock

import bilibili_visual_ocr_paddle as adapter
from bilibili_visual_ocr import SampledFrame


class FakeArray:
    ndim = 3
    shape = (100, 200, 3)


class FakeNumpy:
    uint8 = object()

    @staticmethod
    def frombuffer(payload, dtype):
        return (payload, dtype)


class FakeCv2:
    IMREAD_COLOR = 1

    @staticmethod
    def imdecode(_encoded, _mode):
        return FakeArray()


class FakeResult:
    json = {
        "res": {
            "rec_texts": ["你好", ""],
            "rec_scores": [0.97, 0.99],
            "rec_boxes": [[20, 60, 180, 90], [0, 0, 1, 1]],
        }
    }


class PaddleAdapterTests(unittest.TestCase):
    def test_model_digest_fails_closed_on_missing_cache(self):
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            RuntimeError, "cache is incomplete"
        ):
            adapter._official_model_digest(
                Path(temporary), ("det", "rec")
            )

    def test_result_adapter_emits_normalized_strict_detections(self):
        pipeline = mock.Mock()
        pipeline.predict.return_value = [FakeResult()]
        engine = adapter.PaddleOcrEngine(
            pipeline,
            cv2_module=FakeCv2,
            numpy_module=FakeNumpy,
            version="3.7.0",
        )
        detections = list(engine.recognize(SampledFrame(0, 0.0, b"png")))
        self.assertEqual(len(detections), 1)
        self.assertEqual(detections[0].text, "你好")
        self.assertEqual(detections[0].confidence, 0.97)
        self.assertEqual(detections[0].bbox_norm, (0.1, 0.6, 0.9, 0.9))
        self.assertEqual(engine.metadata["name"], "paddleocr")
        self.assertIn("PP-OCRv5_server_rec", engine.metadata["model"])

    def test_polygon_boxes_and_nested_or_plain_results_are_supported(self):
        polygon = [[10, 10], [90, 10], [90, 20], [10, 20]]
        self.assertEqual(
            adapter._normalized_box(polygon, 100, 50),
            (0.1, 0.2, 0.9, 0.4),
        )
        self.assertEqual(adapter._result_mapping({"rec_texts": []}), {"rec_texts": []})

    def test_invalid_boxes_and_result_wrappers_are_rejected(self):
        with self.assertRaises(ValueError):
            adapter._normalized_box([1, 1, 1, 2], 100, 100)
        with self.assertRaises(ValueError):
            adapter._result_mapping(object())

    def test_inconsistent_or_unbounded_result_arrays_are_rejected(self):
        pipeline = mock.Mock()
        pipeline.predict.return_value = [{
            "rec_texts": ["one"], "rec_scores": [],
            "rec_boxes": [[1, 1, 2, 2]],
        }]
        engine = adapter.PaddleOcrEngine(
            pipeline, cv2_module=FakeCv2, numpy_module=FakeNumpy,
            version="3.7.0",
        )
        with self.assertRaisesRegex(ValueError, "inconsistent lengths"):
            list(engine.recognize(SampledFrame(0, 0.0, b"png")))

        pipeline.predict.return_value = [{
            "rec_texts": ["x"] * (adapter.MAX_DETECTIONS_PER_FRAME + 1),
            "rec_scores": [0.9] * (adapter.MAX_DETECTIONS_PER_FRAME + 1),
            "rec_boxes": [[1, 1, 2, 2]] * (adapter.MAX_DETECTIONS_PER_FRAME + 1),
        }]
        with self.assertRaisesRegex(ValueError, "too many detections"):
            list(engine.recognize(SampledFrame(0, 0.0, b"png")))


if __name__ == "__main__":
    unittest.main()
