import argparse
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import doctor
import probe
from storage import AudioRecord, Storage


class DoctorTests(unittest.TestCase):
    def test_missing_database_is_a_warning(self):
        with tempfile.TemporaryDirectory() as root:
            check = doctor.check_database(Path(root) / "missing.db")
        self.assertEqual(check["status"], "warn")

    def test_existing_database_is_checked_read_only(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = Path(root) / "audio.db"
            Storage(str(db_path))
            before = os.path.getmtime(db_path)
            check = doctor.check_database(db_path)
            after = os.path.getmtime(db_path)
        self.assertEqual(check["status"], "ok")
        self.assertEqual(check["integrity"], "ok")
        self.assertEqual(before, after)

    def test_python_check_reports_executable(self):
        check = doctor.check_python()
        self.assertIn(check["status"], {"ok", "error"})
        self.assertTrue(check["executable"])

    def test_nested_missing_download_directory_does_not_crash(self):
        with tempfile.TemporaryDirectory() as root:
            nested = Path(root) / "missing" / "downloads"
            directory_check = doctor.check_directories(nested)
            disk_check = doctor.check_disk(nested)
        self.assertEqual(directory_check["status"], "ok")
        self.assertIn(disk_check["status"], {"ok", "error"})

    def test_invalid_disk_reserve_is_reported(self):
        previous = os.environ.get("AUDIOSPIDER_MIN_DISK_FREE_BYTES")
        os.environ["AUDIOSPIDER_MIN_DISK_FREE_BYTES"] = "invalid"
        try:
            check = doctor.check_disk(Path(tempfile.gettempdir()))
        finally:
            if previous is None:
                os.environ.pop("AUDIOSPIDER_MIN_DISK_FREE_BYTES", None)
            else:
                os.environ["AUDIOSPIDER_MIN_DISK_FREE_BYTES"] = previous
        self.assertEqual(check["status"], "error")

    def test_visual_ocr_check_reports_cuda_environment_without_credentials(self):
        completed = mock.Mock(
            returncode=0,
            stdout=(
                '{"paddle":"3.3.0","paddleocr":"3.7.0",'
                '"opencv":"4.12.0","cuda":true,"device":"gpu:0"}\n'
            ),
        )
        with tempfile.TemporaryDirectory() as root:
            executable = Path(root) / "python"
            executable.write_text("", encoding="utf-8")
            with mock.patch.dict(
                os.environ, {"BILIBILI_COOKIE": "secret"}
            ), mock.patch(
                "doctor.subprocess.run", return_value=completed
            ) as run:
                check = doctor.check_visual_ocr(executable)
        self.assertEqual(check["status"], "ok")
        environment = run.call_args.kwargs["env"]
        self.assertNotIn("BILIBILI_COOKIE", environment)


class ProbeTests(unittest.TestCase):
    def test_positive_int_rejects_zero(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            probe.positive_int("0")

    def test_database_snapshot_filters_current_source(self):
        with tempfile.TemporaryDirectory() as root:
            storage = Storage(str(Path(root) / "probe.db"))
            storage.add_url(AudioRecord(url="https://a.example/1.mp3", source="a"))
            storage.add_url(AudioRecord(url="https://b.example/1.mp3", source="b"))
            storage.add_url(AudioRecord(url="https://b.example/2.mp3", source="b"))
            total, rows = probe.database_snapshot(storage, "b")
        self.assertEqual(total, 3)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["source"] == "b" for row in rows))

    def test_rss_probe_applies_safe_limits(self):
        args = probe.build_parser().parse_args([
            "--source", "podcast_rss", "--feeds", "1", "--episodes", "3",
        ])
        spider = probe.configure_spider(args)
        self.assertEqual(len(spider.feeds), 1)
        self.assertEqual(spider.max_eps, 3)

    def test_librivox_probe_limits_books_and_chapters(self):
        args = probe.build_parser().parse_args([
            "--source", "librivox", "--books", "2", "--episodes", "3",
        ])
        spider = probe.configure_spider(args)
        self.assertEqual(spider.max_items, 2)
        self.assertEqual(spider.max_tracks_per_book, 3)

    def test_bilibili_probe_applies_safe_limits(self):
        args = probe.build_parser().parse_args([
            "--source", "bilibili", "--videos", "1", "--parts", "2",
            "--search-pages", "1", "--keywords", "测试关键词",
        ])
        spider = probe.configure_spider(args)
        self.assertEqual(spider.keywords, ["测试关键词"])
        self.assertEqual(spider.max_videos_per_keyword, 1)
        self.assertEqual(spider.max_pages_per_video, 2)


if __name__ == "__main__":
    unittest.main()
