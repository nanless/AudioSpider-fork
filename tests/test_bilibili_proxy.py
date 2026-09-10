import os
import unittest
from unittest import mock

from bilibili_proxy import get_bilibili_proxy, proxy_request_kwargs


class BilibiliProxyTests(unittest.TestCase):
    def test_unset_proxy_is_disabled(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(get_bilibili_proxy())
            self.assertEqual(proxy_request_kwargs(None), {})

    def test_exact_loopback_http_proxy_is_accepted(self):
        value = "http://127.0.0.1:18443"
        with mock.patch.dict(
            os.environ, {"AUDIOSPIDER_BILIBILI_PROXY": value}, clear=True
        ):
            self.assertEqual(get_bilibili_proxy(), value)
            self.assertEqual(proxy_request_kwargs(value), {"proxy": value})

    def test_every_other_proxy_shape_is_rejected(self):
        invalid = (
            "https://127.0.0.1:18443",
            "http://localhost:18443",
            "http://0.0.0.0:18443",
            "http://192.0.2.1:18443",
            "http://user:secret@127.0.0.1:18443",
            "http://127.0.0.1:18443/",
            "http://127.0.0.1:18443/path",
            "http://127.0.0.1:18443?query=1",
            "http://127.0.0.1:18443#fragment",
            "http://127.0.0.1:0",
            "http://127.0.0.1:65536",
            "http://127.0.0.1:18443\n",
            " http://127.0.0.1:18443",
        )
        for value in invalid:
            with self.subTest(value=value), mock.patch.dict(
                os.environ, {"AUDIOSPIDER_BILIBILI_PROXY": value}, clear=True
            ), self.assertRaises(ValueError):
                get_bilibili_proxy()


if __name__ == "__main__":
    unittest.main()
