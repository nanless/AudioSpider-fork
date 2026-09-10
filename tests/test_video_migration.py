import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import migrate_video_bundles as migration
from storage import AudioRecord


class VideoMigrationTests(unittest.TestCase):
    def test_bilibili_category_uses_controlled_title_mapping(self):
        self.assertEqual(migration._category({
            "source": "bilibili",
            "source_metadata": {"category": "", "title": "中文有声书合集"},
        }), "有声书")

    def test_failed_registration_rolls_moved_directory_back(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "legacy" / "job"
            destination = root / "downloads" / "bilibili" / "视频" / "BV_p1" / "job"
            source.mkdir(parents=True)
            (source / "metadata.json").write_text("{}", encoding="utf-8")
            job = {
                "source": "bilibili", "source_id": "BV_p1", "category": "视频",
                "source_path": source, "destination": destination,
                "metadata": {"source": "bilibili", "source_id": "BV_p1", "job_key": "job"},
            }
            record = AudioRecord(
                url="https://www.bilibili.com/video/BV?p=1", source="bilibili",
                artifact_kind="video_bundle", job_key="job", source_id="BV_p1",
            )
            database = root / "queue.db"
            with mock.patch("scripts.migrate_video_bundles.discover", return_value=[job]), \
                 mock.patch("scripts.migrate_video_bundles.discover_integrated", return_value=[]), \
                 mock.patch("scripts.migrate_video_bundles._database_has_job", return_value=False), \
                 mock.patch("scripts.migrate_video_bundles._backup_database", return_value=root / "backup"), \
                 mock.patch("scripts.migrate_video_bundles._validate"), \
                 mock.patch("scripts.migrate_video_bundles._build_record", return_value=record), \
                 mock.patch(
                     "scripts.migrate_video_bundles._register_in_transaction",
                     side_effect=RuntimeError("injected registration failure"),
                 ):
                report = migration.migrate([root / "legacy"], apply=True, db_path=database)

            self.assertEqual(report["failure_count"], 1)
            self.assertTrue(source.is_dir())
            self.assertFalse(destination.exists())
            self.assertTrue(report["jobs"][0]["rolled_back_move"])


if __name__ == "__main__":
    unittest.main()
