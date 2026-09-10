import json
import socket
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from scripts import recover_download_claims as recovery
from storage import Storage


class RecoverDownloadClaimsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db_path = self.root / "audiospider.db"
        self.worker = f"{socket.gethostname()}:111:aaaaaaaaaaaa"
        storage = Storage(str(self.db_path))
        connection = storage._get_conn()
        rows = [
            ("bili-a", "bilibili", "video_bundle", "job-a", "访谈", "zh", "downloading", self.worker),
            ("bili-b", "bilibili", "video_bundle", "job-b", "访谈", "zh", "downloading", self.worker),
            ("bili-c", "bilibili", "video_bundle", "job-c", "影视", "zh", "downloading", self.worker),
            ("bili-pending", "bilibili", "video_bundle", "job-p", "访谈", "zh", "pending", self.worker),
            ("bili-failed", "bilibili", "video_bundle", "job-f", "访谈", "zh", "failed", self.worker),
            ("other-worker", "bilibili", "video_bundle", "job-o", "访谈", "zh", "downloading", f"{socket.gethostname()}:222:bbbbbbbbbbbb"),
            ("youtube", "youtube", "video_bundle", "job-y", "访谈", "en", "downloading", self.worker),
            ("legacy-audio", "bilibili", "audio", "", "访谈", "zh", "downloading", self.worker),
        ]
        for source_id, source, kind, job_key, category, language, status, worker in rows:
            connection.execute(
                "INSERT INTO audio_urls "
                "(url, source, artifact_kind, job_key, source_id, category, language, "
                "status, claimed_by, claimed_at, lease_expires_at, discovered_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'claimed-at', 'lease', 'now')",
                (
                    f"https://example.test/{source_id}", source, kind, job_key,
                    source_id, category, language, status, worker,
                ),
            )
        connection.commit()
        connection.close()

    def tearDown(self):
        self.temporary.cleanup()

    def scope(self, **overrides):
        values = {
            "claimed_by": self.worker,
            "source": "bilibili",
            "artifact_kind": "video_bundle",
        }
        values.update(overrides)
        return recovery.RecoveryScope(**values)

    def rows(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            return {
                row["source_id"]: dict(row)
                for row in connection.execute("SELECT * FROM audio_urls")
            }
        finally:
            connection.close()

    def test_parser_is_dry_run_and_requires_exact_identity(self):
        parser = recovery.build_parser()
        args = parser.parse_args([
            "--claimed-by", "host:1:aaaaaaaaaaaa",
            "--source", "bilibili", "--artifact-kind", "video_bundle",
        ])
        self.assertFalse(args.apply)
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["--source", "bilibili", "--artifact-kind", "video_bundle"])

    def test_dry_run_reports_but_changes_nothing(self):
        before = self.rows()
        report = recovery.recover_claims(self.db_path, self.scope())
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(report["matched_count"], 3)
        self.assertEqual(report["updated_count"], 0)
        self.assertIsNone(report["backup"])
        self.assertEqual(self.rows(), before)

    def test_apply_resets_only_exact_downloading_claims_and_creates_backup(self):
        report = recovery.recover_claims(
            self.db_path, self.scope(category="访谈", language="zh"),
            apply=True, process_probe=mock.Mock(return_value=False),
        )
        self.assertEqual(report["matched_count"], 2)
        self.assertEqual(report["updated_count"], 2)
        backup = Path(report["backup"])
        self.assertTrue(backup.is_file())

        rows = self.rows()
        for source_id in ("bili-a", "bili-b"):
            self.assertEqual(rows[source_id]["status"], "pending")
            self.assertEqual(rows[source_id]["claimed_by"], "")
            self.assertEqual(rows[source_id]["claimed_at"], "")
            self.assertEqual(rows[source_id]["lease_expires_at"], "")
        self.assertEqual(rows["bili-c"]["status"], "downloading")
        self.assertEqual(rows["other-worker"]["status"], "downloading")
        self.assertEqual(rows["youtube"]["status"], "downloading")
        self.assertEqual(rows["legacy-audio"]["status"], "downloading")
        self.assertEqual(rows["bili-pending"]["status"], "pending")
        self.assertEqual(rows["bili-failed"]["status"], "failed")
        self.assertEqual(
            rows["bili-pending"]["claimed_by"], self.worker,
        )
        self.assertEqual(
            rows["bili-failed"]["claimed_by"], self.worker,
        )

        backup_connection = sqlite3.connect(backup)
        try:
            backup_rows = backup_connection.execute(
                "SELECT status, claimed_by FROM audio_urls WHERE source_id='bili-a'"
            ).fetchone()
        finally:
            backup_connection.close()
        self.assertEqual(backup_rows, ("downloading", self.worker))

    def test_id_range_and_job_key_file_are_combined_exactly(self):
        rows = self.rows()
        id_a = rows["bili-a"]["id"]
        id_c = rows["bili-c"]["id"]
        keys = self.root / "job-keys.txt"
        keys.write_text("# exact jobs\njob-b\njob-c\njob-c\n", encoding="utf-8")
        scope = self.scope(
            id_min=id_a, id_max=id_c,
            job_keys=recovery.load_job_keys(keys),
        )
        report = recovery.recover_claims(
            self.db_path, scope, apply=True,
            process_probe=mock.Mock(return_value=False),
        )
        self.assertEqual([row["job_key"] for row in report["rows"]], ["job-b", "job-c"])
        self.assertEqual(report["updated_count"], 2)
        after = self.rows()
        self.assertEqual(after["bili-a"]["status"], "downloading")
        self.assertEqual(after["bili-b"]["status"], "pending")
        self.assertEqual(after["bili-c"]["status"], "pending")

    def test_apply_refuses_a_live_local_worker_before_backup(self):
        worker = f"{socket.gethostname()}:43210:abcdef123456"
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            "UPDATE audio_urls SET claimed_by=? WHERE source_id='bili-a'", (worker,),
        )
        connection.commit()
        connection.close()
        with mock.patch.object(recovery, "backup_database") as backup, \
             self.assertRaisesRegex(recovery.RecoveryError, "live local worker PID"):
            recovery.recover_claims(
                self.db_path,
                recovery.RecoveryScope(worker, "bilibili", "video_bundle", job_keys=("job-a",)),
                apply=True, process_probe=mock.Mock(return_value=True),
            )
        backup.assert_not_called()
        self.assertEqual(self.rows()["bili-a"]["status"], "downloading")

    def test_apply_with_no_matches_creates_no_backup(self):
        with mock.patch.object(recovery, "backup_database") as backup:
            report = recovery.recover_claims(
                self.db_path, self.scope(category="不存在"), apply=True,
            )
        backup.assert_not_called()
        self.assertEqual(report["status"], "no-match")
        self.assertEqual(report["matched_count"], 0)
        self.assertEqual(report["updated_count"], 0)

    def test_backup_failure_rolls_back_without_changing_claims(self):
        before = self.rows()
        with mock.patch.object(
            recovery, "backup_database", side_effect=recovery.RecoveryError("backup failed"),
        ), self.assertRaisesRegex(recovery.RecoveryError, "backup failed"):
            recovery.recover_claims(
                self.db_path, self.scope(job_keys=("job-a",)), apply=True,
                process_probe=mock.Mock(return_value=False),
            )
        self.assertEqual(self.rows(), before)

    def test_remote_or_unparseable_worker_is_not_probed_as_a_local_pid(self):
        probe = mock.Mock(return_value=True)
        scope = recovery.RecoveryScope(
            "different-host:111:aaaaaaaaaaaa", "bilibili", "video_bundle",
        )
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            "UPDATE audio_urls SET claimed_by=? WHERE source_id='bili-a'",
            (scope.claimed_by,),
        )
        connection.commit()
        connection.close()
        report = recovery.recover_claims(
            self.db_path, scope, process_probe=probe,
        )
        self.assertIsNone(report["local_worker_pid"])
        self.assertIsNone(report["local_worker_alive"])
        probe.assert_not_called()

    def test_apply_refuses_worker_that_cannot_be_verified_on_this_host(self):
        for claimed_by in ("different-host:111:aaaaaaaaaaaa", "legacy-worker"):
            with self.subTest(claimed_by=claimed_by):
                scope = recovery.RecoveryScope(
                    claimed_by, "bilibili", "video_bundle",
                )
                connection = sqlite3.connect(self.db_path)
                connection.execute(
                    "UPDATE audio_urls SET claimed_by=? WHERE source_id='bili-a'",
                    (scope.claimed_by,),
                )
                connection.commit()
                connection.close()
                with mock.patch.object(recovery, "backup_database") as backup, \
                     self.assertRaisesRegex(recovery.RecoveryError, "cannot be verified"):
                    recovery.recover_claims(self.db_path, scope, apply=True)
                backup.assert_not_called()
        self.assertEqual(self.rows()["bili-a"]["status"], "downloading")

    def test_invalid_half_range_and_empty_job_file_are_rejected(self):
        with self.assertRaisesRegex(recovery.RecoveryError, "provided together"):
            recovery.recover_claims(self.db_path, self.scope(id_min=1))
        empty = self.root / "empty.txt"
        empty.write_text("# no jobs\n\n", encoding="utf-8")
        with self.assertRaisesRegex(recovery.RecoveryError, "contains no job keys"):
            recovery.load_job_keys(empty)

    def test_main_emits_machine_readable_report(self):
        with redirect_stdout(StringIO()) as output:
            result = recovery.main([
                "--db", str(self.db_path),
                "--claimed-by", self.worker,
                "--source", "bilibili", "--artifact-kind", "video_bundle",
                "--category", "影视",
            ])
        self.assertEqual(result, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["matched_count"], 1)
        self.assertEqual(report["rows"][0]["job_key"], "job-c")

    def test_main_refusal_is_machine_readable(self):
        with redirect_stderr(StringIO()) as error:
            result = recovery.main([
                "--db", str(self.db_path),
                "--claimed-by", "remote:111:aaaaaaaaaaaa",
                "--source", "bilibili", "--artifact-kind", "video_bundle",
                "--id-min", "1",
            ])
        self.assertEqual(result, 2)
        report = json.loads(error.getvalue())
        self.assertEqual(report["status"], "refused")
        self.assertIn("provided together", report["reason"])


if __name__ == "__main__":
    unittest.main()
