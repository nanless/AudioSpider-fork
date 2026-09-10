import hashlib
import math
import os
import struct
import tempfile
import unittest
import wave

from convert_audio import convert_file, plan_targets, scan_audio_files
from storage import AudioRecord, Storage


def make_wav(path: str):
    rate = 24000
    with wave.open(path, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        for index in range(rate // 10):
            sample = int(8000 * math.sin(2 * math.pi * 440 * index / rate))
            output.writeframesraw(struct.pack("<h", sample))


class ConvertAudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def test_same_stem_inputs_get_distinct_targets(self):
        mp3 = os.path.join(self.temp.name, "same.mp3")
        wav = os.path.join(self.temp.name, "same.wav")
        targets = plan_targets([mp3, wav])
        self.assertNotEqual(targets[mp3], targets[wav])
        self.assertTrue(targets[mp3].endswith("_mp3.opus"))
        self.assertTrue(targets[wav].endswith("_wav.opus"))

    def test_sidecar_dataset_directory_is_never_bulk_converted(self):
        protected = os.path.join(self.temp.name, "dataset")
        os.makedirs(protected)
        with open(os.path.join(protected, ".audiospider-dataset.json"), "w") as output:
            output.write("{}")
        make_wav(os.path.join(protected, "audio.wav"))
        ordinary = os.path.join(self.temp.name, "ordinary.wav")
        make_wav(ordinary)
        self.assertEqual(scan_audio_files(self.temp.name), [ordinary])
        self.assertEqual(scan_audio_files(protected), [])

    def test_conversion_updates_database_to_final_file(self):
        source_path = os.path.join(self.temp.name, "tone.wav")
        make_wav(source_path)
        storage = Storage(os.path.join(self.temp.name, "audio.db"))
        record = AudioRecord(
            url="https://example.test/tone.wav", source="test",
            file_format="wav", local_path=source_path, status="done",
        )
        storage.add_url(record)
        storage.update_status(record.url, "done", source_path)

        result = convert_file(source_path, storage=storage)
        self.assertEqual(result["status"], "converted")
        final_path = os.path.splitext(source_path)[0] + ".opus"
        row = storage._get_conn().execute(
            "SELECT * FROM audio_urls WHERE url=?", (record.url,),
        ).fetchone()
        self.assertEqual(row["local_path"], final_path)
        self.assertEqual(row["file_format"], "opus")
        self.assertFalse(os.path.exists(source_path))
        self.assertTrue(os.path.isfile(final_path))
        with open(final_path, "rb") as source:
            digest = hashlib.sha256(source.read()).hexdigest()
        self.assertEqual(row["content_hash"], digest)


if __name__ == "__main__":
    unittest.main()
