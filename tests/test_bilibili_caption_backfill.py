import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from scripts import backfill_bilibili_captions as backfill
from storage import AudioRecord, Storage


class CaptionBackfillTests(unittest.TestCase):
    def _database_row(self, root: Path, bundle: Path) -> tuple[Path, int]:
        db_path = root / "audiospider.db"
        storage = Storage(str(db_path))
        storage.add_url(AudioRecord(
            url="https://www.bilibili.com/video/BV1xx411c7mD?p=1",
            source="bilibili",
            source_id="BV1xx411c7mD_p1",
            artifact_kind="video_bundle",
            job_key="BV1xx411c7mD-p1-c1-test",
            status="done",
            local_path=str(bundle / "source.mp4"),
            bundle_path=str(bundle),
            file_size=12,
            content_hash="a" * 64,
        ))
        connection = storage._get_conn()
        row_id = connection.execute("SELECT id FROM audio_urls").fetchone()[0]
        connection.close()
        return db_path, row_id

    @staticmethod
    def _candidate(bundle: Path, row_id: int) -> dict:
        return {
            "id": row_id,
            "source_id": "BV1xx411c7mD_p1",
            "job_key": "BV1xx411c7mD-p1-c1-test",
            "bundle": bundle,
            "_bundle_path_db": str(bundle),
            "_local_path_db": str(bundle / "source.mp4"),
            "_old_file_size": 12,
            "_old_content_hash": "a" * 64,
            "caption_status": "invalid_track_inventory",
        }

    def test_parser_is_dry_run_by_default(self):
        args = backfill.build_parser().parse_args([])
        self.assertFalse(args.apply)
        self.assertFalse(args.allow_bilibili_cookie)

    def test_old_empty_requested_languages_fall_back_to_content_language(self):
        job = backfill._job_from_metadata({
            "bvid": "BV1xx411c7mD",
            "cid": 1,
            "content_language": "zh",
            "caption": {"requested_languages": []},
        })
        self.assertIn("zh", job["languages"])
        self.assertIn("ai-zh", job["languages"])
        self.assertNotEqual(job["languages"], [])

    def test_failure_reason_is_bounded_and_does_not_echo_raw_message(self):
        secretish = ValueError(
            "unexpected https://example.invalid/path?token=do-not-print"
        )
        failure = backfill.CaptionBackfillFailure("inventory", secretish)
        self.assertEqual(failure.phase, "inventory")
        self.assertEqual(failure.error_type, "ValueError")
        self.assertEqual(failure.reason, "validation_or_runtime_error")
        self.assertNotIn("token", str(failure))

    def test_failure_reason_distinguishes_caption_timeline_overrun(self):
        failure = backfill.CaptionBackfillFailure(
            "commit",
            ValueError("caption track 0 extends too far beyond the media"),
        )
        self.assertEqual(failure.reason, "caption_exceeds_media_duration")

    def test_dry_run_does_not_construct_network_client(self):
        args = backfill.build_parser().parse_args([])
        candidate = {
            "id": 1, "source_id": "BV1xx411c7mD_p1", "job_key": "job",
            "bundle": Path("/data/downloads/bilibili/访谈/BV1xx411c7mD_p1/job"),
            "_bundle_path_db": "/data/ignored", "_old_file_size": 12,
            "_old_content_hash": "a" * 64, "caption_status": "unknown",
        }
        with mock.patch.object(backfill, "discover_candidates", return_value=[candidate]), \
             mock.patch.object(backfill, "BilibiliClient") as client, \
             redirect_stdout(StringIO()) as output:
            result = __import__("asyncio").run(backfill.async_main(args))
        self.assertEqual(result, 0)
        client.assert_not_called()
        report = json.loads(output.getvalue())
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(report["candidates"][0]["bundle_path"], str(candidate["bundle"]))

    def test_discovery_requires_exact_done_integrated_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = (
                root / "downloads" / "bilibili" / "访谈" / "BV1xx411c7mD_p1"
                / "BV1xx411c7mD-p1-c1-test"
            )
            bundle.mkdir(parents=True)
            (bundle / "metadata.json").write_text("{}", encoding="utf-8")
            db_path, _ = self._database_row(root, bundle)
            metadata = {
                "job_key": bundle.name,
                "source_id": bundle.parent.name,
                "caption": {"status": "invalid_track_inventory"},
            }
            with mock.patch.object(
                backfill, "validate_bundle", return_value={"metadata": metadata}
            ):
                candidates = backfill.discover_candidates(
                    db_path, root / "downloads", 10
                )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["job_key"], bundle.name)
        self.assertEqual(candidates[0]["_old_file_size"], 12)
        self.assertEqual(candidates[0]["_old_content_hash"], "a" * 64)

    def test_pre_mutation_rejects_disk_closure_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = (
                root / "downloads" / "bilibili" / "访谈" / "BV1xx411c7mD_p1"
                / "BV1xx411c7mD-p1-c1-test"
            )
            bundle.mkdir(parents=True)
            (bundle / "metadata.json").write_text("{}", encoding="utf-8")
            db_path, row_id = self._database_row(root, bundle)
            candidate = self._candidate(bundle, row_id)
            with mock.patch.object(backfill, "validate_bundle", return_value={}), \
                 mock.patch.object(
                     backfill, "bundle_fingerprint", return_value=(13, "b" * 64)
                 ):
                with self.assertRaises(RuntimeError):
                    backfill._verify_pre_mutation(db_path, candidate)

    def test_pre_mutation_rechecks_saved_database_closure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = (
                root / "downloads" / "bilibili" / "访谈" / "BV1xx411c7mD_p1"
                / "BV1xx411c7mD-p1-c1-test"
            )
            bundle.mkdir(parents=True)
            (bundle / "metadata.json").write_text("{}", encoding="utf-8")
            db_path, row_id = self._database_row(root, bundle)
            candidate = self._candidate(bundle, row_id)
            connection = sqlite3.connect(db_path)
            connection.execute(
                "UPDATE audio_urls SET file_size=13 WHERE id=?", (row_id,)
            )
            connection.commit()
            connection.close()
            with mock.patch.object(backfill, "validate_bundle", return_value={}), \
                 mock.patch.object(
                     backfill, "bundle_fingerprint", return_value=(12, "a" * 64)
                 ):
                with self.assertRaises(RuntimeError):
                    backfill._verify_pre_mutation(db_path, candidate)

    def test_network_phase_disk_drift_is_rejected_before_first_move(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = (
                root / "downloads" / "bilibili" / "访谈" / "BV1xx411c7mD_p1"
                / "BV1xx411c7mD-p1-c1-test"
            )
            bundle.mkdir(parents=True)
            original = b'{"original":true}\n'
            (bundle / "metadata.json").write_bytes(original)
            db_path, row_id = self._database_row(root, bundle)
            metadata = {
                "bvid": "BV1xx411c7mD", "cid": 1,
                "content_language": "zh", "files": {},
                "caption": {"requested_languages": ["zh"]},
                "acquisition_policy": {"require_caption": False},
            }
            inventory = {
                "status": "not_provided_publicly",
                "need_login_subtitle": False,
                "tracks": [],
            }
            attempts = [{
                "attempt": 1, "inventory_status": "not_provided_publicly",
                "need_login_subtitle": False, "raw_track_count": 0,
                "selected_track_count": 0,
                "selection_status": "not_provided_publicly", "tracks": [],
            }]
            client = mock.Mock(authenticated=False)
            with mock.patch.object(
                backfill, "_verify_pre_mutation",
                side_effect=[
                    {"metadata": metadata},
                    RuntimeError("disk closure drifted during network phase"),
                ],
            ) as verify, mock.patch.object(
                backfill, "resolve_caption_inventory",
                mock.AsyncMock(return_value=(
                    inventory, [], "not_provided_publicly", attempts,
                )),
            ) as resolve:
                with self.assertRaises(RuntimeError):
                    __import__("asyncio").run(backfill.repair_candidate(
                        client, db_path, self._candidate(bundle, row_id)
                    ))
            self.assertEqual(verify.call_count, 2)
            resolve.assert_awaited_once()
            self.assertEqual((bundle / "metadata.json").read_bytes(), original)
            self.assertEqual(list(bundle.glob("captions.*")), [])

    def test_sqlite_backup_is_consistent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db_path = root / "source.db"
            connection = sqlite3.connect(db_path)
            connection.execute("CREATE TABLE sample(value TEXT)")
            connection.execute("INSERT INTO sample VALUES ('kept')")
            connection.commit()
            connection.close()
            backup = backfill.backup_database(db_path)
            copied = sqlite3.connect(backup).execute(
                "SELECT value FROM sample"
            ).fetchone()[0]
        self.assertEqual(copied, "kept")

    def test_base_exception_removes_partial_backup_and_closes_connections(self):
        class SyntheticInterrupt(BaseException):
            pass

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db_path = root / "source.db"
            db_path.write_bytes(b"placeholder")
            source = mock.Mock()
            destination = mock.Mock()

            def connect(path, **_kwargs):
                if str(path) == str(db_path):
                    return source
                Path(path).write_bytes(b"partial-backup")
                return destination

            source.backup.side_effect = SyntheticInterrupt()
            with mock.patch.object(backfill.sqlite3, "connect", side_effect=connect):
                with self.assertRaises(SyntheticInterrupt):
                    backfill.backup_database(db_path)
            self.assertEqual(list(root.glob("*.caption-backfill-*.bak")), [])
            source.close.assert_called_once()
            destination.close.assert_called_once()

    def test_failed_sidecar_validation_rolls_back_files_and_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = (
                root / "downloads" / "bilibili" / "访谈" / "BV1xx411c7mD_p1"
                / "BV1xx411c7mD-p1-c1-test"
            )
            bundle.mkdir(parents=True)
            video = bundle / "source.mp4"
            audio = bundle / "audio.wav"
            video.write_bytes(b"video-original")
            audio.write_bytes(b"audio-original")
            original = b'{"original":true}\n'
            (bundle / "metadata.json").write_bytes(original)
            db_path, row_id = self._database_row(root, bundle)
            stage = root / "stage"
            stage.mkdir()
            paths = {}
            for extension in ("json", "vtt", "txt"):
                path = stage / f"captions.ai-zh.automatic.1.{extension}"
                path.write_text(extension, encoding="utf-8")
                paths[extension] = path
            metadata = {"files": {}, "caption": {}}
            prepared = [{
                "index": 0,
                "paths": paths,
                "track": {"id_str": "1", "language": "ai-zh"},
            }]
            with mock.patch.object(
                backfill, "validate_bundle",
                side_effect=[{}, ValueError("synthetic validation failure"), {}],
            ), mock.patch.object(
                backfill, "bundle_fingerprint", return_value=(12, "a" * 64)
            ):
                with self.assertRaises(ValueError):
                    backfill._commit_sidecar_and_database(
                        db_path, self._candidate(bundle, row_id), metadata, prepared
                    )
            self.assertEqual((bundle / "metadata.json").read_bytes(), original)
            self.assertEqual(video.read_bytes(), b"video-original")
            self.assertEqual(audio.read_bytes(), b"audio-original")
            self.assertEqual(list(bundle.glob("captions.*")), [])
            connection = sqlite3.connect(db_path)
            row = connection.execute(
                "SELECT file_size, content_hash FROM audio_urls WHERE id=?", (row_id,)
            ).fetchone()
            connection.close()
        self.assertEqual(row, (12, "a" * 64))

    def test_success_updates_closure_without_changing_media(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = (
                root / "downloads" / "bilibili" / "访谈" / "BV1xx411c7mD_p1"
                / "BV1xx411c7mD-p1-c1-test"
            )
            bundle.mkdir(parents=True)
            video = bundle / "source.mp4"
            audio = bundle / "audio.wav"
            video.write_bytes(b"video-original")
            audio.write_bytes(b"audio-original")
            (bundle / "metadata.json").write_text("{}\n", encoding="utf-8")
            db_path, row_id = self._database_row(root, bundle)
            stage = root / "stage"
            stage.mkdir()
            paths = {}
            for extension in ("json", "vtt", "txt"):
                path = stage / f"captions.ai-zh.automatic.1.{extension}"
                path.write_text(extension, encoding="utf-8")
                paths[extension] = path
            metadata = {"files": {}, "caption": {}}
            prepared = [{
                "index": 0,
                "paths": paths,
                "track": {"id_str": "1", "language": "ai-zh"},
            }]
            def fingerprint_without_writer_lock(_bundle):
                probe = sqlite3.connect(db_path, timeout=0)
                probe.execute("BEGIN IMMEDIATE")
                probe.rollback()
                probe.close()
                return 999, "new-hash"

            with mock.patch.object(backfill, "_verify_pre_mutation", return_value={}), \
                 mock.patch.object(backfill, "validate_bundle", return_value={}), \
                 mock.patch.object(
                     backfill, "bundle_fingerprint",
                     side_effect=fingerprint_without_writer_lock,
                 ):
                total, content_hash = backfill._commit_sidecar_and_database(
                    db_path, self._candidate(bundle, row_id), metadata, prepared
                )
            connection = sqlite3.connect(db_path)
            row = connection.execute(
                "SELECT file_size, content_hash FROM audio_urls WHERE id=?", (row_id,)
            ).fetchone()
            connection.close()
            self.assertEqual(video.read_bytes(), b"video-original")
            self.assertEqual(audio.read_bytes(), b"audio-original")
        self.assertEqual((total, content_hash), (999, "new-hash"))
        self.assertEqual(row, (999, "new-hash"))

    def test_database_compare_and_swap_failure_restores_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = (
                root / "downloads" / "bilibili" / "访谈" / "BV1xx411c7mD_p1"
                / "BV1xx411c7mD-p1-c1-test"
            )
            bundle.mkdir(parents=True)
            (bundle / "source.mp4").write_bytes(b"video-original")
            (bundle / "audio.wav").write_bytes(b"audio-original")
            original = b'{"original":true}\n'
            (bundle / "metadata.json").write_bytes(original)
            db_path, row_id = self._database_row(root, bundle)
            candidate = self._candidate(bundle, row_id)
            connection = sqlite3.connect(db_path)
            connection.execute(
                "UPDATE audio_urls SET content_hash=? WHERE id=?", ("b" * 64, row_id)
            )
            connection.commit()
            connection.close()
            with mock.patch.object(backfill, "_verify_pre_mutation", return_value={}), \
                 mock.patch.object(backfill, "validate_bundle", return_value={}), \
                 mock.patch.object(
                     backfill, "bundle_fingerprint", return_value=(99, "new-hash")
                 ):
                with self.assertRaises(RuntimeError):
                    backfill._commit_sidecar_and_database(
                        db_path, candidate, {"files": {}, "caption": {}}, []
                    )
            self.assertEqual((bundle / "metadata.json").read_bytes(), original)
            connection = sqlite3.connect(db_path)
            content_hash = connection.execute(
                "SELECT content_hash FROM audio_urls WHERE id=?", (row_id,)
            ).fetchone()[0]
            connection.close()
        self.assertEqual(content_hash, "b" * 64)

    def test_base_exception_in_mutation_window_restores_idempotently(self):
        class SyntheticAbort(BaseException):
            pass

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = (
                root / "downloads" / "bilibili" / "访谈" / "BV1xx411c7mD_p1"
                / "BV1xx411c7mD-p1-c1-test"
            )
            bundle.mkdir(parents=True)
            (bundle / "source.mp4").write_bytes(b"video-original")
            (bundle / "audio.wav").write_bytes(b"audio-original")
            original = b'{"original":true}\n'
            (bundle / "metadata.json").write_bytes(original)
            db_path, row_id = self._database_row(root, bundle)
            stage = root / "stage"
            stage.mkdir()
            paths = {}
            for extension in ("json", "vtt", "txt"):
                path = stage / f"captions.ai-zh.automatic.1.{extension}"
                path.write_text(extension, encoding="utf-8")
                paths[extension] = path
            prepared = [{
                "index": 0, "paths": paths,
                "track": {"id_str": "1", "language": "ai-zh"},
            }]
            with mock.patch.object(backfill, "_verify_pre_mutation", return_value={}), \
                 mock.patch.object(backfill, "validate_bundle", return_value={}), \
                 mock.patch.object(
                     backfill, "bundle_fingerprint", side_effect=SyntheticAbort()
                 ):
                with self.assertRaises(SyntheticAbort):
                    backfill._commit_sidecar_and_database(
                        db_path, self._candidate(bundle, row_id),
                        {"files": {}, "caption": {}}, prepared,
                    )
                backfill._restore_bundle(bundle / "metadata.json", original, [])
            self.assertEqual((bundle / "metadata.json").read_bytes(), original)
            self.assertEqual(list(bundle.glob("captions.*")), [])

    def test_empty_refresh_explicitly_clears_track_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = (
                root / "downloads" / "bilibili" / "访谈" / "BV1xx411c7mD_p1"
                / "BV1xx411c7mD-p1-c1-test"
            )
            bundle.mkdir(parents=True)
            (bundle / "source.mp4").write_bytes(b"video-original")
            (bundle / "audio.wav").write_bytes(b"audio-original")
            (bundle / "metadata.json").write_text("{}\n", encoding="utf-8")
            db_path, row_id = self._database_row(root, bundle)
            metadata = {
                "files": {},
                "caption": {"tracks": [{"stale": True}], "track_count": 1},
            }
            with mock.patch.object(backfill, "_verify_pre_mutation", return_value={}), \
                 mock.patch.object(backfill, "validate_bundle", return_value={}), \
                 mock.patch.object(
                     backfill, "bundle_fingerprint", return_value=(99, "empty-hash")
                 ):
                backfill._commit_sidecar_and_database(
                    db_path, self._candidate(bundle, row_id), metadata, []
                )
            persisted = json.loads(
                (bundle / "metadata.json").read_text(encoding="utf-8")
            )
        self.assertEqual(persisted["caption"]["tracks"], [])
        self.assertEqual(persisted["caption"]["track_count"], 0)


if __name__ == "__main__":
    unittest.main()
