import json
import os
import tempfile
import unittest

from background import encode_metadata, metadata_envelope
from metadata_backfill import (
    MetadataBackfiller,
    archive_identifiers,
    audit_records,
    bilibili_target,
    xiaoyuzhou_podcast_paths,
)
from storage import AudioRecord, Storage


class MetadataAuditTests(unittest.TestCase):
    def test_audit_distinguishes_rich_partial_and_unresolved(self):
        rows = [
            {
                "source": "podcast_rss", "source_id": "one",
                "description": "detail", "author": "host",
                "cover_url": "https://example.test/cover.jpg",
                "webpage_url": "https://example.test/one",
                "metadata_json": encode_metadata(metadata_envelope(
                    "rss", source_data={"feed_url": "https://example.test/feed"},
                    assets={"transcripts": [{"url": "https://example.test/t.vtt"}]},
                    text_source="rss",
                )),
            },
            {
                "source": "ximalaya", "source_id": "two",
                "description": "", "author": "", "cover_url": "",
                "webpage_url": "https://example.test/two",
                "metadata_json": encode_metadata(metadata_envelope(
                    "ximalaya", source_data={"track_id": "two"},
                )),
            },
            {
                "source": "bilibili", "source_id": "three",
                "description": "", "author": "", "cover_url": "",
                "webpage_url": "", "metadata_json": "",
            },
        ]
        audit = audit_records(rows)
        self.assertEqual(audit["total"], 3)
        self.assertEqual(audit["metadata"], 2)
        self.assertEqual(audit["rich"], 1)
        self.assertEqual(audit["partial"], 1)
        self.assertEqual(audit["unresolved"], 1)
        self.assertEqual(audit["transcript_status"]["provided"], 1)
        self.assertEqual(audit["transcript_status"]["not_provided"], 1)
        self.assertEqual(audit["sources"]["bilibili"]["unresolved"], 1)
        self.assertEqual(audit["unresolved_records"][0]["source_id"], "three")

    def test_source_helpers_use_stable_ids(self):
        self.assertEqual(bilibili_target("BV1abc_p12"), ("BV1abc", 12))
        self.assertIsNone(bilibili_target("not-a-part"))
        rows = [
            {"url": "https://media.xyzcdn.net/abc123/episode.m4a"},
            {"url": "https://other.example/abc123/episode.m4a"},
        ]
        self.assertEqual(xiaoyuzhou_podcast_paths(rows), ["/podcast/abc123"])
        self.assertEqual(
            archive_identifiers([{
                "url": "https://archive.org/download/book-id/chapter01.mp3",
            }]),
            {"book-id"},
        )


class MetadataBackfillerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(os.path.join(self.temp.name, "test.db"))
        self.storage.add_urls_batch([
            AudioRecord(url="https://old.example/one.mp3", source="one", source_id="id-1"),
            AudioRecord(url="https://old.example/two.mp3", source="two", source_id="id-2"),
        ])

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_source_failure_does_not_block_other_source(self):
        async def works(_rows):
            return [AudioRecord(
                url="https://new.example/one.mp3", source="one", source_id="id-1",
                description="filled", author="author", webpage_url="https://example.test/one",
                metadata_json=json.dumps({"schema_version": 1, "source_data": {"one": {"id": 1}}}),
            )]

        async def fails(_rows):
            raise RuntimeError("source unavailable")

        report = await MetadataBackfiller(
            self.storage, handlers={"one": works, "two": fails},
        ).run()
        rows = self.storage.get_metadata_records()
        self.assertEqual(rows[0]["description"], "filled")
        self.assertEqual(report["sources"]["one"]["matched"], 1)
        self.assertIn("source unavailable", report["sources"]["two"]["error"])
        self.assertEqual(report["after"]["unresolved"], 1)


if __name__ == "__main__":
    unittest.main()
