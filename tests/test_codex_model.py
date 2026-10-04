from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from subtitle_app.core import (DEFAULT_CODEX_MODEL, CodexTranslator, Cue, Settings,
    atomic_json, read_json, request_codex_json)


class CodexModelTests(unittest.TestCase):
    def test_old_blank_model_setting_uses_explicit_codex_model(self):
        with tempfile.TemporaryDirectory() as directory, patch("subtitle_app.core.ROOT", Path(directory)):
            atomic_json(Path(directory) / "settings.json", {"codex_model": "", "source_language": "ja"})
            settings = Settings.load()
            self.assertEqual(settings.codex_model, DEFAULT_CODEX_MODEL)
            self.assertEqual(settings.source_language, "ja")

    def test_actual_cli_model_is_confirmed_and_requested_explicitly(self):
        with tempfile.TemporaryDirectory() as directory, patch("subtitle_app.core.ROOT", Path(directory)), \
                patch("subtitle_app.core.find_codex", return_value="codex.exe"), \
                patch("subtitle_app.core.codex_status", return_value="Logged in using ChatGPT"):
            def respond(args, cancel, **kwargs):
                self.assertEqual(args[args.index("--model") + 1], DEFAULT_CODEX_MODEL)
                atomic_json(Path(args[args.index("-o") + 1]), {"value": "결과"})
                return 0, "", f"model: {DEFAULT_CODEX_MODEL}\nprovider: openai\n"
            runtime = {}
            with patch("subtitle_app.core.run_process", side_effect=respond):
                result = request_codex_json("data", {}, Settings(), threading.Event(), lambda text: None, runtime=runtime)
            self.assertEqual(result["value"], "결과")
            self.assertTrue(runtime["model_confirmed"])
            self.assertEqual(runtime["actual_model"], DEFAULT_CODEX_MODEL)
            self.assertEqual(runtime["authentication"], "chatgpt")

    def test_model_mismatch_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as directory, patch("subtitle_app.core.ROOT", Path(directory)), \
                patch("subtitle_app.core.find_codex", return_value="codex.exe"), \
                patch("subtitle_app.core.codex_status", return_value="Logged in using ChatGPT"), \
                patch("subtitle_app.core.run_process", return_value=(0, "", "model: unexpected-model\n")):
            with self.assertRaisesRegex(RuntimeError, "모델이 요청과 다릅니다"):
                request_codex_json("data", {}, Settings(), threading.Event(), lambda text: None)

    def test_api_key_login_is_not_used_for_pro_account_requests(self):
        with patch("subtitle_app.core.codex_status", return_value="Logged in using an API key"), \
                patch("subtitle_app.core.run_process") as request:
            with self.assertRaisesRegex(RuntimeError, "ChatGPT 계정"):
                request_codex_json("data", {}, Settings(), threading.Event(), lambda text: None)
            request.assert_not_called()

    def test_model_change_separates_cache_and_preserves_actual_model_metadata(self):
        cues = [Cue(1, 0, 2, "Hello")]
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            first = CodexTranslator(Settings(codex_model="gpt-6.1-sol"), threading.Event(), lambda text: None)
            def translate(*args):
                first.runtime.update(actual_model="gpt-6.1-sol", model_confirmed=True)
                return {1: {"id": 1, "text": "안녕", "status": "translated", "reason": ""}}
            with patch.object(first, "_request", side_effect=translate):
                first.translate(cues, cache, "en", lambda count: None)
            same = CodexTranslator(Settings(codex_model="gpt-6.1-sol"), threading.Event(), lambda text: None)
            with patch.object(same, "_request") as request:
                same.translate(cues, cache, "en", lambda count: None)
                request.assert_not_called()
            self.assertTrue(same.runtime["model_confirmed"])
            self.assertEqual(same.runtime["actual_model"], "gpt-6.1-sol")
            different = CodexTranslator(Settings(codex_model="gpt-6-sol"), threading.Event(), lambda text: None)
            with patch.object(different, "_request", return_value={1: {"id": 1, "text": "안녕하세요", "status": "translated", "reason": ""}}) as request:
                different.translate(cues, cache, "en", lambda count: None)
                request.assert_called_once()
            self.assertEqual(len(list(cache.glob("translation-*.metadata.json"))), 2)

    def test_updated_movie_policy_does_not_reuse_previous_prompt_translation(self):
        cues = [Cue(1, 0, 2, "Fuck off.")]
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            translator = CodexTranslator(Settings(), threading.Event(), lambda text: None)
            with patch("subtitle_app.core.PROMPT_VERSION", 1):
                old_checkpoint, _, _ = translator.load_checkpoint(cues, cache)
            atomic_json(old_checkpoint, {"1": {"id": 1, "text": "가 주세요.", "status": "translated", "reason": ""}})
            response = {1: {"id": 1, "text": "씨발, 꺼져.", "status": "translated", "reason": ""}}
            with patch.object(translator, "_request", return_value=response) as request:
                translated, issues, _ = translator.translate(cues, cache, "en", lambda count: None)
            request.assert_called_once()
            self.assertEqual(translated[0].text, response[1]["text"])
            self.assertFalse(issues)
            self.assertEqual(read_json(old_checkpoint, {})["1"]["text"], "가 주세요.")


if __name__ == "__main__":
    unittest.main()
