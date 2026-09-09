import unittest

from youtube_vtt import Cue, format_timestamp, group_cues, parse_timestamp, parse_vtt, render_vtt


class YoutubeVttTests(unittest.TestCase):
    def test_timestamp_round_trip(self):
        value = parse_timestamp("01:02:03.456")
        self.assertAlmostEqual(value, 3723.456)
        self.assertEqual(format_timestamp(value), "01:02:03.456")
        with self.assertRaises(ValueError):
            parse_timestamp("00:60.000")

    def test_parse_cleans_markup_and_deduplicates_rolling_caption(self):
        cues = parse_vtt(
            "WEBVTT\n\n"
            "00:00:01.000 --> 00:00:04.000 align:start\n"
            "<c.colorCCCCCC>Hello &amp; welcome</c>\n\n"
            "00:00:04.000 --> 00:00:07.000\n"
            "Hello &amp; welcome\nnew line\n\n",
            deduplicate_rolling=True,
        )
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].text, "Hello & welcome")
        self.assertEqual(cues[1].text, "new line")

    def test_manual_caption_keeps_legitimate_repeated_dialogue(self):
        cues = parse_vtt(
            "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nNo\n\n"
            "00:00:01.000 --> 00:00:02.000\nNo\n"
        )
        self.assertEqual([cue.text for cue in cues], ["No", "No"])

    def test_automatic_caption_removes_only_suffix_prefix_overlap(self):
        cues = parse_vtt(
            "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\none\ntwo\n\n"
            "00:00:01.000 --> 00:00:02.000\ntwo\nthree\n",
            deduplicate_rolling=True,
        )
        self.assertEqual([cue.text for cue in cues], ["one\ntwo", "three"])

    def test_groups_to_target_without_exceeding_bounds(self):
        cues = [Cue(i * 3.0, i * 3.0 + 2.8, f"line {i}") for i in range(8)]
        groups = group_cues(
            cues,
            min_duration=0.418,
            max_duration=29.888,
            target_duration=11.5,
        )
        self.assertGreaterEqual(len(groups), 2)
        self.assertTrue(all(0.418 <= group.duration <= 29.888 for group in groups))
        self.assertEqual(groups[0].text.splitlines()[0], "line 0")

    def test_large_gap_starts_new_group(self):
        groups = group_cues(
            [Cue(0.0, 3.0, "one"), Cue(20.0, 23.0, "two")],
            min_duration=0.418,
            max_duration=29.888,
            target_duration=11.5,
            max_gap=2.0,
        )
        self.assertEqual(len(groups), 2)

    def test_rendered_clip_vtt_uses_relative_timestamps(self):
        output = render_vtt([Cue(10.0, 12.0, "hello"), Cue(12.5, 14.0, "world")], origin=10.0)
        self.assertIn("00:00:00.000 --> 00:00:02.000", output)
        self.assertIn("00:00:02.500 --> 00:00:04.000", output)


if __name__ == "__main__":
    unittest.main()
