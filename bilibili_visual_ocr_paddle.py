#!/usr/bin/env python3
"""Optional PaddleOCR adapter for :mod:`bilibili_visual_ocr`.

The deterministic OCR core deliberately has no ML dependency.  This adapter is
imported only after an operator explicitly enables visual OCR.  Model/package
installation and warm-up belong to the isolated ``audiospider-ocr`` Conda
environment. The adapter never installs packages. PaddleOCR may retrieve a
missing official model during construction, so production setup must run the
repository bootstrap/warm-up step before a long batch.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import hashlib
from pathlib import Path
import threading
from typing import Any

from bilibili_visual_ocr import OcrDetection, SampledFrame


DEFAULT_DETECTION_MODEL = "PP-OCRv5_server_det"
DEFAULT_RECOGNITION_MODEL = "PP-OCRv5_server_rec"
MAX_DETECTIONS_PER_FRAME = 256
_ENGINE: "PaddleOcrEngine | None" = None
_ENGINE_KEY: tuple[str, str, str] | None = None
_ENGINE_LOCK = threading.Lock()


def _official_model_digest(cache_root: Path, model_names: tuple[str, str]) -> str:
    digest = hashlib.sha256()
    for model_name in model_names:
        model_dir = Path(cache_root) / "official_models" / model_name
        required = [
            model_dir / "inference.json",
            model_dir / "inference.pdiparams",
            model_dir / "inference.yml",
        ]
        if any(path.is_symlink() or not path.is_file() for path in required):
            raise RuntimeError("PaddleOCR official model cache is incomplete")
        for path in required:
            digest.update(model_name.encode())
            digest.update(b"\0")
            digest.update(path.name.encode())
            digest.update(b"\0")
            digest.update(str(path.stat().st_size).encode("ascii"))
            digest.update(b"\0")
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


def _result_mapping(value: Any) -> Mapping[str, Any]:
    """Normalize PaddleX/PaddleOCR result wrappers without serializing arrays."""

    if isinstance(value, Mapping):
        payload: Any = value
    else:
        payload = getattr(value, "json", None)
        if callable(payload):
            payload = payload()
        if payload is None:
            payload = getattr(value, "res", None)
        if not isinstance(payload, Mapping):
            raise ValueError("PaddleOCR returned an unsupported result object")
    nested = payload.get("res")
    return nested if isinstance(nested, Mapping) else payload


def _sequence(value: Any) -> Any:
    """Return an array-like value without NumPy's ambiguous truth testing."""

    return [] if value is None else value


def _normalized_box(value: Any, width: int, height: int) -> tuple[float, float, float, float]:
    if hasattr(value, "tolist"):
        value = value.tolist()
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("PaddleOCR returned an invalid recognition box")
    if len(value) == 4 and all(isinstance(item, (int, float)) for item in value):
        x1, y1, x2, y2 = (float(item) for item in value)
    else:
        points = []
        for point in value:
            if hasattr(point, "tolist"):
                point = point.tolist()
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                raise ValueError("PaddleOCR returned an invalid recognition polygon")
            points.append((float(point[0]), float(point[1])))
        x1, x2 = min(point[0] for point in points), max(point[0] for point in points)
        y1, y2 = min(point[1] for point in points), max(point[1] for point in points)
    x1, x2 = max(0.0, x1), min(float(width), x2)
    y1, y2 = max(0.0, y1), min(float(height), y2)
    if x2 <= x1 or y2 <= y1:
        raise ValueError("PaddleOCR returned a degenerate recognition box")
    return (x1 / width, y1 / height, x2 / width, y2 / height)


