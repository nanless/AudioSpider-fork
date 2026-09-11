import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
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
        self.assertFalse(args.retry_invalid_timeline)
        self.assertFalse(args.visual_ocr)
        self.assertEqual(args.job_key, [])
        self.assertEqual(args.ocr_engine, "paddle")
        self.assertEqual(args.ocr_sample_fps, 4.0)
        self.assertEqual(args.ocr_region, (0.0, 0.5, 1.0, 1.0))
        self.assertEqual(args.ocr_min_confidence, 0.80)

    def test_parser_accepts_repeatable_exact_job_keys(self):
        args = backfill.build_parser().parse_args([
            "--job-key", "first", "--job-key", "second",
        ])
        self.assertEqual(args.job_key, ["first", "second"])

    def test_visual_ocr_cli_config_is_explicit_and_validated(self):
        args = backfill.build_parser().parse_args([
            "--visual-ocr", "--ocr-engine", "paddle",
            "--ocr-sample-fps", "2", "--ocr-region", "0.1,0.7,0.9,0.98",
            "--ocr-min-confidence", "0.9", "--ocr-profile", "sample-v2",
        ])
        self.assertTrue(args.visual_ocr)
        self.assertFalse(args.apply)
        self.assertEqual(args.ocr_region, (0.1, 0.7, 0.9, 0.98))
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            backfill.build_parser().parse_args(["--ocr-region", "0,1,1,0"])

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

    def test_prepare_rejects_mismatched_timeline_without_persisted_payloads(self):
        track = {
            "url": "https://aisubtitle.hdslb.com/test.json",
            "selection_rank": 0,
            "id_str": "1",
            "language": "ai-zh",
            "kind": "automatic",
        }
        client = mock.Mock()
        client.subtitle = mock.AsyncMock(return_value={"body": [
            {"from": 0.0, "to": 210.4, "content": "错配字幕"},
        ]})
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            prepared, rejected = __import__("asyncio").run(
                backfill._prepare_captions(client, [track], stage, 8.576)
            )
            self.assertEqual(len(list(stage.glob("*.rejected.json"))), 1)
            self.assertEqual(len(list(stage.glob("*.vtt"))), 0)
            self.assertEqual(len(list(stage.glob("*.txt"))), 0)
        self.assertEqual(prepared, [])
        self.assertEqual(len(rejected), 1)
        rejection = rejected[0]["track"]["timeline_rejection"]
        self.assertEqual(rejection["reason"], "caption_exceeds_media_duration")
        self.assertAlmostEqual(rejection["overrun_seconds"], 201.824)

    def test_prepare_keeps_valid_track_and_quarantines_only_bad_track(self):
        tracks = [{
            "url": "https://aisubtitle.hdslb.com/manual.json",
            "selection_rank": 0, "id_str": "1", "language": "zh-Hans",
            "kind": "manual",
        }, {
            "url": "https://aisubtitle.hdslb.com/auto.json",
            "selection_rank": 1, "id_str": "2", "language": "ai-zh",
            "kind": "automatic",
        }]
        client = mock.Mock()
        client.subtitle = mock.AsyncMock(side_effect=[
            {"body": [{"from": 0.0, "to": 8.0, "content": "人工"}]},
            {"body": [{"from": 0.0, "to": 50.0, "content": "错配自动"}]},
        ])
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            stale = stage / "captions.stale.automatic.9.vtt"
            stale.write_text("stale", encoding="utf-8")
            accepted, rejected = __import__("asyncio").run(
                backfill._prepare_captions(client, tracks, stage, 10.0)
            )
            self.assertFalse(stale.exists())
            self.assertEqual(len(list(stage.glob("*.vtt"))), 1)
            self.assertEqual(len(list(stage.glob("*.txt"))), 1)
            self.assertEqual(len(list(stage.glob("*.json"))), 2)
        self.assertEqual([row["track"]["id_str"] for row in accepted], ["1"])
        self.assertEqual([row["track"]["id_str"] for row in rejected], ["2"])

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
        self.assertFalse(report["visual_ocr_enabled"])
        self.assertEqual(report["candidates"][0]["bundle_path"], str(candidate["bundle"]))

    def test_visual_ocr_dry_run_constructs_neither_network_nor_engine(self):
        args = backfill.build_parser().parse_args(["--visual-ocr"])
        candidate = {
            "id": 1, "source_id": "BV1xx411c7mD_p1", "job_key": "job",
            "bundle": Path("/data/downloads/bilibili/访谈/BV1xx411c7mD_p1/job"),
            "_bundle_path_db": "/data/ignored", "_old_file_size": 12,
            "_old_content_hash": "a" * 64, "caption_status": "unknown",
        }
        with mock.patch.object(backfill, "discover_candidates", return_value=[candidate]), \
             mock.patch.object(backfill, "BilibiliClient") as client, \
             mock.patch.object(backfill, "_create_visual_ocr_engine") as engine, \
             redirect_stdout(StringIO()) as output:
            result = __import__("asyncio").run(backfill.async_main(args))
        self.assertEqual(result, 0)
        client.assert_not_called()
        engine.assert_not_called()
        report = json.loads(output.getvalue())
        self.assertTrue(report["visual_ocr_enabled"])

    def test_exact_job_key_not_eligible_is_a_nonzero_dry_run(self):
        args = backfill.build_parser().parse_args([
            "--job-key", "missing-job",
        ])
        with mock.patch.object(backfill, "discover_candidates", return_value=[]), \
             redirect_stdout(StringIO()) as output:
            result = __import__("asyncio").run(backfill.async_main(args))
        self.assertEqual(result, 2)
        report = json.loads(output.getvalue())
        self.assertEqual(report["requested_job_keys_not_eligible"], ["missing-job"])

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

    def test_invalid_timeline_requires_explicit_retry_flag(self):
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
                "caption": {"status": "invalid_timeline"},
            }
            with mock.patch.object(
                backfill, "validate_bundle", return_value={"metadata": metadata}
            ):
                default = backfill.discover_candidates(
                    db_path, root / "downloads", 10
                )
                explicit = backfill.discover_candidates(
                    db_path, root / "downloads", 10,
                    include_invalid_timeline=True,
                )
        self.assertEqual(default, [])
        self.assertEqual(len(explicit), 1)

    def test_discovery_can_filter_one_exact_job_key(self):
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
                selected = backfill.discover_candidates(
                    db_path, root / "downloads", 10,
                    job_keys={bundle.name},
                )
                omitted = backfill.discover_candidates(
                    db_path, root / "downloads", 10,
                    job_keys={"some-other-job"},
                )
        self.assertEqual([row["job_key"] for row in selected], [bundle.name])
        self.assertEqual(omitted, [])

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

    def test_interrupted_precommit_journal_rolls_bundle_back_exactly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = (
                root / "downloads/bilibili/访谈/BV1xx411c7mD_p1"
                / "BV1xx411c7mD-p1-c1-test"
            )
            bundle.mkdir(parents=True)
            old_asset = bundle / "captions.old.txt"
            old_asset.write_bytes(b"old-caption")
            original_sidecar = b'{"original":true}\n'
            sidecar = bundle / "metadata.json"
            sidecar.write_bytes(original_sidecar)
            db_path, row_id = self._database_row(root, bundle)
            candidate = self._candidate(bundle, row_id)
            stage = root / "stage"
            stage.mkdir()
            new_asset = stage / "captions.new.txt"
            new_asset.write_bytes(b"new-caption")
            journal_dir = backfill._journal_directory(bundle, candidate["job_key"])
            journal = backfill._write_repair_journal(
                journal_dir, candidate, sidecar,
                [(old_asset.name, old_asset)], [new_asset],
            )
            backup = journal_dir / journal["old_assets"][0]["backup"]
            old_asset.replace(backup)
            (bundle / new_asset.name).write_bytes(new_asset.read_bytes())
            sidecar.write_bytes(b'{"half_applied":true}\n')
            with mock.patch.object(backfill, "validate_bundle", return_value={}), \
                 mock.patch.object(
                     backfill, "bundle_fingerprint",
                     return_value=(12, "a" * 64),
                 ):
                result = backfill._recover_one_journal(
                    db_path, root / "downloads", journal_dir
                )
            self.assertEqual(result["action"], "rolled_back")
            self.assertEqual(old_asset.read_bytes(), b"old-caption")
            self.assertFalse((bundle / new_asset.name).exists())
            self.assertEqual(sidecar.read_bytes(), original_sidecar)
            self.assertFalse(journal_dir.exists())

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
                "media": {"duration_seconds": 10.0},
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
            metadata = {"files": {}, "caption": {
                "payload_status": "rejected",
                "rejected_track_count": 1,
                "rejected_tracks": [{"stale": True}],
            }}
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
            persisted = json.loads(
                (bundle / "metadata.json").read_text(encoding="utf-8")
            )
        self.assertEqual((total, content_hash), (999, "new-hash"))
        self.assertEqual(row, (999, "new-hash"))
        self.assertNotIn("payload_status", persisted["caption"])
        self.assertNotIn("rejected_tracks", persisted["caption"])

    def test_rejected_raw_caption_updates_closure_without_changing_media(self):
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
            raw = stage / "captions.ai-zh.automatic.1.rejected.json"
            raw.write_text('{"body":[]}\n', encoding="utf-8")
            rejected = [{
                "index": 0,
                "paths": {"json": raw},
                "track": {
                    "id_str": "1", "language": "ai-zh", "status": "rejected",
                    "files": {"json": "caption_rejected_0_json"},
                },
            }]
            metadata = {"files": {}, "caption": {"status": "invalid_timeline"}}
            with mock.patch.object(
                backfill, "_verify_pre_mutation", return_value={}
            ), mock.patch.object(
                backfill, "validate_bundle", return_value={}
            ), mock.patch.object(
                backfill, "bundle_fingerprint", return_value=(123, "new-hash")
            ):
                backfill._commit_sidecar_and_database(
                    db_path, self._candidate(bundle, row_id), metadata, [], rejected
                )
            persisted = json.loads(
                (bundle / "metadata.json").read_text(encoding="utf-8")
            )
            connection = sqlite3.connect(db_path)
            row = connection.execute(
                "SELECT file_size, content_hash FROM audio_urls WHERE id=?", (row_id,)
            ).fetchone()
            connection.close()
            video_bytes = video.read_bytes()
            audio_bytes = audio.read_bytes()
        self.assertEqual(video_bytes, b"video-original")
        self.assertEqual(audio_bytes, b"audio-original")
        self.assertEqual(persisted["caption"]["track_count"], 0)
        self.assertEqual(persisted["caption"]["rejected_track_count"], 1)
        self.assertEqual(persisted["caption"]["payload_status"], "rejected")
        self.assertEqual(row, (123, "new-hash"))

    def test_retry_invalid_timeline_to_valid_replaces_old_caption_closure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "downloads" / "bilibili" / "访谈" / "source" / "job"
            bundle.mkdir(parents=True)
            (bundle / "source.mp4").write_bytes(b"video")
            (bundle / "audio.wav").write_bytes(b"audio")
            old = bundle / "captions.ai-zh.automatic.same.1.rejected.json"
            old.write_bytes(b"old-rejected")
            (bundle / "metadata.json").write_text("{}\n", encoding="utf-8")
            db_path, row_id = self._database_row(root, bundle)
            candidate = self._candidate(bundle, row_id)
            stage = root / "stage"
            stage.mkdir()
            paths = {}
            for extension in ("json", "vtt", "txt"):
                path = stage / f"captions.ai-zh.automatic.same.1.{extension}"
                path.write_text(f"new-{extension}", encoding="utf-8")
                paths[extension] = path
            metadata = {
                "files": {
                    "video": {"path": "source.mp4"},
                    "audio": {"path": "audio.wav"},
                    "caption_rejected_0_json": {"path": old.name},
                },
                "caption": {
                    "payload_status": "rejected", "rejected_track_count": 1,
                    "rejected_tracks": [{"files": {"json": "caption_rejected_0_json"}}],
                },
            }
            prepared = [{
                "index": 0, "paths": paths,
                "track": {"id_str": "same", "language": "ai-zh"},
            }]
            with mock.patch.object(backfill, "_verify_pre_mutation", return_value={}), \
                 mock.patch.object(backfill, "validate_bundle", return_value={}), \
                 mock.patch.object(
                     backfill, "bundle_fingerprint", return_value=(321, "valid-hash")
                 ):
                backfill._commit_sidecar_and_database(
                    db_path, candidate, metadata, prepared
                )
            persisted = json.loads(
                (bundle / "metadata.json").read_text(encoding="utf-8")
            )
            self.assertFalse(old.exists())
            self.assertNotIn("caption_rejected_0_json", persisted["files"])
            self.assertEqual(
                set(persisted["files"]),
                {"video", "audio", "caption_0_json", "caption_0_vtt", "caption_0_txt"},
            )
            self.assertEqual(list(bundle.parent.glob(".*.caption-rollback-*")), [])

    def test_retry_invalid_timeline_to_rejected_replaces_same_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "downloads" / "bilibili" / "访谈" / "source" / "job"
            bundle.mkdir(parents=True)
            (bundle / "source.mp4").write_bytes(b"video")
            (bundle / "audio.wav").write_bytes(b"audio")
            name = "captions.ai-zh.automatic.same.1.rejected.json"
            old = bundle / name
            old.write_bytes(b"old-rejected")
            (bundle / "metadata.json").write_text("{}\n", encoding="utf-8")
            db_path, row_id = self._database_row(root, bundle)
            stage = root / "stage"
            stage.mkdir()
            replacement = stage / name
            replacement.write_bytes(b"new-rejected")
            metadata = {
                "files": {
                    "video": {"path": "source.mp4"},
                    "audio": {"path": "audio.wav"},
                    "caption_rejected_0_json": {"path": name},
                },
                "caption": {
                    "tracks": [],
                    "rejected_tracks": [{
                        "files": {"json": "caption_rejected_0_json"}
                    }],
                },
            }
            rejected = [{
                "index": 0, "paths": {"json": replacement},
                "track": {
                    "id_str": "same", "language": "ai-zh", "status": "rejected",
                    "files": {"json": "caption_rejected_0_json"},
                },
            }]
            with mock.patch.object(backfill, "_verify_pre_mutation", return_value={}), \
                 mock.patch.object(backfill, "validate_bundle", return_value={}), \
                 mock.patch.object(
                     backfill, "bundle_fingerprint", return_value=(322, "rejected-hash")
                 ):
                backfill._commit_sidecar_and_database(
                    db_path, self._candidate(bundle, row_id), metadata, [], rejected
                )
            self.assertEqual(old.read_bytes(), b"new-rejected")
            self.assertEqual(list(bundle.parent.glob(".*.caption-rollback-*")), [])

    def test_retry_failure_restores_displaced_caption_sidecar_and_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "downloads" / "bilibili" / "访谈" / "source" / "job"
            bundle.mkdir(parents=True)
            (bundle / "source.mp4").write_bytes(b"video")
            (bundle / "audio.wav").write_bytes(b"audio")
            name = "captions.ai-zh.automatic.same.1.rejected.json"
            old = bundle / name
            old.write_bytes(b"old-rejected")
            original_sidecar = b'{"original":true}\n'
            (bundle / "metadata.json").write_bytes(original_sidecar)
            db_path, row_id = self._database_row(root, bundle)
            stage = root / "stage"
            stage.mkdir()
            replacement = stage / name
            replacement.write_bytes(b"new-rejected")
            metadata = {
                "files": {
                    "video": {"path": "source.mp4"},
                    "audio": {"path": "audio.wav"},
                    "caption_rejected_0_json": {"path": name},
                },
                "caption": {
                    "tracks": [],
                    "rejected_tracks": [{
                        "files": {"json": "caption_rejected_0_json"}
                    }],
                },
            }
            rejected = [{
                "index": 0, "paths": {"json": replacement},
                "track": {
                    "id_str": "same", "language": "ai-zh", "status": "rejected",
                    "files": {"json": "caption_rejected_0_json"},
                },
            }]
            with mock.patch.object(backfill, "_verify_pre_mutation", return_value={}), \
                 mock.patch.object(
                     backfill, "validate_bundle",
                     side_effect=[ValueError("synthetic validation failure"), {}],
                 ), mock.patch.object(
                     backfill, "bundle_fingerprint", return_value=(323, "new-hash")
                 ):
                with self.assertRaises(ValueError):
                    backfill._commit_sidecar_and_database(
                        db_path, self._candidate(bundle, row_id), metadata, [], rejected
                    )
            self.assertEqual(old.read_bytes(), b"old-rejected")
            self.assertEqual(
                (bundle / "metadata.json").read_bytes(), original_sidecar
            )
            self.assertEqual(list(bundle.parent.glob(".*.caption-rollback-*")), [])
            connection = sqlite3.connect(db_path)
            row = connection.execute(
                "SELECT file_size, content_hash FROM audio_urls WHERE id=?", (row_id,)
            ).fetchone()
            connection.close()
            self.assertEqual(row, (12, "a" * 64))

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

    def test_platform_refresh_removes_only_caption_namespace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "downloads/bilibili/访谈/BV1xx411c7mD_p1/BV1xx411c7mD-p1-c1-test"
            bundle.mkdir(parents=True)
            for name, data in {
                "source.mp4": b"video", "audio.wav": b"audio",
                "captions.old.txt": b"old-caption",
                "visual_ocr.json": b"visual", "future.asset": b"future",
            }.items():
                (bundle / name).write_bytes(data)
            (bundle / "metadata.json").write_text("{}\n", encoding="utf-8")
            db_path, row_id = self._database_row(root, bundle)
            stage = root / "stage"
            stage.mkdir()
            replacement = stage / "captions.zh.manual.new.txt"
            replacement.write_bytes(b"new-caption")
            metadata = {
                "files": {
                    "video": {"path": "source.mp4"},
                    "audio": {"path": "audio.wav"},
                    "caption_0_txt": {"path": "captions.old.txt"},
                    "visual_ocr_json": {"path": "visual_ocr.json"},
                    "future_derivation": {"path": "future.asset"},
                },
                "caption": {
                    "tracks": [{"files": {"txt": "caption_0_txt"}}],
                },
                "derived_text": {"visual_ocr": {
                    "sentinel": True,
                    "files": {"json": "visual_ocr_json"},
                }},
            }
            prepared = [{
                "index": 0, "paths": {"txt": replacement},
                "track": {"id_str": "new", "language": "zh"},
            }]
            with mock.patch.object(backfill, "_verify_pre_mutation", return_value={}), \
                 mock.patch.object(backfill, "validate_bundle", return_value={}), \
                 mock.patch.object(
                     backfill, "bundle_fingerprint", return_value=(100, "new-hash")
                 ):
                backfill._commit_sidecar_and_database(
                    db_path, self._candidate(bundle, row_id), metadata, prepared
                )
            persisted = json.loads(
                (bundle / "metadata.json").read_text(encoding="utf-8")
            )
            self.assertFalse((bundle / "captions.old.txt").exists())
            self.assertEqual((bundle / "visual_ocr.json").read_bytes(), b"visual")
            self.assertEqual((bundle / "future.asset").read_bytes(), b"future")
            self.assertIn("visual_ocr_json", persisted["files"])
            self.assertIn("future_derivation", persisted["files"])

    def test_platform_caption_prevents_engine_construction(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "downloads/bilibili/访谈/source/job"
            bundle.mkdir(parents=True)
            metadata = {
                "bvid": "BV1xx411c7mD", "cid": 1,
                "content_language": "zh", "files": {},
                "media": {"duration_seconds": 10.0},
                "caption": {"requested_languages": ["zh"]},
                "acquisition_policy": {"require_caption": False},
            }
            prepared = [{"index": 0, "paths": {}, "track": {}}]
            client = mock.Mock(authenticated=False)
            factory = mock.Mock()

            def commit(_db, _candidate, committed, accepted, _rejected,
                       *, visual_ocr=None):
                committed["caption"].update({"tracks": [], "track_count": 1})
                self.assertIsNone(visual_ocr)
                return 200, "hash"

            with mock.patch.object(
                backfill, "_verify_pre_mutation", return_value={"metadata": metadata}
            ), mock.patch.object(
                backfill, "resolve_caption_inventory",
                mock.AsyncMock(return_value=(
                    {"need_login_subtitle": False}, [{"language": "zh"}],
                    "downloaded", [{"attempt": 1}],
                )),
            ), mock.patch.object(
                backfill, "_prepare_captions",
                mock.AsyncMock(return_value=(prepared, [])),
            ), mock.patch.object(
                backfill, "_commit_sidecar_and_database", side_effect=commit
            ):
                __import__("asyncio").run(backfill.repair_candidate(
                    client, Path(temporary) / "db", self._candidate(bundle, 1),
                    enable_visual_ocr=True, visual_ocr_config=mock.sentinel.config,
                    visual_ocr_engine_factory=factory,
                ))
            factory.assert_not_called()

    def test_missing_platform_caption_uses_mock_engine_and_ocr(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "downloads/bilibili/访谈/source/job"
            bundle.mkdir(parents=True)
            metadata = {
                "bvid": "BV1xx411c7mD", "cid": 1,
                "content_language": "zh", "files": {},
                "media": {"duration_seconds": 10.0},
                "caption": {"requested_languages": ["zh"]},
                "acquisition_policy": {"require_caption": False},
            }
            ocr_result = {
                "document": {
                    "status": "downloaded", "profile": "test-profile",
                    "cue_count": 1, "cues": [], "text_source": "visual_ocr",
                },
                "payloads": {"vtt": "WEBVTT\n\n", "txt": "你好\n"},
            }
            client = mock.Mock(authenticated=False)
            engine = mock.sentinel.engine
            factory = mock.Mock(return_value=engine)
            captured = {}

            def commit(_db, _candidate, committed, accepted, rejected,
                       *, visual_ocr=None):
                committed["caption"].update({"tracks": [], "track_count": 0})
                committed["derived_text"] = {"visual_ocr": {"status": "downloaded"}}
                captured["visual_ocr"] = visual_ocr
                return 201, "hash"

            with mock.patch.object(
                backfill, "_verify_pre_mutation", return_value={"metadata": metadata}
            ), mock.patch.object(
                backfill, "resolve_caption_inventory",
                mock.AsyncMock(return_value=(
                    {"need_login_subtitle": False}, [], "not_provided_publicly",
                    [{"attempt": 1}],
                )),
            ), mock.patch.object(
                backfill, "_prepare_captions", mock.AsyncMock(return_value=([], [])),
            ), mock.patch.object(
                backfill, "_prepare_visual_ocr", return_value=ocr_result
            ) as visual, mock.patch.object(
                backfill, "_commit_sidecar_and_database", side_effect=commit
            ):
                result = __import__("asyncio").run(backfill.repair_candidate(
                    client, Path(temporary) / "db", self._candidate(bundle, 1),
                    enable_visual_ocr=True, visual_ocr_config=mock.sentinel.config,
                    visual_ocr_engine_factory=factory,
                ))
            factory.assert_called_once_with()
            visual.assert_called_once_with(
                bundle / "source.mp4", config=mock.sentinel.config, engine=engine,
                timeout_seconds=14_400,
            )
            self.assertEqual(captured["visual_ocr"]["document"],
                             ocr_result["document"])
            self.assertEqual(result["visual_ocr_status"], "downloaded")

    def test_visual_ocr_failure_restores_previous_namespace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "downloads/bilibili/访谈/BV1xx411c7mD_p1/BV1xx411c7mD-p1-c1-test"
            bundle.mkdir(parents=True)
            for name, data in {
                "source.mp4": b"video", "audio.wav": b"audio",
                "visual_ocr.json": b"old-visual",
            }.items():
                (bundle / name).write_bytes(data)
            original = b'{"original":true}\n'
            (bundle / "metadata.json").write_bytes(original)
            db_path, row_id = self._database_row(root, bundle)
            stage = root / "stage"
            stage.mkdir()
            new_visual = stage / "visual_ocr.json"
            new_visual.write_bytes(b"new-visual")
            metadata = {
                "files": {
                    "video": {"path": "source.mp4"},
                    "audio": {"path": "audio.wav"},
                    "visual_ocr_json": {"path": "visual_ocr.json"},
                },
                "caption": {}, "acquisition_policy": {},
                "derived_text": {"visual_ocr": {
                    "status": "downloaded",
                    "files": {"json": "visual_ocr_json"},
                }},
            }
            with mock.patch.object(backfill, "_verify_pre_mutation", return_value={}), \
                 mock.patch.object(
                     backfill, "validate_bundle",
                     side_effect=[ValueError("synthetic failure"), {}],
                 ), mock.patch.object(
                     backfill, "bundle_fingerprint", return_value=(102, "hash")
                 ):
                with self.assertRaises(ValueError):
                    backfill._commit_sidecar_and_database(
                        db_path, self._candidate(bundle, row_id), metadata, [],
                        visual_ocr={
                            "document": {
                                "status": "no_stable_text_detected",
                                "profile": "test-profile",
                            },
                            "paths": {"json": new_visual},
                        },
                    )
            self.assertEqual((bundle / "visual_ocr.json").read_bytes(), b"old-visual")
            self.assertEqual((bundle / "metadata.json").read_bytes(), original)
            row = sqlite3.connect(db_path).execute(
                "SELECT file_size, content_hash FROM audio_urls WHERE id=?", (row_id,)
            ).fetchone()
        self.assertEqual(row, (12, "a" * 64))


if __name__ == "__main__":
    unittest.main()
