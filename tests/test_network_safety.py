import os
import tempfile
import unittest

from downloader import _safe_join, safe_filename
from network_safety import UnsafeURLError, validate_public_http_url


class NetworkSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_non_http_and_private_destinations(self):
        for url in (
            "file:///etc/passwd",
            "http://127.0.0.1/resource",
            "http://[::1]/resource",
            "http://169.254.169.254/latest/meta-data",
            "http://user:password@example.com/resource",
        ):
            with self.subTest(url=url):
                with self.assertRaises(UnsafeURLError):
                    await validate_public_http_url(url)

    async def test_private_destination_can_be_enabled_only_for_tests(self):
        value = await validate_public_http_url(
            "http://127.0.0.1/resource", allow_private=True,
        )
        self.assertEqual(value, "http://127.0.0.1/resource")

    def test_encoded_separators_cannot_escape_download_root(self):
        root = tempfile.mkdtemp()
        for url in (
            "https://example.test/%2Ftmp%2Fowned.mp3",
            "https://example.test/..%2F..%2Foutside.mp3",
        ):
            filename = safe_filename(url, fmt="mp3")
            self.assertEqual(os.path.basename(filename), filename)
            target = _safe_join(root, filename)
            self.assertEqual(os.path.commonpath([root, target]), root)

    def test_safe_join_rejects_escape(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                _safe_join(root, "../outside")


if __name__ == "__main__":
    unittest.main()