class PaddleOcrEngine:
    """Concrete OCR adapter around one already constructed PaddleOCR pipeline."""

    def __init__(
        self,
        pipeline: Any,
        *,
        cv2_module: Any,
        numpy_module: Any,
        version: str,
        detection_model: str = DEFAULT_DETECTION_MODEL,
        recognition_model: str = DEFAULT_RECOGNITION_MODEL,
        model_version: str | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._cv2 = cv2_module
        self._numpy = numpy_module
        self._metadata = {
            "name": "paddleocr",
            "version": str(version),
            "model": f"{detection_model}+{recognition_model}",
            "model_version": str(model_version or version),
        }
        self._recognize_lock = threading.Lock()

    @property
    def metadata(self) -> Mapping[str, str]:
        return dict(self._metadata)

    def recognize(self, frame: SampledFrame) -> Iterable[OcrDetection]:
        encoded = self._numpy.frombuffer(frame.image_bytes, dtype=self._numpy.uint8)
        image = self._cv2.imdecode(encoded, self._cv2.IMREAD_COLOR)
        if image is None or getattr(image, "ndim", 0) != 3:
            raise ValueError("PaddleOCR could not decode the sampled PNG")
        height, width = int(image.shape[0]), int(image.shape[1])
        # Paddle predictors are not assumed thread-safe.  AudioSpider may have
        # several download workers, but one L4 OCR model must run at a time.
        with self._recognize_lock:
            results = list(self._pipeline.predict(image))
        detections: list[OcrDetection] = []
        for raw_result in results:
            result = _result_mapping(raw_result)
            texts = list(_sequence(result.get("rec_texts")))
            scores = list(_sequence(result.get("rec_scores")))
            boxes = result.get("rec_boxes")
            if boxes is None:
                boxes = _sequence(result.get("rec_polys"))
            boxes = list(boxes)
            if not (len(texts) == len(scores) == len(boxes)):
                raise ValueError("PaddleOCR result arrays have inconsistent lengths")
            if len(texts) > MAX_DETECTIONS_PER_FRAME:
                raise ValueError("PaddleOCR returned too many detections for one frame")
            for text, score, box in zip(texts, scores, boxes):
                if not str(text).strip():
                    continue
                detections.append(OcrDetection(
                    text=str(text),
                    confidence=float(score),
                    bbox_norm=_normalized_box(box, width, height),
                ))
        return detections


def create_paddle_engine(
    *,
    device: str = "gpu:0",
    detection_model: str = DEFAULT_DETECTION_MODEL,
    recognition_model: str = DEFAULT_RECOGNITION_MODEL,
    allow_model_download: bool = False,
) -> PaddleOcrEngine:
    """Construct one pinned PaddleOCR pipeline; never install dependencies.

    PaddleOCR itself can populate a missing official-model cache here. The
    repository bootstrap calls this once with the official BOS source so
    ordinary batch workers start from an already warmed cache.
    """

    import cv2
    import numpy
    import paddle
    import paddleocr
    import paddlex
    from paddlex.inference.utils.official_models import CACHE_DIR
    from paddleocr import PaddleOCR

    if not isinstance(allow_model_download, bool):
        raise TypeError("allow_model_download must be boolean")
    model_names = (detection_model, recognition_model)
    model_digest = None
    if not allow_model_download:
        # Fail closed before PaddleOCR construction so an ordinary data job
        # never performs a surprise network model download.
        model_digest = _official_model_digest(Path(CACHE_DIR), model_names)

    pipeline = PaddleOCR(
        text_detection_model_name=detection_model,
        text_recognition_model_name=recognition_model,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        device=device,
        engine="paddle",
        text_det_thresh=0.3,
        text_det_box_thresh=0.6,
        text_det_unclip_ratio=1.5,
        text_rec_score_thresh=0.0,
    )
    if model_digest is None:
        model_digest = _official_model_digest(Path(CACHE_DIR), model_names)
    runtime_version = (
        f"ocr-{paddleocr.__version__}+pdx-{paddlex.__version__}+"
        f"paddle-{paddle.__version__}+cv-{cv2.__version__}"
    )
    return PaddleOcrEngine(
        pipeline,
        cv2_module=cv2,
        numpy_module=numpy,
        version=runtime_version,
        detection_model=(
            f"{detection_model}-det0.3-box0.6-unclip1.5"
        ),
        recognition_model=f"{recognition_model}-rec0.0",
        model_version=f"sha256-{model_digest}",
    )


def get_paddle_engine(
    *,
    device: str = "gpu:0",
    detection_model: str = DEFAULT_DETECTION_MODEL,
    recognition_model: str = DEFAULT_RECOGNITION_MODEL,
) -> PaddleOcrEngine:
    """Return one process-wide model instance, serialized for a single L4."""

    global _ENGINE, _ENGINE_KEY
    key = (device, detection_model, recognition_model)
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = create_paddle_engine(
                device=device,
                detection_model=detection_model,
                recognition_model=recognition_model,
            )
            _ENGINE_KEY = key
        elif _ENGINE_KEY != key:
            raise RuntimeError("PaddleOCR engine is already loaded with another profile")
        return _ENGINE
