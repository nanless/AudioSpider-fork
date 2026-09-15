import io
import os
import unittest
from argparse import Namespace
from unittest import mock

from main import effective_artifact_kind, load_youtube_auth_cookies


class MainRoutingTests(unittest.TestCase):
    def test_video_platform_source_defaults_to_video_bundle(self):
        self.assertEqual(effective_artifact_kind("bilibili", None), "video_bundle")
        self.assertEqual(effective_artifact_kind("youtube", None), "video_bundle")

    def test_explicit_audio_and_global_mode_are_preserved(self):
        self.assertEqual(effective_artifact_kind("bilibili", "audio"), "audio")
        self.assertIsNone(effective_artifact_kind(None, None))

    @staticmethod
    def _args(**overrides):
        values = {
            "allow_youtube_cookie": True,
            "action": "download",
            "source": "youtube",
            "loop": False,
            "batch_id": "batch-1",
            "category": "影视",
            "language": "en",
            "workers": 1,
            "per_source": False,
            "per_category": False,
        }
        values.update(overrides)
        return Namespace(**values)

    def test_youtube_cookie_gate_consumes_stdin_and_clears_ambient_secret(self):
        payload = (
            b'[{"name":"LOGIN_INFO","value":"login-value","domain":".youtube.com",'
            b'"path":"/","secure":true},{"name":"SAPISID","value":"sapisid-value",'
            b'"domain":".youtube.com","path":"/","secure":true}]'
        )
        with mock.patch.dict(os.environ, {"YOUTUBE_COOKIE_JSON": "must-disappear"}):
            cookies = load_youtube_auth_cookies(
                self._args(), "video_bundle", io.BytesIO(payload)
            )
            self.assertNotIn("YOUTUBE_COOKIE_JSON", os.environ)
        self.assertEqual([cookie["name"] for cookie in cookies], ["LOGIN_INFO", "SAPISID"])

    def test_youtube_cookie_gate_rejects_wrong_scope_before_reading(self):
        cases = [
            (self._args(action="stats"), "video_bundle"),
            (self._args(source="bilibili"), "video_bundle"),
            (self._args(), "audio"),
            (self._args(loop=True), "video_bundle"),
            (self._args(batch_id=None), "video_bundle"),
            (self._args(category=None), "video_bundle"),
            (self._args(language=None), "video_bundle"),
            (self._args(workers=2), "video_bundle"),
            (self._args(per_source=True), "video_bundle"),
            (self._args(per_category=True), "video_bundle"),
        ]
        for args, artifact_kind in cases:
            with self.subTest(args=args, artifact_kind=artifact_kind):
                stream = mock.Mock()
                with self.assertRaises(ValueError):
                    load_youtube_auth_cookies(args, artifact_kind, stream)
                stream.read.assert_not_called()

    def test_disabled_gate_discards_ambient_secret_without_reading_stdin(self):
        stream = mock.Mock()
        args = self._args(allow_youtube_cookie=False)
        with mock.patch.dict(os.environ, {"YOUTUBE_COOKIE_JSON": "must-disappear"}):
            self.assertEqual(
                load_youtube_auth_cookies(args, "video_bundle", stream), []
            )
            self.assertNotIn("YOUTUBE_COOKIE_JSON", os.environ)
        stream.read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
