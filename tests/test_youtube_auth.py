import io
import json
import time
import unittest
from http.cookiejar import CookieJar
from urllib.request import Request

from youtube_auth import (
    cookie_secret_variants,
    install_cookiejar,
    read_cookie_payload,
    redact_cookie_values,
    validate_cookie_payload,
)


def payload(**overrides):
    auth = {
        "name": "SAPISID",
        "value": "secret-value",
        "domain": ".youtube.com",
        "path": "/",
        "secure": True,
        "expires": int(time.time()) + 3600,
    }
    auth.update(overrides)
    login = dict(auth, name="LOGIN_INFO", value="login-secret")
    return json.dumps([auth, login])


class FakeJar:
    def __init__(self):
        self.cookies = []

    def set_cookie(self, cookie):
        self.cookies.append(cookie)


class YouTubeAuthTests(unittest.TestCase):
    def test_reads_bounded_valid_payload(self):
        observed = read_cookie_payload(io.BytesIO(payload().encode()))
        self.assertEqual(observed[0]["name"], "SAPISID")
        self.assertEqual(observed[0]["domain"], ".youtube.com")

    def test_rejects_forbidden_cookie_without_echoing_secret(self):
        secret = "do-not-echo-this"
        with self.assertRaises(ValueError) as raised:
            validate_cookie_payload(payload(name="UNRELATED", value=secret))
        self.assertNotIn(secret, str(raised.exception))

    def test_rejects_forbidden_domain(self):
        with self.assertRaisesRegex(ValueError, "forbidden domain"):
            validate_cookie_payload(payload(domain=".google.com"))

    def test_rejects_insecure_or_control_character_values(self):
        with self.assertRaisesRegex(ValueError, "must be Secure"):
            validate_cookie_payload(payload(secure=False))
        with self.assertRaisesRegex(ValueError, "unsafe value"):
            validate_cookie_payload(payload(value="secret\nheader"))

    def test_rejects_expired_and_duplicate_entries(self):
        with self.assertRaisesRegex(ValueError, "expired"):
            validate_cookie_payload(payload(expires=int(time.time()) - 1))
        entry = json.loads(payload())[0]
        with self.assertRaisesRegex(ValueError, "duplicated"):
            validate_cookie_payload(json.dumps([entry, entry, json.loads(payload())[1]]))

    def test_requires_minimum_authenticated_session_entries(self):
        entries = json.loads(payload())
        with self.assertRaisesRegex(ValueError, "minimum authenticated"):
            validate_cookie_payload(json.dumps(entries[:1]))
        with self.assertRaisesRegex(ValueError, "minimum authenticated"):
            validate_cookie_payload(json.dumps(entries[1:]))

    def test_installs_domain_scoped_cookiejar_entries(self):
        cookies = validate_cookie_payload(payload())
        jar = FakeJar()
        install_cookiejar(jar, cookies)
        self.assertEqual(len(jar.cookies), 2)
        self.assertEqual(jar.cookies[0].domain, ".youtube.com")
        self.assertTrue(jar.cookies[0].secure)

    def test_cookiejar_never_sends_cookie_to_media_or_unrelated_domains(self):
        jar = CookieJar()
        install_cookiejar(jar, validate_cookie_payload(payload()))
        youtube = Request("https://www.youtube.com/watch?v=test")
        jar.add_cookie_header(youtube)
        self.assertIn("SAPISID=secret-value", youtube.get_header("Cookie"))
        for url in (
            "https://rr1---sn.example.googlevideo.com/videoplayback",
            "https://youtube.com.example.com/",
            "https://example.com/",
        ):
            request = Request(url)
            jar.add_cookie_header(request)
            self.assertIsNone(request.get_header("Cookie"))

    def test_redacts_cookie_values_from_errors(self):
        cookies = validate_cookie_payload(payload())
        observed = redact_cookie_values(
            "failed Cookie: SAPISID=secret-value and login-secret", cookies
        )
        self.assertNotIn("secret-value", observed)
        self.assertNotIn("login-secret", observed)

    def test_redaction_is_longest_first_for_overlapping_cookie_values(self):
        cookies = [
            {"name": "SID", "value": "prefix"},
            {"name": "LOGIN_INFO", "value": "prefix-with-private-suffix"},
        ]
        observed = redact_cookie_values(
            "failed prefix-with-private-suffix", cookies
        )
        self.assertEqual(observed, "failed [youtube-cookie]")

    def test_redacts_every_supported_reversible_variant(self):
        cookies = [{"name": "LOGIN_INFO", "value": "fictional/value+with space"}]
        variants = cookie_secret_variants(cookies)
        observed = redact_cookie_values(" | ".join(variants), cookies)
        for variant in variants:
            self.assertNotIn(variant, observed)
