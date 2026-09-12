import json
from pathlib import Path
import unittest

from scripts.audit_multispeaker_batch import load_and_validate_batch


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


if __name__ == "__main__":
    unittest.main()
