import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from background import decode_metadata
from youtube_dataset import CaptionSelection
from spiders.youtube import YoutubeSpider


class YoutubeSpiderTests(unittest.IsolatedAsyncioTestCase):
    async def test_manifest_job_enters_shared_queue_as_complete_video_bundle(self):
        document = {
            "schema_version": 1,
            "items": [{
                "url": "https://www.youtube.com/watch?v=u7TwqpWiY5s",
                "profile": "youtube_interviews",
                "content_language": "en",
                "languages": ["en"],
                "speaker_count": None,
                "speaker_count_status": "needs_review",
                "program": "Interview",
                "rights": {"status": "needs_review"},
            }],
        }
        with tempfile.TemporaryDirectory() as temp:
            manifest = Path(temp) / "youtube.json"
            manifest.write_text(json.dumps(document), encoding="utf-8")
            spider = YoutubeSpider()
            spider.manifest_path = manifest
            info = {
                "id": "u7TwqpWiY5s", "title": "Long interview",
                "description": "Background", "duration": 1800,
                "channel": "Example Channel", "upload_date": "20260102",
            }
            caption = CaptionSelection(
                language="en", kind="manual", text_source="platform_manual",
                ext="vtt", selected_by_rule="exact_requested_language",
            )
            with mock.patch("spiders.youtube._bounded_inspect_item", return_value=(info, caption)):
                records = await spider.crawl()

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.url, "https://www.youtube.com/watch?v=u7TwqpWiY5s")
        self.assertEqual(record.artifact_kind, "video_bundle")
        self.assertEqual(record.file_format, "mp4")
        self.assertEqual(record.category, "访谈")
        metadata = decode_metadata(record.metadata_json)
        job = metadata["source_data"]["youtube"]["job"]
        self.assertEqual(job["video_id"], record.source_id)
        self.assertEqual(job["content_language"], "en")
        self.assertEqual(
            metadata["source_data"]["youtube"]["inspection"]["caption"]["kind"],
            "manual",
        )

    async def test_rejected_inspection_is_not_queued(self):
        document = {
            "schema_version": 1,
            "items": [{
                "url": "https://www.youtube.com/watch?v=u7TwqpWiY5s",
                "profile": "youtube_interviews",
                "content_language": "en",
                "languages": ["en"],
                "rights": {"status": "needs_review"},
            }],
        }
        with tempfile.TemporaryDirectory() as temp:
            manifest = Path(temp) / "youtube.json"
            manifest.write_text(json.dumps(document), encoding="utf-8")
            spider = YoutubeSpider()
            spider.manifest_path = manifest
            with mock.patch(
                "spiders.youtube._bounded_inspect_item", side_effect=ValueError("no caption")
            ):
                records = await spider.crawl()
        self.assertEqual(records, [])

    async def test_best_effort_manifest_queues_parent_without_caption(self):
        document = {
            "schema_version": 1,
            "items": [{
                "url": "https://www.youtube.com/watch?v=u7TwqpWiY5s",
                "profile": "youtube_interviews",
                "content_language": "en",
                "languages": ["en"],
                "require_caption": False,
                "rights": {"status": "needs_review"},
            }],
        }
        with tempfile.TemporaryDirectory() as temp:
            manifest = Path(temp) / "youtube.json"
            manifest.write_text(json.dumps(document), encoding="utf-8")
            spider = YoutubeSpider()
            spider.manifest_path = manifest
            info = {
                "id": "u7TwqpWiY5s", "title": "Captionless interview",
                "description": "Background", "duration": 1800,
                "channel": "Example Channel", "upload_date": "20260102",
                "caption_status": "missing",
            }
            with mock.patch(
                "spiders.youtube._bounded_inspect_item", return_value=(info, None)
            ):
                records = await spider.crawl()

        self.assertEqual(len(records), 1)
        metadata = decode_metadata(records[0].metadata_json)
        source = metadata["source_data"]["youtube"]
        self.assertFalse(source["job"]["require_caption"])
        self.assertEqual(source["inspection"]["caption"]["status"], "missing")
        self.assertEqual(metadata["transcript_status"], "missing")
        self.assertEqual(metadata["text_source"], "none")

    async def test_new_parent_profiles_use_semantic_category_and_keep_candidate_metadata(self):
        document = {
            "schema_version": 1,
            "items": [
                {
                    "url": "https://www.youtube.com/watch?v=u7TwqpWiY5s",
                    "profile": "youtube_screen_parents",
                    "dataset_category": "影视",
                    "content_kind": "screen_media",
                    "content_language": "en",
                    "languages": ["en"],
                    "require_caption": False,
                    "rights": {"status": "needs_review"},
                    "candidate_metadata": {"search_rank": 4, "signals": ["ensemble cast"]},
                },
                {
                    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    "profile": "youtube_conference_forums",
                    "content_language": "en",
                    "languages": ["en"],
                    "require_caption": False,
                    "rights": {"status": "needs_review"},
                },
            ],
        }
        info = {
            "title": "Complete parent", "description": "Background", "duration": 3600,
            "channel": "Example Channel", "upload_date": "20260102",
            "caption_status": "missing",
        }
        with tempfile.TemporaryDirectory() as temp:
            manifest = Path(temp) / "youtube.json"
            manifest.write_text(json.dumps(document), encoding="utf-8")
            spider = YoutubeSpider()
            spider.manifest_path = manifest
            with mock.patch(
                "spiders.youtube._bounded_inspect_item",
                side_effect=[(info | {"id": "u7TwqpWiY5s"}, None),
                             (info | {"id": "dQw4w9WgXcQ"}, None)],
            ):
                records = await spider.crawl()

        self.assertEqual([record.category for record in records], ["影视", "会议论坛"])
        self.assertTrue(all(record.artifact_kind == "video_bundle" for record in records))
        first_job = decode_metadata(records[0].metadata_json)["source_data"]["youtube"]["job"]
        self.assertEqual(first_job["candidate_metadata"]["signals"], ["ensemble cast"])
        self.assertEqual(first_job["content_kind"], "screen_media")


if __name__ == "__main__":
    unittest.main()
