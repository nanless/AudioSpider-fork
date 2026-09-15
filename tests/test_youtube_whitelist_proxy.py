import importlib.util
import socket
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "youtube_whitelist_proxy.py"
SPEC = importlib.util.spec_from_file_location("youtube_whitelist_proxy", SCRIPT)
proxy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy)


class YouTubeWhitelistProxyTests(unittest.TestCase):
    def test_authority_and_suffix_allowlist_fail_closed(self):
        parse = proxy.YouTubeConnectHandler._parse_authority
        allowed = proxy.YouTubeConnectHandler._is_allowed_host
        self.assertEqual(parse("R1---SN.googlevideo.com:443"), ("r1---sn.googlevideo.com", 443))
        self.assertTrue(allowed("www.youtube.com"))
        self.assertTrue(allowed("r1---sn.googlevideo.com"))
        for host in ("youtube.com.example.com", "googlevideo.com.evil.test", "localhost"):
            self.assertFalse(allowed(host))
        for authority in ("user@youtube.com:443", "youtube.com:443/path", "youtube.com"):
            self.assertIsNone(parse(authority))

    def test_dns_requires_every_address_to_be_global(self):
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        private = public + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with mock.patch.object(proxy.socket, "getaddrinfo", return_value=public):
            self.assertEqual(
                proxy.YouTubeConnectHandler._public_addresses("www.youtube.com", 443), public
            )
        with mock.patch.object(proxy.socket, "getaddrinfo", return_value=private):
            with self.assertRaisesRegex(OSError, "non-public"):
                proxy.YouTubeConnectHandler._public_addresses("www.youtube.com", 443)


if __name__ == "__main__":
    unittest.main()
