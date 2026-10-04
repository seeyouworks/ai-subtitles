from __future__ import annotations

import os
import queue
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import Application, main
from subtitle_app.core import (Cancelled, CodexTranslator, Cue, Pipeline, Settings, atomic_json,
    atomic_text, collect_inputs, load_original_cache, lock_outputs, output_conflicts, output_paths,
    parse_srt, read_json, read_srt, run_process, to_srt)


class ReliabilityTests(unittest.TestCase):
    def test_failed_write_never_publishes_incomplete_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for protect in (False, True):
                output = root / "한글 자막.srt"
                with self.subTest(protect=protect), patch("subtitle_app.core.os.fsync", side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        atomic_text(output, "new subtitle", protect=protect)
                    self.assertFalse(output.exists())
                    self.assertFalse(list(root.glob("*.tmp")))
            output.write_text("old subtitle", encoding="utf-8")
            with patch("subtitle_app.core.os.fsync", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    atomic_text(output, "new subtitle")
            self.assertEqual(output.read_text(encoding="utf-8"), "old subtitle")

    def test_atomic_publication_failure_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "sample.srt"
            operation = "os.rename" if os.name == "nt" else "os.link"
            with patch(f"subtitle_app.core.{operation}", side_effect=PermissionError("read only")):
                with self.assertRaises(PermissionError):
                    atomic_text(output, "complete text", protect=True)
            self.assertEqual(list(output.parent.iterdir()), [])

    def test_malformed_settings_are_rejected_without_type_errors(self):
        invalid = [{"device": []}, {"source_language": None}, {"target_language": []},
                   {"batch_size": "40"}, {"timeout": True}, {"audio_track": -1},
                   {"translate": "false"}, {"overwrite": 1}, {"whisper_model": ""}, {"glossary": []}]
        with tempfile.TemporaryDirectory() as directory, patch("subtitle_app.core.ROOT", Path(directory)):
            for values in invalid:
                with self.subTest(values=values):
                    with self.assertRaises(ValueError):
                        Settings(**values).validate()
                    atomic_json(Path(directory) / "settings.json", values)
                    self.assertEqual(Settings.load(), Settings())

    def test_invalid_srt_times_and_cached_cues_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_srt("1\n00:60:00,000 --> 01:01:00,000\nBad time")
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "original.json"
            bad = [[], {"cues": [None], "language": "en", "method": "srt"},
                   {"cues": [{"id": 1, "start": float("nan"), "end": 2, "text": "Hello"}], "language": "en", "method": "srt"},
                   {"cues": [{"id": 2, "start": 0, "end": 2, "text": "Hello"}], "language": "en", "method": "srt"}]
            for payload in bad:
                with self.subTest(payload=payload):
                    atomic_json(cache, payload)
                    self.assertIsNone(load_original_cache(cache, lambda text: None))

    def test_corrupted_original_cache_is_rebuilt_from_source(self):
        with tempfile.TemporaryDirectory() as directory, patch("subtitle_app.core.ROOT", Path(directory)):
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(to_srt([Cue(1, 1, 2, "Hello")]), encoding="utf-8")
            Pipeline(Settings(translate=False), log=lambda text: None).process(source)
            original_cache = next((root / "cache").glob("*/original.json"))
            atomic_json(original_cache, {"cues": "corrupt", "language": "en", "method": "srt"})
            logs = []
            result = Pipeline(Settings(translate=False), log=logs.append).process(source)
            self.assertEqual(read_srt(result.original)[0].text, "Hello")
            self.assertIsInstance(read_json(original_cache, {})["cues"], list)
            self.assertTrue(any("손상" in line for line in logs))

    def test_corrupted_translation_rows_are_retried_and_valid_rows_reused(self):
        cues = [Cue(1, 0, 2, "Hello"), Cue(2, 2, 4, "World")]
        valid = {"id": 1, "text": "안녕", "status": "translated", "reason": ""}
        second = {"id": 2, "text": "세계", "status": "translated", "reason": ""}
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            translator = CodexTranslator(Settings(), threading.Event(), lambda text: None)
            checkpoint, _, _ = translator.load_checkpoint(cues, cache)
            for bad in ({**second, "id": 1}, {**second, "text": ""}, {**second, "status": []}, None):
                with self.subTest(bad=bad):
                    atomic_json(checkpoint, {"1": valid, "2": bad, "unknown": second, "999": second})
                    with patch.object(translator, "_request", return_value={2: second}) as request:
                        translated, issues, _ = translator.translate(cues, cache, "en", lambda count: None)
                    self.assertEqual([cue.id for cue in request.call_args.args[0]], [2])
                    self.assertEqual([cue.text for cue in translated], ["안녕", "세계"])
                    self.assertFalse(issues)
            atomic_json(checkpoint, [valid, second])
            with patch.object(translator, "_request", return_value={1: valid, 2: second}) as request:
                translator.translate(cues, cache, "en", lambda count: None)
            self.assertEqual(len(request.call_args.args[0]), 2)

    def test_video_outputs_cannot_overwrite_selected_srt_inputs(self):
        video = Path("D:/videos/movie.mp4")
        for name in ("movie.srt", "movie.original.srt", "movie.ko.srt"):
            source = video.with_name(name)
            with self.subTest(name=name):
                self.assertEqual(output_conflicts([video, source]), {video, source})
        first = Path("D:/videos/movie.srt")
        second = first.with_name("movie.original.srt")
        self.assertEqual(output_conflicts([first, second]), {first, second})

    def test_existing_translation_with_incomplete_checkpoint_prevents_new_request(self):
        with tempfile.TemporaryDirectory() as directory, patch("subtitle_app.core.ROOT", Path(directory)):
            root = Path(directory).resolve()
            source = root / "movie.srt"
            text = to_srt([Cue(1, 0, 2, "Original")])
            source.write_text(text, encoding="utf-8")
            Pipeline(Settings(translate=False), log=lambda text: None).process(source)
            (root / "movie.ko.srt").write_text("foreign subtitle", encoding="utf-8")
            with patch.object(CodexTranslator, "_request") as request:
                with self.assertRaisesRegex(FileExistsError, "번역 자막"):
                    Pipeline(Settings(), log=lambda text: None).process(source)
            request.assert_not_called()
            self.assertEqual(source.read_text(encoding="utf-8"), text)
            self.assertEqual((root / "movie.ko.srt").read_text(encoding="utf-8"), "foreign subtitle")

    def test_missing_and_unsupported_inputs_are_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good = root / "good.mp4"
            good.touch()
            with self.assertRaises(FileNotFoundError):
                collect_inputs([good, root / "missing.mp4"])
            other = root / "other.txt"
            other.touch()
            with self.assertRaises(ValueError):
                collect_inputs([other])

    def test_output_locks_prevent_overlap_and_release_after_failure(self):
        with tempfile.TemporaryDirectory() as directory, patch("subtitle_app.core.ROOT", Path(directory)):
            root = Path(directory)
            first = output_paths(root / "movie.mp4", "ko", True)
            second = output_paths(root / "movie.original.mp4", "ko", True)
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                with lock_outputs(first):
                    with self.assertRaisesRegex(RuntimeError, "다른 실행"):
                        with lock_outputs(second):
                            self.fail("Overlapping outputs were allowed")
                    raise RuntimeError("injected failure")
            with lock_outputs(second):
                pass

    def test_large_stdin_does_not_block_timeout_when_child_never_reads(self):
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            run_process([sys.executable, "-c", "import time; time.sleep(30)"], threading.Event(),
                        stdin="안녕" * 500000, timeout=0.4)
        self.assertLess(time.monotonic() - started, 10)

    def test_large_stdin_cancellation_and_utf8_delivery(self):
        cancel = threading.Event()
        timer = threading.Timer(0.4, cancel.set)
        timer.start()
        try:
            with self.assertRaises(Cancelled):
                run_process([sys.executable, "-c", "import time; time.sleep(30)"], cancel, stdin="Hello" * 500000)
        finally:
            timer.cancel()
        code, output, _ = run_process([sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
                                     threading.Event(), stdin="안녕 / 你好 / 日本語 / Español / Русский")
        self.assertEqual(code, 0)
        self.assertEqual(output, "안녕 / 你好 / 日本語 / Español / Русский")

    def test_output_failure_report_and_retry_reuse_complete_translation(self):
        with tempfile.TemporaryDirectory() as directory, patch("subtitle_app.core.ROOT", Path(directory)):
            root = Path(directory).resolve()
            video = root / "movie.mp4"
            video.write_bytes(b"untouched")
            source = [Cue(1, 0, 2, "Hello")]
            rows = {1: {"id": 1, "text": "안녕", "status": "translated", "reason": ""}}
            original_write = atomic_text
            def fail_alias(path, text, **kwargs):
                if path == video.with_suffix(".srt"):
                    raise OSError("disk full")
                original_write(path, text, **kwargs)
            with patch("subtitle_app.core.inspect_media", return_value={"subtitles": []}), \
                    patch("subtitle_app.core.extract_embedded", return_value=source), \
                    patch.object(CodexTranslator, "_request", return_value=rows), \
                    patch("subtitle_app.core.atomic_text", side_effect=fail_alias):
                with self.assertRaisesRegex(OSError, "disk full"):
                    Pipeline(Settings(), log=lambda text: None).process(video)
            report = read_json(root / "movie.subtitle-report.json", {})
            self.assertEqual(report["status"], "output_failed")
            self.assertTrue((root / "movie.ko.srt").exists())
            self.assertFalse(video.with_suffix(".srt").exists())
            with patch.object(CodexTranslator, "_request") as request, patch("subtitle_app.core.inspect_media") as media:
                result = Pipeline(Settings(), log=lambda text: None).process(video)
            request.assert_not_called()
            media.assert_not_called()
            self.assertEqual(read_srt(result.matched_subtitle)[0].text, "안녕")
            self.assertEqual(read_json(result.report, {})["status"], "complete")
            self.assertEqual(video.read_bytes(), b"untouched")

    def test_translation_file_save_failure_is_recorded(self):
        with tempfile.TemporaryDirectory() as directory, patch("subtitle_app.core.ROOT", Path(directory)):
            root = Path(directory).resolve()
            source = root / "movie.srt"
            source.write_text(to_srt([Cue(1, 0, 2, "Hello")]), encoding="utf-8")
            rows = {1: {"id": 1, "text": "안녕", "status": "translated", "reason": ""}}
            original_write = atomic_text
            def fail_translation(path, text, **kwargs):
                if path == root / "movie.ko.srt":
                    raise OSError("disk full")
                original_write(path, text, **kwargs)
            with patch.object(CodexTranslator, "_request", return_value=rows), \
                    patch("subtitle_app.core.atomic_text", side_effect=fail_translation):
                with self.assertRaises(OSError):
                    Pipeline(Settings(), log=lambda text: None).process(source)
            self.assertEqual(read_json(root / "movie.subtitle-report.json", {})["status"], "output_failed")

    def test_previous_complete_subtitles_retained_and_reported_for_partial_run(self):
        with tempfile.TemporaryDirectory() as directory, patch("subtitle_app.core.ROOT", Path(directory)):
            root = Path(directory).resolve()
            video = root / "movie.mp4"
            video.touch()
            matched = video.with_suffix(".srt")
            target = root / "movie.ko.srt"
            for output in (matched, target):
                output.write_text("previous subtitle", encoding="utf-8")
            (root / "movie.ko.partial.srt").write_text("previous partial", encoding="utf-8")
            rows = {1: {"id": 1, "text": "", "status": "review_required", "reason": "Unavailable"}}
            with patch("subtitle_app.core.inspect_media", return_value={"subtitles": []}), \
                    patch("subtitle_app.core.extract_embedded", return_value=[Cue(1, 0, 2, "Hello")]), \
                    patch.object(CodexTranslator, "_request", return_value=rows):
                result = Pipeline(Settings(overwrite=True), log=lambda text: None).process(video)
            report = read_json(result.report, {})
            self.assertEqual(report["status"], "review_required")
            self.assertIsNone(result.matched_subtitle)
            self.assertEqual(set(report["retained_outputs"]), {str(matched), str(target)})
            self.assertEqual(matched.read_text(encoding="utf-8"), "previous subtitle")

    def test_source_change_during_extraction_prevents_publication(self):
        with tempfile.TemporaryDirectory() as directory, patch("subtitle_app.core.ROOT", Path(directory)):
            video = Path(directory).resolve() / "movie.mp4"
            video.write_bytes(b"original")
            def extract(*args):
                video.write_bytes(b"changed input video")
                return [Cue(1, 0, 2, "Hello")]
            with patch("subtitle_app.core.inspect_media", return_value={"subtitles": []}), \
                    patch("subtitle_app.core.extract_embedded", side_effect=extract):
                with self.assertRaisesRegex(RuntimeError, "입력 파일이 변경"):
                    Pipeline(Settings(translate=False), log=lambda text: None).process(video)
            self.assertFalse(video.with_suffix(".srt").exists())
            self.assertFalse(video.with_name("movie.original.srt").exists())

    def test_source_change_during_translation_is_reported_without_final_subtitle(self):
        with tempfile.TemporaryDirectory() as directory, patch("subtitle_app.core.ROOT", Path(directory)):
            root = Path(directory).resolve()
            source = root / "movie.srt"
            source.write_text(to_srt([Cue(1, 0, 2, "Hello")]), encoding="utf-8")
            def translate(*args):
                source.write_text(to_srt([Cue(1, 0, 2, "Changed dialogue")]), encoding="utf-8")
                return {1: {"id": 1, "text": "안녕", "status": "translated", "reason": ""}}
            with patch.object(CodexTranslator, "_request", side_effect=translate):
                with self.assertRaisesRegex(RuntimeError, "입력 파일이 변경"):
                    Pipeline(Settings(), log=lambda text: None).process(source)
            report = read_json(root / "movie.subtitle-report.json", {})
            self.assertEqual(report["status"], "translation_failed")
            self.assertIn("입력 파일이 변경", report["error"])
            self.assertFalse((root / "movie.ko.srt").exists())

    def test_worker_initialization_failure_always_finishes(self):
        application = Application.__new__(Application)
        application.cancel = threading.Event()
        application.events = queue.Queue()
        with patch("app.Pipeline", side_effect=RuntimeError("initialization failed")):
            application.worker(Settings(), [Path("movie.mp4")])
        events = list(application.events.queue)
        self.assertEqual(events[-1], ("finished", 0, 1, 0, False, 0))
        self.assertIn("initialization failed", events[0][1])

    def test_cli_bad_settings_have_friendly_error_and_no_inference(self):
        with patch.object(sys, "argv", ["app.py", "--cli", "movie.mp4", "--batch-size", "0"]), \
                patch("app.Pipeline") as pipeline, patch("sys.stderr") as error:
            with self.assertRaises(SystemExit) as failure:
                main()
        self.assertEqual(failure.exception.code, 2)
        pipeline.assert_not_called()
        self.assertTrue(error.write.called)


if __name__ == "__main__":
    unittest.main()
