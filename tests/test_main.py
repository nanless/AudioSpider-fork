import unittest

from main import effective_artifact_kind


class MainRoutingTests(unittest.TestCase):
    def test_video_platform_source_defaults_to_video_bundle(self):
        self.assertEqual(effective_artifact_kind("bilibili", None), "video_bundle")
        self.assertEqual(effective_artifact_kind("youtube", None), "video_bundle")

    def test_explicit_audio_and_global_mode_are_preserved(self):
        self.assertEqual(effective_artifact_kind("bilibili", "audio"), "audio")
        self.assertIsNone(effective_artifact_kind(None, None))


if __name__ == "__main__":
    unittest.main()
