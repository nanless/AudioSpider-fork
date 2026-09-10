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
                claimed_at TEXT DEFAULT '', lease_expires_at TEXT DEFAULT '',
                legacy_note TEXT DEFAULT ''
            );
            CREATE TABLE crawl_checkpoints (
                source TEXT NOT NULL, checkpoint_key TEXT NOT NULL,
                checkpoint_value TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (source, checkpoint_key)
            );
        """)
        connection.execute(
            "INSERT INTO audio_urls "
            "(url, source, source_id, discovered_at, legacy_note) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "https://cdn.example/audio", "bilibili", "BV1example_p1",
                "2026-01-01", "must survive rebuild",
            ),
        )
        connection.commit()
        connection.close()

        storage = Storage(self.db_path)
        columns = {
            row[1] for row in storage._get_conn().execute("PRAGMA table_info(audio_urls)")
        }
        self.assertTrue({
            "webpage_url", "description", "author", "cover_url", "metadata_json",
            "artifact_kind", "bundle_path", "job_key", "legacy_note",
        }.issubset(columns))
        row = storage._get_conn().execute(
            "SELECT url, source_id, discovered_at, artifact_kind, bundle_path, job_key, "
            "legacy_note "
            "FROM audio_urls"
        ).fetchone()
        self.assertEqual(
            tuple(row),
            (
                "https://cdn.example/audio", "BV1example_p1", "2026-01-01",
                "audio", "", "", "must survive rebuild",
            ),
        )
        table_sql = storage._get_conn().execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='audio_urls'"
        ).fetchone()["sql"]
        self.assertNotIn("url TEXT UNIQUE", table_sql)
        indexes = {
            index["name"]: (index["unique"], index["partial"])
            for index in storage._get_conn().execute("PRAGMA index_list(audio_urls)")
        }
        self.assertEqual(indexes["idx_unique_legacy_url"], (1, 1))
        self.assertEqual(indexes["idx_unique_artifact_job"], (1, 1))

        added, updated = storage.add_urls_batch([
            AudioRecord(
                url="https://cdn.example/audio", source="bilibili",
                source_id="BV1example_p1", artifact_kind="video_bundle",
                job_key="height=1080",
            ),
            AudioRecord(
                url="https://cdn.example/audio", source="bilibili",
                source_id="BV1example_p1", artifact_kind="video_bundle",
                job_key="height=720",
            ),
        ])
        self.assertEqual((added, updated), (2, 0))
        self.assertEqual(
            storage._get_conn().execute("SELECT COUNT(*) FROM audio_urls").fetchone()[0],
            3,
        )
        reopened = Storage(self.db_path)
        self.assertEqual(
            reopened._get_conn().execute("SELECT COUNT(*) FROM audio_urls").fetchone()[0],
            3,
        )

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

    def test_same_source_id_can_coexist_across_artifact_kinds(self):
        storage = Storage(self.db_path)
        added, updated = storage.add_urls_batch([
            AudioRecord(
                url="https://cdn.example/audio?sig=temporary",
                source="bilibili", source_id="BV1example_p1",
            ),
            AudioRecord(
                url="https://www.bilibili.com/video/BV1example?p=1",
                source="bilibili", source_id="BV1example_p1",
                artifact_kind="video_bundle",
            ),
        ])
        rows = storage._get_conn().execute(
            "SELECT artifact_kind FROM audio_urls ORDER BY artifact_kind"
        ).fetchall()
        self.assertEqual((added, updated), (2, 0))
        self.assertEqual([row["artifact_kind"] for row in rows], ["audio", "video_bundle"])
        self.assertTrue(storage.source_id_exists("bilibili", "BV1example_p1"))
        self.assertTrue(storage.source_id_exists(
            "bilibili", "BV1example_p1", artifact_kind="video_bundle",
        ))
        self.assertTrue(storage.source_id_prefix_exists(
            "bilibili", "BV1example_p", artifact_kind="video_bundle",
        ))
        self.assertFalse(storage.source_id_prefix_exists(
            "bilibili", "BV1other_p", artifact_kind="video_bundle",
        ))

    def test_enrichment_is_scoped_to_artifact_kind(self):
        storage = Storage(self.db_path)
        storage.add_urls_batch([
            AudioRecord(
                url="https://cdn.example/audio?sig=old", source="bilibili",
                source_id="BV1example_p1", title="Audio title",
            ),
            AudioRecord(
                url="https://www.bilibili.com/video/BV1example?p=1", source="bilibili",
                source_id="BV1example_p1", artifact_kind="video_bundle",
                title="Bundle title",
            ),
        ])
        added, updated = storage.add_urls_batch([AudioRecord(
            url="https://www.bilibili.com/video/BV1example?p=1&revision=2",
            source="bilibili", source_id="BV1example_p1",
            artifact_kind="video_bundle", description="Bundle metadata",
        )])
        rows = storage._get_conn().execute(
            "SELECT artifact_kind, description FROM audio_urls ORDER BY artifact_kind"
        ).fetchall()
        self.assertEqual((added, updated), (0, 1))
        self.assertEqual(
            [(row["artifact_kind"], row["description"]) for row in rows],
            [("audio", ""), ("video_bundle", "Bundle metadata")],
        )

    def test_same_url_video_jobs_coexist_and_updates_target_job_key(self):
        storage = Storage(self.db_path)
        url = "https://www.bilibili.com/video/BV1example?p=1"
        added, updated = storage.add_urls_batch([
            AudioRecord(
                url=url, source="bilibili", source_id="BV1example_p1",
                artifact_kind="video_bundle", job_key="height=1080",
                title="1080p job",
            ),
            AudioRecord(
                url=url, source="bilibili", source_id="BV1example_p1",
                artifact_kind="video_bundle", job_key="height=720",
                title="720p job",
            ),
        ])
        self.assertEqual((added, updated), (2, 0))

        storage.finalize_artifact(
            url, "/bundles/1080/source.mp4", "/bundles/1080", "mp4", 100,
            "hash-1080", artifact_kind="video_bundle", job_key="height=1080",
        )
        storage.update_status(url, "failed", job_key="height=720")
        rows = storage._get_conn().execute(
            "SELECT job_key, status, local_path, bundle_path FROM audio_urls "
            "ORDER BY job_key"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                ("height=1080", "done", "/bundles/1080/source.mp4", "/bundles/1080"),
                ("height=720", "failed", "", ""),
            ],
        )

    def test_new_job_does_not_enrich_done_job_with_same_url(self):
        storage = Storage(self.db_path)
        url = "https://www.youtube.com/watch?v=example"
        storage.add_url(AudioRecord(
            url=url, source="youtube", source_id="example",
            artifact_kind="video_bundle", job_key="captions=en",
            title="English job",
        ))
        storage.finalize_artifact(
            url, "/bundles/en/source.mp4", "/bundles/en", "mp4", 100,
            "hash-en", artifact_kind="video_bundle", job_key="captions=en",
        )

        added, updated = storage.add_urls_batch([AudioRecord(
            url=url, source="youtube", source_id="example",
            artifact_kind="video_bundle", job_key="captions=zh",
            title="Chinese job", bundle_path="/planned/zh",
        )])

        self.assertEqual((added, updated), (1, 0))
        rows = storage._get_conn().execute(
            "SELECT job_key, status, title, local_path, bundle_path FROM audio_urls "
            "ORDER BY job_key"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                (
                    "captions=en", "done", "English job",
                    "/bundles/en/source.mp4", "/bundles/en",
                ),
                ("captions=zh", "pending", "Chinese job", "", "/planned/zh"),
            ],
        )

    def test_job_key_deduplicates_across_url_changes(self):
        storage = Storage(self.db_path)
        storage.add_url(AudioRecord(
            url="https://www.youtube.com/watch?v=example&token=old",
            source="youtube", artifact_kind="video_bundle",
            job_key="captions=en", title="Original",
        ))

        added, updated = storage.add_urls_batch([AudioRecord(
            url="https://www.youtube.com/watch?v=example&token=new",
            source="youtube", artifact_kind="video_bundle",
            job_key="captions=en", description="Refreshed metadata",
        )])

        row = storage._get_conn().execute(
            "SELECT url, title, description FROM audio_urls"
        ).fetchone()
        self.assertEqual((added, updated), (0, 1))
        self.assertEqual(
            tuple(row),
            (
                "https://www.youtube.com/watch?v=example&token=old",
                "Original", "Refreshed metadata",
            ),
        )

    def test_batch_deduplicates_repeated_composite_identity(self):
        storage = Storage(self.db_path)
        added, updated = storage.add_urls_batch([
            AudioRecord(
                url="https://example.test/bundle?revision=1", source="bilibili",
                source_id="BV1example_p1", artifact_kind="video_bundle",
            ),
            AudioRecord(
                url="https://example.test/bundle?revision=2", source="bilibili",
                source_id="BV1example_p1", artifact_kind="video_bundle",
                description="Latest metadata",
            ),
        ])
        rows = storage._get_conn().execute(
            "SELECT artifact_kind, description FROM audio_urls"
        ).fetchall()
        self.assertEqual((added, updated), (1, 1))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["description"], "Latest metadata")

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

    def test_expired_lease_recovery_is_scoped_to_current_claim(self):
        storage = Storage(self.db_path)
        storage.add_urls_batch([
            AudioRecord(
                url="https://example.test/youtube", source="youtube",
                category="访谈", language="en", artifact_kind="video_bundle",
                job_key="youtube-job",
            ),
            AudioRecord(
                url="https://example.test/bilibili", source="bilibili",
                category="访谈", language="zh", artifact_kind="video_bundle",
                job_key="bilibili-job",
            ),
        ])
        youtube = storage.claim_pending(
            1, "youtube-worker", 3600, source="youtube",
            artifact_kind="video_bundle",
        )[0]
        bilibili = storage.claim_pending(
            1, "bilibili-worker", 3600, source="bilibili",
            artifact_kind="video_bundle",
        )[0]
        storage._get_conn().execute(
            "UPDATE audio_urls SET lease_expires_at=? WHERE id IN (?,?)",
            ("2000-01-01T00:00:00+00:00", youtube["id"], bilibili["id"]),
        )
        storage._get_conn().commit()

        reclaimed = storage.claim_pending(
            1, "new-bilibili-worker", 3600, source="bilibili",
            category="访谈", language="zh", artifact_kind="video_bundle",
        )

        self.assertEqual([row["id"] for row in reclaimed], [bilibili["id"]])
        rows = storage._get_conn().execute(
            "SELECT id, status, claimed_by FROM audio_urls ORDER BY id"
        ).fetchall()
        self.assertEqual(
            [(row["id"], row["status"], row["claimed_by"]) for row in rows],
            [
                (youtube["id"], "downloading", "youtube-worker"),
                (bilibili["id"], "downloading", "new-bilibili-worker"),
            ],
        )

    def test_failed_claim_honors_all_download_scope_filters(self):
        storage = Storage(self.db_path)
        records = [
            AudioRecord(
                url="https://example.test/target", source="youtube",
                category="访谈", language="en", published_at="2026-09-01",
                artifact_kind="video_bundle", job_key="target",
            ),
            AudioRecord(
                url="https://example.test/wrong-category", source="youtube",
                category="影视", language="en", published_at="2026-09-01",
                artifact_kind="video_bundle", job_key="wrong-category",
            ),
            AudioRecord(
                url="https://example.test/wrong-language", source="youtube",
                category="访谈", language="zh", published_at="2026-09-01",
                artifact_kind="video_bundle", job_key="wrong-language",
            ),
            AudioRecord(
                url="https://example.test/wrong-date", source="youtube",
                category="访谈", language="en", published_at="2025-01-01",
                artifact_kind="video_bundle", job_key="wrong-date",
            ),
            AudioRecord(
                url="https://example.test/wrong-kind", source="youtube",
                category="访谈", language="en", published_at="2026-09-01",
            ),
        ]
        storage.add_urls_batch(records)
        for record in records:
            storage.update_status(
                record.url, "failed", job_key=record.job_key,
            )

        claimed = storage.claim_failed(
            limit=10, worker_id="retry-worker", lease_seconds=60,
            source="youtube", category="访谈", language="en",
            published_since="2026-01-01", published_before="2026-12-31",
            artifact_kind="video_bundle",
        )

        self.assertEqual([row["job_key"] for row in claimed], ["target"])
        untouched = storage._get_conn().execute(
            "SELECT COUNT(*) FROM audio_urls WHERE status='failed'"
        ).fetchone()[0]
        self.assertEqual(untouched, 4)

    def test_renew_claims_only_updates_active_rows_owned_by_worker(self):
        storage = Storage(self.db_path)
        storage.add_urls_batch([
            AudioRecord(url="https://example.test/a", source="test"),
            AudioRecord(url="https://example.test/b", source="test"),
            AudioRecord(url="https://example.test/c", source="test"),
        ])
        claimed = storage.claim_pending(2, "worker-a", 3600)
        other = storage.claim_pending(1, "worker-b", 3600)[0]
        first, completed = claimed
        storage.finalize_download(
            completed["url"], "/tmp/completed", "wav", 1, "hash-completed"
        )
        storage._get_conn().execute(
            "UPDATE audio_urls SET lease_expires_at=? WHERE status='downloading'",
            ("2000-01-01T00:00:00+00:00",),
        )
        storage._get_conn().commit()

        renewed = storage.renew_claims(
            [first["id"], completed["id"], other["id"]], "worker-a", 3600
        )

        self.assertEqual(renewed, 1)
        rows = storage._get_conn().execute(
            "SELECT id, status, claimed_by, lease_expires_at FROM audio_urls ORDER BY id"
        ).fetchall()
        by_id = {row["id"]: row for row in rows}
        self.assertGreater(
            by_id[first["id"]]["lease_expires_at"],
            "2000-01-01T00:00:00+00:00",
        )
        self.assertEqual(by_id[completed["id"]]["status"], "done")
        self.assertEqual(
            by_id[other["id"]]["lease_expires_at"],
            "2000-01-01T00:00:00+00:00",
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

    def test_finalize_artifact_keeps_bundle_even_when_hash_matches(self):
        storage = Storage(self.db_path)
        storage.add_urls_batch([
            AudioRecord(
                url="https://example.test/bundle-a", source="bilibili",
                source_id="part-a", artifact_kind="video_bundle",
            ),
            AudioRecord(
                url="https://example.test/bundle-b", source="bilibili",
                source_id="part-b", artifact_kind="video_bundle",
            ),
        ])
        self.assertFalse(storage.finalize_artifact(
            "https://example.test/bundle-a", "/bundles/a/source.mp4", "/bundles/a",
            "mp4", 100, "same-hash", artifact_kind="video_bundle",
        ))
        self.assertFalse(storage.finalize_artifact(
            "https://example.test/bundle-b", "/bundles/b/source.mp4", "/bundles/b",
            "mp4", 100, "same-hash", artifact_kind="video_bundle",
        ))
        rows = storage._get_conn().execute(
            "SELECT status, local_path, bundle_path FROM audio_urls ORDER BY id"
        ).fetchall()
        self.assertEqual(
            [(row["status"], row["local_path"], row["bundle_path"]) for row in rows],
            [
                ("done", "/bundles/a/source.mp4", "/bundles/a"),
                ("done", "/bundles/b/source.mp4", "/bundles/b"),
            ],
        )

    def test_bundle_hash_never_causes_audio_duplicate_signal(self):
        storage = Storage(self.db_path)
        storage.add_urls_batch([
            AudioRecord(
                url="https://example.test/bundle", source="bilibili",
                source_id="part", artifact_kind="video_bundle",
            ),
            AudioRecord(
                url="https://example.test/audio", source="bilibili",
                source_id="part", artifact_kind="audio",
            ),
        ])
        storage.finalize_artifact(
            "https://example.test/bundle", "/bundle/source.mp4", "/bundle",
            "mp4", 10, "shared-hash",
        )
        self.assertFalse(storage.finalize_download(
            "https://example.test/audio", "/audio.opus", "opus", 10, "shared-hash",
        ))
        row = storage._get_conn().execute(
            "SELECT local_path FROM audio_urls WHERE artifact_kind='audio'"
        ).fetchone()
        self.assertEqual(row["local_path"], "/audio.opus")

    def test_finalize_artifact_rejects_mismatched_artifact_kind(self):
        storage = Storage(self.db_path)
        storage.add_url(AudioRecord(
            url="https://example.test/bundle", source="bilibili",
            source_id="part", artifact_kind="video_bundle",
        ))

        with self.assertRaises(ValueError):
            storage.finalize_artifact(
                "https://example.test/bundle", "/bundle/source.mp4", "/bundle",
                "mp4", 10, "bundle-hash", artifact_kind="audio",
            )

        row = storage._get_conn().execute(
            "SELECT status, local_path, bundle_path FROM audio_urls WHERE url=?",
            ("https://example.test/bundle",),
        ).fetchone()
        self.assertEqual(
            (row["status"], row["local_path"], row["bundle_path"]),
            ("pending", "", ""),
        )

    def test_artifact_filters_and_stats_include_artifact_kind(self):
        storage = Storage(self.db_path)
        storage.add_urls_batch([
            AudioRecord(url="https://example.test/audio", source="same", source_id="one"),
            AudioRecord(
                url="https://example.test/bundle", source="same", source_id="one",
                artifact_kind="video_bundle", bundle_path="/bundles/one",
            ),
        ])
        all_by_default = storage.get_pending(limit=10)
        self.assertEqual(
            {row["artifact_kind"] for row in all_by_default},
            {"audio", "video_bundle"},
        )
        audio = storage.get_pending(limit=10, artifact_kind="audio")
        self.assertEqual([row["artifact_kind"] for row in audio], ["audio"])
        bundles = storage.get_pending(limit=10, artifact_kind="video_bundle")
        self.assertEqual(len(bundles), 1)
        self.assertEqual(bundles[0]["bundle_path"], "/bundles/one")
        stats = storage.get_stats()
        self.assertEqual(stats["by_artifact_kind"]["audio"]["pending"], 1)
        self.assertEqual(stats["by_artifact_kind"]["video_bundle"]["pending"], 1)

        claimed = storage.claim_pending(
            limit=10, worker_id="worker", lease_seconds=60,
        )
        self.assertEqual(
            {row["artifact_kind"] for row in claimed},
            {"audio", "video_bundle"},
        )


if __name__ == "__main__":
    unittest.main()
