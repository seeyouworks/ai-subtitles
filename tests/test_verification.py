from __future__ import annotations

import hashlib
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from subtitle_app.core import Cancelled, Cue, Settings, read_json
from subtitle_app.verification import (compare_dialogue, download_sample, load_sample,
    overall_status, review_translation, subtitle_problems, translation_problems,
    word_error_rate, write_reports)


class VerificationTests(unittest.TestCase):
    def test_word_error_rate_handles_punctuation_and_real_missing_words(self):
        self.assertEqual(word_error_rate("I'm here.", "I’m HERE!"), 0)
        self.assertAlmostEqual(word_error_rate("one two three", "one three"), 1 / 3)
        self.assertAlmostEqual(word_error_rate("one two", "one two extra"), 1 / 2)

    def test_music_dialogue_omission_is_a_failure(self):
        sample = load_sample()
        rows = sample["dialogue"]
        cues = [Cue(row["id"], row["start"], row["end"], row["en"]) for row in rows[:2]]
        comparison = compare_dialogue(cues, sample)
        self.assertEqual(comparison["covered"], 2)
        self.assertEqual(comparison["expected_count"], 4)
        self.assertGreater(comparison["wer"], sample["maximum_wer"])

    def test_extra_dialogue_is_flagged_even_if_reference_is_complete(self):
        sample = load_sample()
        cues = [Cue(1, 0, 0.75, "You")]
        cues += [Cue(index + 2, row["start"], row["end"], row["en"]) for index, row in enumerate(sample["dialogue"])]
        comparison = compare_dialogue(cues, sample)
        self.assertEqual(comparison["covered"], 4)
        self.assertEqual([cue["id"] for cue in comparison["unexpected_cues"]], [1])

    def test_time_and_translation_validation(self):
        source = [Cue(1, 1, 2, "Hello")]
        self.assertFalse(subtitle_problems(source, 3))
        self.assertTrue(subtitle_problems([Cue(1, 1, 4, "Hello")], 3))
        self.assertFalse(translation_problems(source, [Cue(1, 1, 2, "안녕")]))
        self.assertTrue(translation_problems(source, [Cue(1, 1, 2, "Hello")]))
        self.assertTrue(translation_problems(source, [Cue(1, 1.1, 2, "안녕")]))

    def test_download_reuses_only_checksum_verified_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "test_media"
            media.mkdir()
            (media / "sample.mp4").write_bytes(b"valid")
            sample = {"filename": "sample.mp4", "sha256": hashlib.sha256(b"valid").hexdigest(), "size_bytes": 5}
            with patch("subtitle_app.verification.ROOT", root), patch("subtitle_app.verification.load_sample", return_value=sample), patch("urllib.request.urlopen") as network:
                result = download_sample(threading.Event(), lambda text: None)
                self.assertEqual(result.read_bytes(), b"valid")
                network.assert_not_called()

    def test_bad_download_does_not_replace_existing_file(self):
        from io import BytesIO
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "test_media"
            media.mkdir()
            (media / "sample.mp4").write_bytes(b"old")
            sample = {"filename": "sample.mp4", "url": "https://example.org/video", "title": "Sample", "license": "CC BY",
                      "sha256": hashlib.sha256(b"good").hexdigest(), "size_bytes": 4}
            with patch("subtitle_app.verification.ROOT", root), patch("subtitle_app.verification.load_sample", return_value=sample), patch("urllib.request.urlopen", return_value=BytesIO(b"bad!")):
                with self.assertRaises(ValueError):
                    download_sample(threading.Event(), lambda text: None)
            self.assertEqual((media / "sample.mp4").read_bytes(), b"old")
            self.assertFalse(list(media.glob("*.part")))

    def test_cancel_download_before_network(self):
        cancel = threading.Event()
        cancel.set()
        sample = load_sample()
        with tempfile.TemporaryDirectory() as directory, patch("subtitle_app.verification.ROOT", Path(directory)), patch("subtitle_app.verification.load_sample", return_value=sample), patch("urllib.request.urlopen") as network:
            with self.assertRaises(Cancelled):
                download_sample(cancel, lambda text: None)
            network.assert_not_called()

    def test_duplicate_semantic_review_rejected(self):
        rows = [{"id": 1, "en": "Hi", "ko": "안녕", "cue_ids": [1]}, {"id": 2, "en": "Bye", "ko": "잘가", "cue_ids": [2]}]
        payload = {"reviews": [{"id": 1, "faithful": True, "reason": ""}, {"id": 1, "faithful": True, "reason": ""}]}
        with patch("subtitle_app.verification.request_codex_json", return_value=payload):
            with self.assertRaises(ValueError):
                review_translation(rows, [], Settings(), threading.Event(), lambda text: None)

    def test_warning_never_reported_as_full_pass(self):
        self.assertEqual(overall_status([{"status": "pass"}, {"status": "warning"}]), "warning")
        self.assertEqual(overall_status([{"status": "warning"}, {"status": "fail"}]), "fail")

    def test_html_escapes_untrusted_subtitle_text(self):
        sample = load_sample()
        with tempfile.TemporaryDirectory() as directory, patch("subtitle_app.verification.ROOT", Path(directory)):
            root = Path(directory)
            report = {"sample": sample, "created_at": "test", "checks": [{"name": "sample", "status": "warning", "detail": "Review"}], "original": None, "translated": None}
            output = write_reports(root / "result", report, [Cue(1, 1, 2, '<script>alert("x")</script>')], [])
            content = output.read_text(encoding="utf-8")
            self.assertNotIn('<script>alert("x")</script>', content)
            self.assertIn("&lt;script&gt;", content)
            embedded = re.search(r'<script id="subtitle-data" type="application/json">(.*?)</script>', content, re.S)
            self.assertIsNotNone(embedded)
            self.assertEqual(json.loads(embedded.group(1))[0]["cues"][0]["text"], '<script>alert("x")</script>')
            self.assertEqual(read_json(root / "result" / "report.json", {})["status"], "warning")
            report["settings"] = {"translate": False, "codex_model": "gpt-6.1-sol"}
            output = write_reports(root / "result", report, [], [])
            self.assertIn("Codex: 번역 생략", output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
