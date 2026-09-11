import queue
import unittest
from pathlib import Path
from unittest import mock

import bilibili_visual_ocr_worker as worker
from bilibili_visual_ocr import VisualOcrConfig


class FakeProcess:
    def __init__(self):
        self.alive = True
        self.terminated = False
        self.killed = False

    def start(self):
        return None

    def join(self, timeout=None):
        return None

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.terminated = True
        self.alive = False

    def kill(self):
        self.killed = True
        self.alive = False


class VisualOcrWorkerTests(unittest.TestCase):
    def test_timeout_terminates_child(self):
        process = FakeProcess()
        result_queue = mock.Mock()
        result_queue.get.side_effect = queue.Empty
        context = mock.Mock()
        context.Queue.return_value = result_queue
        context.Process.return_value = process
        with mock.patch.object(
            worker.multiprocessing, "get_context", return_value=context
        ), self.assertRaises(TimeoutError):
            worker.prepare_visual_ocr_payloads_bounded(
                Path("video.mp4"), VisualOcrConfig(), timeout_seconds=60
            )
        self.assertTrue(process.terminated)
        result_queue.close.assert_called_once_with()

    def test_success_returns_child_payload(self):
        process = FakeProcess()
        process.alive = False
        result_queue = mock.Mock()
        result_queue.get.return_value = ("ok", {"document": {}, "payloads": {}})
        context = mock.Mock()
        context.Queue.return_value = result_queue
        context.Process.return_value = process
        with mock.patch.object(
            worker.multiprocessing, "get_context", return_value=context
        ):
            result = worker.prepare_visual_ocr_payloads_bounded(
                Path("video.mp4"), VisualOcrConfig(), timeout_seconds=60
            )
        self.assertIn("document", result)
