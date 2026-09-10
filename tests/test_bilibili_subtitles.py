import unittest

from bilibili_subtitles import (
    classify_subtitle_inventory,
    classify_subtitle_track,
    parse_subtitle_document,
    render_subtitle_text,
    render_subtitle_vtt,
    sanitize_subtitle_track,
    subtitle_track_diagnostic,
)


class SubtitleProvenanceTests(unittest.TestCase):
    def test_type_one_is_platform_automatic(self):
        result = classify_subtitle_track({
            "type": 1,
            "lan": "ai-zh",
            "lan_doc": "中文（自动生成）",
            "ai_type": 0,
            "ai_status": 2,
        })
        self.assertEqual(result["kind"], "automatic")
        self.assertEqual(result["text_source"], "platform_auto")
        self.assertEqual(result["selected_by_rule"], "bilibili_type_ai")

    def test_type_zero_with_author_is_platform_manual(self):
        result = classify_subtitle_track({
            "type": 0,
            "lan": "zh-Hans",
            "lan_doc": "中文（简体）",
            "author": {"mid": 123, "name": "字幕君"},
        })
        self.assertEqual(result["kind"], "manual")
        self.assertEqual(result["text_source"], "platform_manual")
        self.assertEqual(result["selected_by_rule"], "bilibili_cc_with_author")

    def test_explicit_auto_label_is_a_fallback(self):
        result = classify_subtitle_track({
            "lan": "ai-en",
            "lan_doc": "English (auto-generated)",
        })
        self.assertEqual(result["kind"], "automatic")
        self.assertEqual(result["selected_by_rule"], "bilibili_auto_label_fallback")

    def test_type_zero_without_author_stays_unknown(self):
        result = classify_subtitle_track({
            "type": 0, "lan": "zh-CN", "lan_doc": "中文"
        })
        self.assertEqual(result["kind"], "unknown")
        self.assertEqual(result["text_source"], "platform_unknown")

    def test_conflicting_type_and_label_stays_unknown(self):
        result = classify_subtitle_track({
            "type": 0,
            "lan": "ai-zh",
            "lan_doc": "中文（自动生成）",
            "author": {"mid": 123, "name": "字幕君"},
        })
        self.assertEqual(result["kind"], "unknown")
        self.assertEqual(result["selected_by_rule"], "bilibili_conflicting_evidence")

    def test_ai_type_is_preserved_but_not_used_as_generation_proof(self):
        row = sanitize_subtitle_track({
            "id": 7,
            "id_str": "7",
            "lan": "en-US",
            "lan_doc": "English",
            "subtitle_url": "//aisubtitle.hdslb.com/a.json?auth_key=secret",
            "type": 0,
            "ai_type": 1,
            "ai_status": 2,
        })
        self.assertEqual(row["kind"], "unknown")
        self.assertEqual(row["ai_type"], 1)
        self.assertEqual(row["translation_kind"], "automatic_translation")
        self.assertEqual(row["url"], "https://aisubtitle.hdslb.com/a.json?auth_key=secret")
        self.assertEqual(row["url_redacted"], "https://aisubtitle.hdslb.com/a.json")

    def test_non_integer_type_is_not_treated_as_ai_enum(self):
        for value in (True, "1", 1.0):
            with self.subTest(value=value):
                result = classify_subtitle_track({"type": value, "lan": "zh-CN"})
                self.assertEqual(result["kind"], "unknown")

    def test_empty_inventory_distinguishes_auth_required_and_public_missing(self):
        auth = classify_subtitle_inventory({
            "need_login_subtitle": True, "subtitle": {"subtitles": []},
        })
        missing = classify_subtitle_inventory({
            "need_login_subtitle": False, "subtitle": {"subtitles": []},
        })
        self.assertEqual(auth["status"], "auth_required")
        self.assertEqual(missing["status"], "not_provided_publicly")
        malformed = classify_subtitle_inventory({
            "need_login_subtitle": {"token": "must-not-persist"},
            "subtitle": {"subtitles": []},
        })
        self.assertEqual(malformed["status"], "unknown")
        self.assertIsNone(malformed["need_login_subtitle"])

    def test_track_diagnostic_never_retains_signed_url_components(self):
        diagnostic = subtitle_track_diagnostic({
            "id_str": "7", "lan": "ai-zh", "type": 1,
            "subtitle_url": (
                "http://aisubtitle.hdslb.com/bfs/ai_subtitle/file.json"
                "?auth_key=secret#fragment"
            ),
        }, 0)
        self.assertEqual(diagnostic["url_form"], "http")
        self.assertEqual(diagnostic["url_host"], "aisubtitle.hdslb.com")
        self.assertEqual(diagnostic["rejection_reason"], "unsupported_scheme")
        rendered = str(diagnostic).lower()
        self.assertNotIn("auth_key", rendered)
        self.assertNotIn("/bfs/", rendered)
        self.assertNotIn("secret", rendered)

    def test_non_string_track_url_is_safely_diagnosed(self):
        diagnostic = subtitle_track_diagnostic({
            "lan": "ai-zh", "subtitle_url": {"unexpected": "shape"},
        }, 0)
        self.assertEqual(diagnostic["rejection_reason"], "url_not_string")
        self.assertEqual(diagnostic["url_value_type"], "dict")

    def test_track_identity_diagnostic_rejects_url_like_tokens(self):
        diagnostic = subtitle_track_diagnostic({
            "id_str": "https://evil.example/path?token=secret",
            "lan": "zh/Hans?token=secret",
            "subtitle_url": "//aisubtitle.hdslb.com/subtitle.json",
        }, 0)
        self.assertEqual(diagnostic["id_str"], "")
        self.assertEqual(diagnostic["language"], "")
        self.assertNotIn("token", str(diagnostic).lower())


class SubtitleDocumentTests(unittest.TestCase):
    def test_json_body_becomes_cues_vtt_and_text(self):
        cues = parse_subtitle_document({"body": [
            {"from": 0.04, "to": 1.20, "content": "你好"},
            {"from": 1.20, "to": 2.50, "content": "world\n第二行"},
        ]})
        self.assertEqual(len(cues), 2)
        self.assertIn("00:00:00.040 --> 00:00:01.200", render_subtitle_vtt(cues))
        self.assertEqual(render_subtitle_text(cues), "你好\nworld\n第二行\n")

    def test_invalid_or_empty_cues_are_rejected(self):
        for document in (
            {},
            {"body": []},
            {"body": [{"from": 2, "to": 1, "content": "bad"}]},
            {"body": [{"from": 0, "to": 1, "content": "   "}]},
            {"body": [{"from": float("nan"), "to": 1, "content": "bad"}]},
        ):
            with self.subTest(document=document):
                with self.assertRaises(ValueError):
                    parse_subtitle_document(document)

    def test_cue_limit_is_enforced(self):
        with self.assertRaises(ValueError):
            parse_subtitle_document({"body": [
                {"from": i, "to": i + 0.5, "content": str(i)} for i in range(3)
            ]}, max_cues=2)


if __name__ == "__main__":
    unittest.main()
