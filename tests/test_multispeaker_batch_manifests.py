import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from scripts.audit_multispeaker_batch import (
    _open_readonly_database,
    audit_artifacts,
    load_and_validate_batch,
    main as audit_main,
)


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "config" / "multispeaker_video_100_20260912.batch.json"


class MultispeakerBatchManifestTests(unittest.TestCase):
    def test_exact_six_cell_quota_and_unique_ids(self):
        batch = load_and_validate_batch(INDEX)
        self.assertEqual(batch["batch_id"], "multispeaker-video-100-20260912")
        self.assertEqual(len(batch["items"]["bilibili"]), 50)
        self.assertEqual(len(batch["items"]["youtube"]), 50)
        self.assertEqual(
            len({item["bvid"] for item in batch["items"]["bilibili"]}), 50
        )
        self.assertEqual(
            len({item["video_id"] for item in batch["items"]["youtube"]}), 50
        )

    def test_every_item_keeps_conservative_provenance(self):
        batch = load_and_validate_batch(INDEX)
        for items in batch["items"].values():
            for item in items:
                self.assertFalse(item["require_caption"])
                self.assertIsNone(item["speaker_count"])
                self.assertEqual(item["speaker_count_status"], "needs_review")
                self.assertEqual(item["rights"]["status"], "needs_review")
                self.assertEqual(item["ai_generation"]["status"], "unknown")
                evidence = item["candidate_metadata"]["multi_speaker_evidence"]
                self.assertEqual(evidence["status"], "candidate_unverified")
                self.assertGreaterEqual(evidence["minimum_possible_speakers"], 2)


