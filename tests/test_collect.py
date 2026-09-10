import unittest
from unittest.mock import patch

import collect
from storage import AudioRecord


def _part(bvid: str, page: int) -> AudioRecord:
    return AudioRecord(
        url=f"https://www.bilibili.com/video/{bvid}?p={page}",
        source="bilibili",
        source_id=f"{bvid}_p{page}",
        title=f"{bvid} P{page}",
        artifact_kind="video_bundle",
    )


class _BilibiliBatchSpider:
    name = "bilibili"
    max_new_records = 1

    async def crawl(self, on_batch=None):
        on_batch([_part("BVexisting", 1), _part("BVexisting", 2)])
        on_batch([_part("BVnew", 1), _part("BVnew", 2), _part("BVnew", 3)])
        on_batch([_part("BVnew", 1), _part("BVnew", 2), _part("BVnew", 3)])
        return []


class _Storage:
    def __init__(self):
        self.inserted = []

    def source_id_prefix_exists(self, source, source_id_prefix, artifact_kind="audio"):
        return source_id_prefix == "BVexisting_p"

    def add_urls_batch(self, records):
        self.inserted.extend(records)
        return len(records), 0

    def show_stats(self):
        pass


class CollectBilibiliQuotaTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_parent_is_skipped_and_new_parent_keeps_all_parts(self):
        storage = _Storage()
        with (
            patch.object(collect, "ALL_SPIDERS", [_BilibiliBatchSpider]),
            patch.dict(collect.SPIDER_CONFIGS, {"bilibili": {"enabled": True}}, clear=True),
        ):
            added = await collect.do_crawl(storage, ["bilibili"])

        self.assertEqual(added, 3)
        self.assertEqual(
            [record.source_id for record in storage.inserted],
            ["BVnew_p1", "BVnew_p2", "BVnew_p3"],
        )


if __name__ == "__main__":
    unittest.main()
