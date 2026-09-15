import importlib.util
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "run_youtube_edge_gate.py"
SPEC = importlib.util.spec_from_file_location("run_youtube_edge_gate", SCRIPT)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


class YouTubeEdgeGateTests(unittest.TestCase):
    def test_profile_directory_is_bounded_to_edge_user_data(self):
        with mock.patch.object(gate, "EDGE_USER_DATA", Path("/tmp/edge")):
            profile, database = gate.resolve_cookie_database("Profile 2")
        self.assertEqual(profile, "Profile 2")
        self.assertEqual(database, Path("/tmp/edge/Profile 2/Cookies").resolve())
        for invalid in ("../Default", "Profile 0", "Profile 1000", "Other"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                gate.resolve_cookie_database(invalid)

    def test_remote_command_is_bounded_and_contains_no_secret_argument(self):
        command = gate.remote_command(batch_id="batch-1", category="影视", limit=1)
        self.assertIn("--allow-youtube-cookie", command)
        self.assertIn("--workers 1", command)
        self.assertNotIn("Cookie", command)
        pending = gate.remote_command(
            batch_id="batch-1", category="影视", limit=1,
            retry_failed=False,
        )
        self.assertNotIn("--retry-failed", pending)
        with self.assertRaises(ValueError):
            gate.remote_command(batch_id="bad value", category="影视", limit=1)

    def test_leak_audit_is_attempted_after_remote_transport_exception(self):
        cookies = [
            {"name": "LOGIN_INFO", "value": "fictional-login"},
            {"name": "SAPISID", "value": "fictional-sapisid"},
        ]
        with mock.patch.object(
            gate, "resolve_cookie_database", return_value=("Default", Path("/tmp/Cookies"))
        ), mock.patch.object(
            gate, "load_youtube_cookies", return_value=cookies
        ), mock.patch.object(
            gate, "_run_remote_with_payload", side_effect=[BrokenPipeError(), 0]
        ) as remote, mock.patch.object(
            gate.sys, "argv", ["gate", "--batch-id", "batch-1", "--category", "影视"]
        ):
            status = gate.main()
        self.assertEqual(status, 4)
        self.assertEqual(remote.call_count, 2)
        self.assertIn("audit_youtube_cookie_leaks.py", remote.call_args_list[1].args[0])

    def test_loader_preserves_scope_and_filters_expired_rows(self):
        future = int((time.time() + 3600 + gate.CHROME_EPOCH_OFFSET_SECONDS) * 1_000_000)
        expired = int((time.time() - 3600 + gate.CHROME_EPOCH_OFFSET_SECONDS) * 1_000_000)
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "Cookies"
            connection = sqlite3.connect(database)
            connection.executescript(
                "CREATE TABLE meta(key TEXT, value TEXT);"
                "CREATE TABLE cookies(host_key TEXT, name TEXT, encrypted_value BLOB, "
                "expires_utc INTEGER, path TEXT, is_secure INTEGER);"
            )
            connection.execute("INSERT INTO meta VALUES('version', '23')")
            connection.executemany(
                "INSERT INTO cookies VALUES(?,?,?,?,?,?)",
                [
                    (".youtube.com", "LOGIN_INFO", b"v10login", future, "/", 1),
                    (".youtube.com", "SAPISID", b"v10sapisid", future, "/", 1),
                    (".youtube.com", "SID", b"v10expired", expired, "/", 1),
                    (".youtube.com", "SSID", b"v10insecure", future, "/", 0),
                ],
            )
            connection.commit()
            connection.close()
            with mock.patch.object(
                gate, "resolve_cookie_database", return_value=("Default", database)
            ), mock.patch.object(
                gate, "_edge_safe_storage_password", return_value=b"password"
            ), mock.patch.object(
                gate, "_decrypt_cookie", side_effect=lambda _host, encrypted, *_: encrypted.decode()
            ):
                cookies = gate.load_youtube_cookies()
        self.assertEqual({cookie["name"] for cookie in cookies}, {"LOGIN_INFO", "SAPISID"})
        self.assertTrue(all(cookie["domain"] == ".youtube.com" for cookie in cookies))
        self.assertTrue(all(cookie["secure"] is True for cookie in cookies))


if __name__ == "__main__":
    unittest.main()