class MultispeakerBatchArtifactAuditTests(unittest.TestCase):
    batch_id = "test-multispeaker-batch"
    video_id = "u7TwqpWiY5s"
    job_key = "u7TwqpWiY5s-youtube_interviews-en-test"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db = self.root / "audiospider.db"
        connection = sqlite3.connect(self.db)
        connection.execute(
            "CREATE TABLE audio_urls ("
            "source TEXT, source_id TEXT, job_key TEXT, category TEXT, status TEXT, "
            "bundle_path TEXT, local_path TEXT, content_hash TEXT, metadata_json TEXT, "
            "artifact_kind TEXT)"
        )
        connection.commit()
        connection.close()
        self.batch = {
            "batch_id": self.batch_id,
            "baseline": {"status": "not_configured"},
            "items": {
                "bilibili": [],
                "youtube": [{
                    "video_id": self.video_id,
                    "dataset_category": "访谈",
                }],
            },
        }

    def tearDown(self):
        self.temporary.cleanup()

    def _insert_row(self, *, status="done", bundle_path="", local_path=""):
        envelope = {
            "source_data": {
                "youtube": {"job": {"batch_id": self.batch_id}},
            },
        }
        connection = sqlite3.connect(self.db)
        connection.execute(
            "INSERT INTO audio_urls VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "youtube", self.video_id, self.job_key, "访谈", status,
                bundle_path, local_path, "a" * 64,
                json.dumps(envelope), "video_bundle",
            ),
        )
        connection.commit()
        connection.close()

    def _make_bundle(self):
        bundle = (
            self.root / "downloads" / "youtube" / "访谈"
            / self.video_id / self.job_key
        )
        bundle.mkdir(parents=True)
        (bundle / "source.mp4").write_bytes(b"video")
        (bundle / "metadata.json").write_text("{}", encoding="utf-8")
        return bundle

    def _validated_result(self):
        return {
            "metadata": {
                "source": "youtube",
                "source_id": self.video_id,
                "job_key": self.job_key,
                "dataset_category": "访谈",
                "caption": {
                    "status": "downloaded",
                    "kind": "automatic",
                    "text_source": "platform_auto",
                },
                "rights": {"status": "needs_review"},
                "ai_generation": {"status": "unknown", "evidence": []},
                "speaker_count": None,
                "speaker_count_status": "needs_review",
            },
            "video_duration": 12.5,
        }

    def test_database_connection_is_enforced_read_only(self):
        connection = _open_readonly_database(self.db)
        try:
            self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("CREATE TABLE forbidden(value TEXT)")
        finally:
            connection.close()

    def test_exact_artifact_audit_validates_and_summarizes_one_bundle(self):
        bundle = self._make_bundle()
        self._insert_row(
            bundle_path=str(bundle), local_path=str(bundle / "source.mp4")
        )
        unrelated = (
            self.root / "downloads" / "youtube" / "访谈"
            / ".staging" / "unrelated-historical-job"
        )
        unrelated.mkdir(parents=True)
        (unrelated / "old.part").write_bytes(b"old")
        validated_paths = []

        def validate(path):
            validated_paths.append(path)
            return self._validated_result()

        report = audit_artifacts(
            self.batch, self.db, self.root / "downloads",
            database_report={"complete": True},
            validators={"youtube": validate},
            fingerprint=lambda path: (7, "a" * 64),
        )

        self.assertTrue(report["complete"])
        self.assertEqual(report["audited_rows"], 1)
        self.assertEqual(report["total_bytes"], 7)
        self.assertEqual(report["total_duration_seconds"], 12.5)
        self.assertEqual(report["staging_count"], 0)
        self.assertEqual(report["partial_count"], 0)
        self.assertEqual(report["summary"]["caption_track_kind"], {"automatic": 1})
        self.assertEqual(report["summary"]["speaker_count_values"], {"unknown": 1})
        self.assertEqual(validated_paths, [(bundle / "metadata.json").resolve()])

    def test_target_staging_and_partial_fail_closed(self):
        self._insert_row(status="downloading")
        stage = (
            self.root / "downloads" / "youtube" / "访谈"
            / ".staging" / self.job_key
        )
        stage.mkdir(parents=True)
        (stage / "source.mp4.part-Frag12").write_bytes(b"partial")

        report = audit_artifacts(
            self.batch, self.db, self.root / "downloads",
            database_report={"complete": False},
            validators={"youtube": lambda path: self.fail("validator must not run")},
            fingerprint=lambda path: self.fail("fingerprint must not run"),
        )

        self.assertFalse(report["complete"])
        self.assertEqual(report["staging_count"], 1)
        self.assertEqual(report["partial_count"], 1)
        self.assertEqual(report["failures"][0]["code"], "row_not_done")

    def test_bilibili_integrated_sidecar_need_not_duplicate_dataset_category(self):
        bvid = "BV1xx411c7mD"
        source_id = f"{bvid}_p1"
        job_key = f"{source_id}-c123456-test"
        bundle = (
            self.root / "downloads" / "bilibili" / "影视"
            / source_id / job_key
        )
        bundle.mkdir(parents=True)
        (bundle / "source.mp4").write_bytes(b"video")
        (bundle / "metadata.json").write_text("{}", encoding="utf-8")
        envelope = {
            "source_data": {
                "bilibili": {"download_task": {"batch_id": self.batch_id}},
            },
        }
        connection = sqlite3.connect(self.db)
        connection.execute(
            "INSERT INTO audio_urls VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "bilibili", source_id, job_key, "影视", "done",
                str(bundle), str(bundle / "source.mp4"), "b" * 64,
                json.dumps(envelope), "video_bundle",
            ),
        )
        connection.commit()
        connection.close()
        batch = {
            "batch_id": self.batch_id,
            "baseline": {"status": "not_configured"},
            "items": {
                "bilibili": [{"bvid": bvid, "dataset_category": "影视"}],
                "youtube": [],
            },
        }
        metadata = {
            "source": "bilibili",
            "source_id": source_id,
            "job_key": job_key,
            # Integrated Bilibili sidecars intentionally do not duplicate
            # dataset_category; manifest + SQLite + path prove classification.
            "caption": {"status": "missing", "tracks": []},
            "rights": {"status": "needs_review"},
            "ai_generation": {"status": "unknown", "evidence": []},
            "speaker_count": None,
            "speaker_count_status": "needs_review",
        }

        report = audit_artifacts(
            batch, self.db, self.root / "downloads",
            database_report={"complete": True},
            validators={
                "bilibili": lambda path: {
                    "metadata": metadata,
                    "video": {"duration_seconds": 21.25},
                },
            },
            fingerprint=lambda path: (11, "b" * 64),
        )

        self.assertTrue(report["complete"])
        self.assertEqual(report["audited_rows"], 1)
        self.assertEqual(report["summary"]["source_rows"], {"bilibili": 1})
        self.assertEqual(report["summary"]["visual_ocr_status"], {"not_run": 1})

    def test_validator_error_does_not_echo_url_or_payload_text(self):
        bundle = self._make_bundle()
        self._insert_row(
            bundle_path=str(bundle), local_path=str(bundle / "source.mp4")
        )

        def reject(_path):
            raise RuntimeError("https://secret.invalid/?token=value caption payload text")

        report = audit_artifacts(
            self.batch, self.db, self.root / "downloads",
            database_report={"complete": True},
            validators={"youtube": reject},
            fingerprint=lambda path: (7, "a" * 64),
        )
        serialized = json.dumps(report)
        self.assertFalse(report["complete"])
        self.assertNotIn("https://", serialized)
        self.assertNotIn("caption payload text", serialized)
        self.assertEqual(report["failures"][0]["error_type"], "RuntimeError")

    def test_default_cli_output_contract_has_no_artifacts_key(self):
        batch = {"batch_id": self.batch_id}
        database_report = {
            "batch_id": self.batch_id,
            "complete": False,
            "cells": {},
            "missing": [],
            "extras": [],
        }
        output = io.StringIO()
        with mock.patch(
            "scripts.audit_multispeaker_batch.load_and_validate_batch",
            return_value=batch,
        ), mock.patch(
            "scripts.audit_multispeaker_batch.audit_database",
            return_value=database_report,
        ), contextlib.redirect_stdout(output):
            result = audit_main(["--batch-index", "ignored", "--db", "ignored"])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue()), database_report)
        self.assertNotIn("artifacts", json.loads(output.getvalue()))


if __name__ == "__main__":
    unittest.main()
