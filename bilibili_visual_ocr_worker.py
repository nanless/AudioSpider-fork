#!/usr/bin/env python3
"""Killable process boundary for one Bilibili visual-OCR bundle."""

from __future__ import annotations

import multiprocessing
import os
import queue
from pathlib import Path
from typing import Any

from bilibili_visual_ocr import VisualOcrConfig


DEFAULT_VISUAL_OCR_TIMEOUT_SECONDS = 14_400


def _worker(video_path: str, config: VisualOcrConfig, result_queue: Any) -> None:
    try:
        for name in list(os.environ):
            normalized = name.upper()
            if any(marker in normalized for marker in (
                "COOKIE", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL",
                "AUTHORIZATION", "PROXY", "API_KEY", "ACCESS_KEY",
            )):
                os.environ.pop(name, None)
        from bilibili_visual_ocr import prepare_visual_ocr_payloads
        from bilibili_visual_ocr_paddle import create_paddle_engine

        engine = create_paddle_engine(device="gpu:0")
        result_queue.put((
            "ok",
            prepare_visual_ocr_payloads(Path(video_path), config, engine=engine),
        ))
    except BaseException as exc:
        result_queue.put(("error", type(exc).__name__))


def prepare_visual_ocr_payloads_bounded(
    video_path: Path,
    config: VisualOcrConfig,
    *,
    timeout_seconds: int = DEFAULT_VISUAL_OCR_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run one OCR job in a killable, credential-clean child process."""

    if type(timeout_seconds) is not int or not 60 <= timeout_seconds <= 86_400:
        raise ValueError("visual OCR timeout must be an integer in 60..86400")
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue(maxsize=1)
    process = context.Process(
        target=_worker,
        args=(str(Path(video_path)), config, result_queue),
    )
    process.start()
    try:
        try:
            result = result_queue.get(timeout=timeout_seconds)
        except queue.Empty as exc:
            process.terminate()
            process.join(timeout=10)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
            raise TimeoutError(
                f"visual OCR exceeded {timeout_seconds}s hard limit"
            ) from exc
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        if result[0] != "ok":
            raise RuntimeError(f"visual OCR child failed: {result[1]}")
        if not isinstance(result[1], dict):
            raise RuntimeError("visual OCR child returned an invalid result")
        return result[1]
    finally:
        result_queue.close()
        result_queue.join_thread()
