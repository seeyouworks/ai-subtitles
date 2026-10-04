from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from subtitle_app.core import (Cancelled, CodexTranslator, Cue, Pipeline, Settings, atomic_json,
    atomic_text, collect_inputs, parse_srt, pick_subtitles, read_json, read_srt, segment_cues,
    output_conflicts, timestamp, to_srt, validate_translation, wrap_subtitle)


class SubtitleTests(unittest.TestCase):
    def test_roundtrip_multilingual_and_timestamp_carry(self):
        cues = [Cue(1, 59.9996, 63.1, "你好。こんにちは。\n안녕하세요."), Cue(2, 3600, 3602, "English / Español / Русский")]
        result = parse_srt(to_srt(cues))
        self.assertEqual(result[0].start, 60)
        self.assertEqual(result[0].text, cues[0].text)
        self.assertEqual(timestamp(3599.9996), "01:00:00,000")
        self.assertEqual(result[1], cues[1])

    def test_malformed_srt_not_silently_discarded(self):
        with self.assertRaises(ValueError):
            parse_srt("1\n00:00:03,000 --> 00:00:01,000\nHi")
        with self.assertRaises(ValueError):
            parse_srt("1\nmalformed\nHi")

    def test_output_protects_existing_and_allows_identical_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "한글 이름.srt"
            atomic_text(output, "원문\n", protect=True)
            atomic_text(output, "원문\n", protect=True)
            with self.assertRaises(FileExistsError):
                atomic_text(output, "바뀐 내용\n", protect=True)
            self.assertEqual(output.read_text(encoding="utf-8-sig"), "원문\n")

    def test_duplicate_missing_unknown_and_empty_translation_rejected(self):
        batch = [Cue(1, 0, 1, "Hello"), Cue(2, 1, 2, "World")]
        first = {"id": 1, "text": "안녕", "status": "translated", "reason": ""}
        second = {"id": 2, "text": "세계", "status": "translated", "reason": ""}
        self.assertEqual(set(validate_translation({"translations": [second, first]}, batch)), {1, 2})
        for rows in ([first], [first, first], [first, dict(second, id=3)], [first, dict(second, text="")]):
            with self.assertRaises(ValueError):
                validate_translation({"translations": rows}, batch)

    def test_refusal_keeps_original_and_resume_reuses_success(self):
        cues = [Cue(1, 0, 2, "Hello"), Cue(2, 2, 4, "Original dialogue")]
        settings = Settings(batch_size=2)
        with tempfile.TemporaryDirectory() as directory:
            translator = CodexTranslator(settings, threading.Event(), lambda text: None)
            with patch.object(translator, "_request", return_value={1: {"id": 1, "text": "안녕", "status": "translated", "reason": ""},
                    2: {"id": 2, "text": "", "status": "review_required", "reason": "Unavailable"}}) as request:
                translated, issues, _ = translator.translate(cues, Path(directory), "en", lambda done: None)
                self.assertEqual(translated[1], cues[1])
                self.assertEqual(issues[0]["id"], 2)
                self.assertEqual(translated[0].start, cues[0].start)
                self.assertEqual(request.call_count, 1)
            with patch.object(translator, "_request", return_value={2: {"id": 2, "text": "원문 대사", "status": "translated", "reason": ""}}) as request:
                _, issues, _ = translator.translate(cues, Path(directory), "en", lambda done: None)
                self.assertEqual([cue.id for cue in request.call_args.args[0]], [2])
                self.assertFalse(issues)
            with patch.object(translator, "_request") as request:
                translator.translate(cues, Path(directory), "en", lambda done: None)
                request.assert_not_called()

    def test_language_track_selection_excludes_bitmap_and_wrong_language(self):
        streams = [{"index": 2, "codec": "hdmv_pgs_subtitle", "language": "eng"},
                   {"index": 3, "codec": "subrip", "language": "jpn"}, {"index": 4, "codec": "subrip", "language": "eng"}]
        self.assertEqual(pick_subtitles(streams, "en"), [streams[2]])
        self.assertEqual(pick_subtitles(streams, "ko"), [])

    def test_paths_with_spaces_and_nonrecursive_folder_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "한글 영상 & test.mp4"
            video.touch()
            (root / "generated.ko.srt").touch()
            (root / "nested").mkdir()
            (root / "nested" / "other.mp4").touch()
            self.assertEqual(collect_inputs([root, video]), [video.resolve()])

    def test_recursive_folder_import_ignores_subtitles_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "한글 영상.MP4"
            first.touch()
            nested = root / "시즌 1"
            nested.mkdir()
            second = nested / "다음 영상.mkv"
            second.touch()
            (nested / "다음 영상.srt").touch()
            (nested / "설명.txt").touch()
            self.assertEqual(collect_inputs([root, second], recursive=True), [first.resolve(), second.resolve()])

    def test_output_name_collision_is_only_within_same_folder(self):
        first, second = Path("D:/videos/movie.mp4"), Path("D:/videos/movie.mkv")
        other = Path("D:/videos/season2/movie.mp4")
        self.assertEqual(output_conflicts([first, second, other]), {first, second})
        self.assertFalse(output_conflicts([first, first, other]))
        original_named = Path("D:/videos/movie.original.mp4")
        self.assertEqual(output_conflicts([first, original_named]), {first, original_named})
        target_named = Path("D:/videos/movie.ja.mp4")
        self.assertEqual(output_conflicts([first, target_named], target_language="ja"), {first, target_named})
        self.assertFalse(output_conflicts([first, target_named], translate=False))

    def test_video_translation_writes_matching_name_without_changing_video(self):
        source = [Cue(1, 1, 3, "Hello")]
        payload = {1: {"id": 1, "text": "안녕하세요", "status": "translated", "reason": ""}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "한글 영상 & test.mp4"
            video.write_bytes(b"untouched video")
            with patch("subtitle_app.core.ROOT", root), patch("subtitle_app.core.codex_status", return_value="Logged in using ChatGPT"), \
                    patch("subtitle_app.core.inspect_media", return_value={"subtitles": []}), \
                    patch("subtitle_app.core.extract_embedded", return_value=source), patch.object(CodexTranslator, "_request", return_value=payload):
                result = Pipeline(Settings(), log=lambda text: None).process(video)
            self.assertEqual(result.matched_subtitle, (root / "한글 영상 & test.srt").resolve())
            self.assertEqual(read_srt(result.matched_subtitle)[0].text, "안녕하세요")
            self.assertEqual(read_srt(result.original)[0].text, "Hello")
            self.assertEqual(read_srt(result.translated), read_srt(result.matched_subtitle))
            self.assertEqual(read_json(result.report, {})["matched_subtitle"], str(result.matched_subtitle))
            self.assertEqual(video.read_bytes(), b"untouched video")
            with patch("subtitle_app.core.ROOT", root):
                result = Pipeline(Settings(translate=False, overwrite=True), log=lambda text: None).process(video)
            self.assertEqual(read_srt(result.matched_subtitle)[0].text, "Hello")

    def test_video_partial_translation_does_not_create_matching_name(self):
        source = [Cue(1, 1, 3, "Hello")]
        payload = {1: {"id": 1, "text": "", "status": "review_required", "reason": "Unavailable"}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "partial.mp4"
            video.touch()
            with patch("subtitle_app.core.ROOT", root), patch("subtitle_app.core.codex_status", return_value="Logged in using ChatGPT"), \
                    patch("subtitle_app.core.inspect_media", return_value={"subtitles": []}), \
                    patch("subtitle_app.core.extract_embedded", return_value=source), patch.object(CodexTranslator, "_request", return_value=payload):
                result = Pipeline(Settings(), log=lambda text: None).process(video)
            self.assertIsNone(result.matched_subtitle)
            self.assertFalse(video.with_suffix(".srt").exists())
            self.assertEqual(result.translated.name, "partial.ko.partial.srt")

    def test_existing_matching_subtitle_is_protected_before_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "existing.mp4"
            video.touch()
            matched = video.with_suffix(".srt")
            matched.write_text("existing subtitle", encoding="utf-8")
            with patch("subtitle_app.core.codex_status") as codex, patch("subtitle_app.core.inspect_media") as media:
                with self.assertRaises(FileExistsError):
                    Pipeline(Settings()).process(video)
            codex.assert_not_called()
            media.assert_not_called()
            self.assertEqual(matched.read_text(encoding="utf-8"), "existing subtitle")

    def test_cjk_line_wrapping_keeps_text(self):
        text = "안녕하세요 오늘은 자동 자막 생성 프로그램을 확인하고 있습니다."
        wrapped = wrap_subtitle(text)
        self.assertGreater(len(wrapped.splitlines()), 1)
        self.assertEqual("".join(wrapped.split()), "".join(text.split()))

    def test_cancelled_before_writing_anything(self):
        cancel = threading.Event()
        cancel.set()
        pipeline = Pipeline(Settings(translate=False), cancel=cancel)
        with self.assertRaises(Cancelled):
            pipeline.process(Path("unavailable.mp4"))

    def test_original_srt_input_without_codex(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "sample.srt"
            original.write_text(to_srt([Cue(1, 1, 3, "Original text")]), encoding="utf-8")
            pipeline = Pipeline(Settings(translate=False), log=lambda text: None)
            result = pipeline.process(original)
            self.assertIsNone(result.translated)
            self.assertEqual(read_srt(result.original)[0].text, "Original text")
            self.assertEqual(read_json(result.report, {})["status"], "original_only")
            self.assertEqual(original.read_text(encoding="utf-8"), to_srt([Cue(1, 1, 3, "Original text")]))

    def test_partial_output_does_not_claim_complete_and_records_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "partial.srt"
            source.write_text(to_srt([Cue(1, 1, 3, "Original dialogue")]), encoding="utf-8")
            with patch("subtitle_app.core.codex_status", return_value="Logged in using ChatGPT"), patch.object(CodexTranslator, "_request",
                    return_value={1: {"id": 1, "text": "", "status": "review_required", "reason": "Unable to process"}}):
                result = Pipeline(Settings(), log=lambda text: None).process(source)
            self.assertEqual(result.translated.name, "partial.ko.partial.srt")
            self.assertEqual(read_srt(result.translated)[0].text, "Original dialogue")
            self.assertFalse((Path(directory) / "partial.ko.srt").exists())
            self.assertEqual(read_json(result.report, {})["review_required"][0]["id"], 1)

    def test_translation_failure_preserves_original_and_report(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "failed.srt"
            source.write_text(to_srt([Cue(1, 1, 3, "Original dialogue")]), encoding="utf-8")
            with patch("subtitle_app.core.codex_status", return_value="Logged in using ChatGPT"), patch.object(CodexTranslator, "_request", side_effect=RuntimeError("network unavailable")):
                with self.assertRaises(RuntimeError):
                    Pipeline(Settings(), log=lambda text: None).process(source)
            self.assertEqual(read_srt(Path(directory) / "failed.original.srt")[0].text, "Original dialogue")
            self.assertEqual(read_json(Path(directory) / "failed.subtitle-report.json", {})["status"], "translation_failed")
            self.assertFalse((Path(directory) / "failed.ko.srt").exists())

    def test_gui_settings_json_has_no_bom_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as directory, patch("subtitle_app.core.ROOT", Path(directory)):
            settings = Settings(source_language="ja", glossary="John=존", overwrite=True)
            settings.save()
            self.assertFalse((Path(directory) / "settings.json").read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertEqual(Settings.load(), settings)


if __name__ == "__main__":
    unittest.main()
