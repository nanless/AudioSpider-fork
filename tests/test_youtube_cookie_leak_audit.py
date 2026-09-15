import importlib.util
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "audit_youtube_cookie_leaks.py"
SPEC = importlib.util.spec_from_file_location("audit_youtube_cookie_leaks", SCRIPT)
audit_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit_module)


class YouTubeCookieLeakAuditTests(unittest.TestCase):
    def _database(self, path: Path, metadata: str = ""):
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE audio_urls(id INTEGER, metadata_json TEXT)")
        connection.execute("INSERT INTO audio_urls VALUES(1, ?)", (metadata,))
        connection.commit()
        connection.close()

    def test_clean_and_leaking_artifacts_are_reported_without_secret_value(self):
        secret = "fictional-cookie-secret"
        cookies = [{"value": secret}]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "test.db"
            self._database(database)
            (root / "metadata.json").write_text("{}", encoding="utf-8")
            clean = audit_module.audit(root, database, cookies)
            self.assertEqual(clean["status"], "clean")
            (root / "failure.json").write_text(secret, encoding="utf-8")
            leaked = audit_module.audit(root, database, cookies)
        self.assertEqual(leaked["status"], "failed")
        self.assertEqual(leaked["file_matches"], ["failure.json"])
        self.assertNotIn(secret, json.dumps(leaked))

    def test_large_file_is_streamed_and_cross_chunk_match_is_found(self):
        secret = "fictional-large-cookie-secret"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "test.db"
            self._database(database)
            padding = b"x" * (audit_module.SCAN_CHUNK_BYTES - 7)
            (root / "download.log").write_bytes(padding + secret.encode("utf-8"))
            report = audit_module.audit(root, database, [{"value": secret}])
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["file_matches"], ["download.log"])

    def test_url_encoded_and_base64_variants_are_detected(self):
        secret = "fictional/value+with space"
        variants = audit_module._secret_variants([{"value": secret}])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "test.db"
            self._database(database)
            (root / "unknown.data").write_bytes(variants[-1])
            report = audit_module.audit(root, database, [{"value": secret}])
        self.assertEqual(report["status"], "failed")

    def test_all_sqlite_tables_are_scanned(self):
        secret = "fictional-table-cookie"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "test.db"
            self._database(database)
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE extra_state(value TEXT)")
            connection.execute("INSERT INTO extra_state VALUES(?)", (secret,))
            connection.commit()
            connection.close()
            report = audit_module.audit(root, database, [{"value": secret}])
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["database_match_count"], 1)

    def test_unreadable_candidate_is_not_reported_clean(self):
        secret = "fictional-unreadable-cookie"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "test.db"
            self._database(database)
            candidate = root / "download.log"
            candidate.write_text("safe", encoding="utf-8")
            original_stream_contains = audit_module._stream_contains

            def fail_candidate_only(path, needles):
                if path.resolve() == candidate.resolve():
                    raise PermissionError()
                return original_stream_contains(path, needles)

            with mock.patch.object(
                audit_module, "_stream_contains", side_effect=fail_candidate_only
            ):
                report = audit_module.audit(root, database, [{"value": secret}])
        self.assertEqual(report["status"], "error")
        self.assertEqual(report["scan_error_count"], 1)
        self.assertTrue(all(secret not in item for item in report["scan_errors"]))

    def test_secret_in_filename_is_detected_without_echoing_it(self):
        secret = "fictional-filename-cookie"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "test.db"
            self._database(database)
            (root / f"failure-{secret}.txt").write_text("safe", encoding="utf-8")
            report = audit_module.audit(root, database, [{"value": secret}])
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["file_match_count"], 1)
        self.assertNotIn(secret, json.dumps(report))

    def test_unknown_and_caption_part_files_are_scanned(self):
        secret = "fictional-part-cookie"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "test.db"
            self._database(database)
            for filename in ("source.mp4.part", "source.en.vtt.part", "unknown.part"):
                with self.subTest(filename=filename):
                    candidate = root / filename
                    candidate.write_text(secret, encoding="utf-8")
                    report = audit_module.audit(root, database, [{"value": secret}])
                    self.assertEqual(report["status"], "failed")
                    self.assertIn(filename, report["file_matches"])
                    candidate.unlink()

    def test_only_sidecar_declared_hashed_binary_media_is_excluded(self):
        media = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "test.db"
            self._database(database)
            part = root / "source.mp4.part"
            final = root / "source.mp4"
            part.write_bytes(media)
            final.write_bytes(media)
            (root / "metadata.json").write_text(json.dumps({
                "files": {
                    "video": {
                        "path": final.name,
                        "bytes": len(media),
                        "sha256": hashlib.sha256(media).hexdigest(),
                    },
                    "resumable_video": {
                        "path": part.name,
                        "bytes": len(media),
                        "sha256": hashlib.sha256(media).hexdigest(),
                    },
                }
            }), encoding="utf-8")
            report = audit_module.audit(
                root, database, [{"value": "fictional-absent-cookie"}]
            )
        self.assertEqual(report["status"], "clean")
        self.assertEqual(report["excluded_binary_files"], 2)

    def test_undeclared_final_media_extension_is_scanned(self):
        secret = "fictional-undeclared-media-cookie"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "test.db"
            self._database(database)
            (root / "payload.mp4").write_bytes(
                b"\x00\x00\x00\x18ftypisom" + secret.encode("utf-8")
            )
            report = audit_module.audit(root, database, [{"value": secret}])
        self.assertEqual(report["status"], "failed")
        self.assertIn("payload.mp4", report["file_matches"])

    def test_invalid_sidecar_media_closure_is_scanned(self):
        secret = "fictional-invalid-closure-cookie"
        for case in ("wrong-hash", "wrong-magic"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                database = root / "test.db"
                self._database(database)
                media = (
                    b"\x00\x00\x00\x18ftypisom" if case == "wrong-hash"
                    else b"not-an-mp4-container"
                ) + secret.encode("utf-8")
                target = root / "payload.mp4"
                target.write_bytes(media)
                expected_hash = (
                    "0" * 64 if case == "wrong-hash"
                    else hashlib.sha256(media).hexdigest()
                )
                (root / "metadata.json").write_text(json.dumps({
                    "files": {"video": {
                        "path": target.name,
                        "bytes": len(media),
                        "sha256": expected_hash,
                    }}
                }), encoding="utf-8")
                report = audit_module.audit(root, database, [{"value": secret}])
                self.assertEqual(report["status"], "failed")
                self.assertIn("payload.mp4", report["file_matches"])

    def test_secret_in_excluded_binary_or_symlink_name_is_still_detected(self):
        secret = "fictional-path-cookie"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "test.db"
            self._database(database)
            (root / f"video-{secret}.mp4").write_bytes(b"binary")
            (root / f"link-{secret}").symlink_to("missing-target")
            report = audit_module.audit(root, database, [{"value": secret}])
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["file_match_count"], 2)
        self.assertNotIn(secret, json.dumps(report))

    def test_primary_database_bytes_are_scanned_in_addition_to_rows(self):
        secret = "fictional-database-cookie"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "test.db"
            self._database(database, metadata=secret)
            with mock.patch.object(audit_module, "_sqlite_match_count", return_value=0):
                report = audit_module.audit(root, database, [{"value": secret}])
        self.assertEqual(report["status"], "failed")
        self.assertIn("test.db", report["file_matches"])


if __name__ == "__main__":
    unittest.main()
