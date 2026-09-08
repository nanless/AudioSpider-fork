import os
import tempfile
import unittest

from storage import AudioRecord, Storage


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.root.name, "audio.db")

    def tearDown(self):
        self.root.cleanup()

    def test_storage_instances_do_not_share_connections(self):
        first = Storage(self.db_path)
        second_path = os.path.join(self.root.name, "second.db")
        second = Storage(second_path)
        self.assertIsNot(first._get_conn(), second._get_conn())
        self.assertTrue(os.path.exists(second_path))

    def test_add_url_reports_duplicate_as_false(self):
        storage = Storage(self.db_path)
        record = AudioRecord(url="https://example.test/a.mp3", source="test")
        self.assertTrue(storage.add_url(record))
        self.assertFalse(storage.add_url(record))

    def test_old_database_migrates_rich_metadata_columns(self):
        import sqlite3

        connection = sqlite3.connect(self.db_path)
        connection.executescript("""
            CREATE TABLE audio_urls (
                id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT UNIQUE NOT NULL,
                source TEXT NOT NULL, title TEXT DEFAULT '', file_format TEXT DEFAULT '',
                file_size INTEGER DEFAULT 0, duration INTEGER DEFAULT 0,
                language TEXT DEFAULT '', category TEXT DEFAULT '', speaker TEXT DEFAULT '',
                status TEXT DEFAULT 'pending', local_path TEXT DEFAULT '',
                content_hash TEXT DEFAULT '', source_id TEXT DEFAULT '',
                published_at TEXT DEFAULT '', discovered_at TEXT NOT NULL,
                downloaded_at TEXT DEFAULT '', claimed_by TEXT DEFAULT '',
                claimed_at TEXT DEFAULT '', lease_expires_at TEXT DEFAULT ''
            );
            CREATE TABLE crawl_checkpoints (
                source TEXT NOT NULL, checkpoint_key TEXT NOT NULL,
                checkpoint_value TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (source, checkpoint_key)
            );
        """)
        connection.commit()
        connection.close()

        storage = Storage(self.db_path)
        columns = {
            row[1] for row in storage._get_conn().execute("PRAGMA table_info(audio_urls)")
        }
        self.assertTrue({
            "webpage_url", "description", "author", "cover_url", "metadata_json",
        }.issubset(columns))

    def test_existing_url_is_enriched_without_erasing_existing_values(self):
        storage = Storage(self.db_path)
        url = "https://example.test/a.mp3"
        storage.add_url(AudioRecord(
            url=url, source="test", title="Original", author="First",
        ))
        added, updated = storage.add_urls_batch([AudioRecord(
            url=url, source="test", description="Show notes",
            webpage_url="https://example.test/episode",
            cover_url="https://example.test/cover.jpg",
            metadata_json='{"schema_version":1}',
        )])
        row = storage._get_conn().execute(
            "SELECT * FROM audio_urls WHERE url=?", (url,),
        ).fetchone()
        self.assertEqual(added, 0)
        self.assertEqual(updated, 1)
        self.assertEqual(row["title"], "Original")
        self.assertEqual(row["author"], "First")
        self.assertEqual(row["description"], "Show notes")
        self.assertEqual(row["webpage_url"], "https://example.test/episode")

    def test_changed_signed_url_enriches_by_source_id_without_duplicate(self):
        storage = Storage(self.db_path)
        storage.add_url(AudioRecord(
            url="https://cdn.example/audio?sig=old", source="video",
            source_id="stable-1",
        ))
        added, updated = storage.add_urls_batch([AudioRecord(
            url="https://cdn.example/audio?sig=new", source="video",
            source_id="stable-1", description="New metadata",
        )])
        rows = storage._get_conn().execute(
            "SELECT url, description FROM audio_urls",
        ).fetchall()
        self.assertEqual((added, updated), (0, 1))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["description"], "New metadata")

    def test_per_source_keeps_other_filters_and_before_includes_day(self):
        storage = Storage(self.db_path)
        storage.add_urls_batch([
            AudioRecord(
                url="https://example.test/a", source="A", category="播客",
                language="zh", published_at="2024-12-31T08:00:00+08:00",
            ),
            AudioRecord(
                url="https://example.test/b", source="B", category="有声书",
                language="en", published_at="2024-12-31T08:00:00+08:00",
            ),
        ])
        rows = storage.get_pending(
            limit=1, source="A", category="播客", language="zh",
            per_source=True, published_before="2024-12-31",
        )
        self.assertEqual([row["source"] for row in rows], ["A"])

    def test_claims_are_disjoint_and_active_lease_is_not_reset(self):
        first = Storage(self.db_path)
        first.add_urls_batch([
            AudioRecord(url="https://example.test/a", source="test"),
            AudioRecord(url="https://example.test/b", source="test"),
        ])
        claimed_a = first.claim_pending(1, "worker-a", 3600)
        self.assertEqual(len(claimed_a), 1)

        second = Storage(self.db_path)
        claimed_b = second.claim_pending(1, "worker-b", 3600)
        self.assertEqual(len(claimed_b), 1)
        self.assertNotEqual(claimed_a[0]["id"], claimed_b[0]["id"])

        statuses = first._get_conn().execute(
            "SELECT status, claimed_by FROM audio_urls ORDER BY id"
        ).fetchall()
        self.assertEqual(
            [(row["status"], row["claimed_by"]) for row in statuses],
            [("downloading", "worker-a"), ("downloading", "worker-b")],
        )

    def test_finalize_download_deduplicates_transactionally(self):
        storage = Storage(self.db_path)
        storage.add_urls_batch([
            AudioRecord(url="https://example.test/a", source="test"),
            AudioRecord(url="https://example.test/b", source="test"),
        ])
        self.assertFalse(storage.finalize_download(
            "https://example.test/a", "/tmp/a", "opus", 10, "same-hash",
        ))
        self.assertTrue(storage.finalize_download(
            "https://example.test/b", "/tmp/b", "opus", 10, "same-hash",
        ))
        row = storage._get_conn().execute(
            "SELECT local_path FROM audio_urls WHERE url=?",
            ("https://example.test/b",),
        ).fetchone()
        self.assertEqual(row["local_path"], "dup:same-hash")


if __name__ == "__main__":
    unittest.main()
