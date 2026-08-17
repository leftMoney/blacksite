import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from processors.llm_router import codex_login_status, run_codex, selected_provider


class LLMRouterTests(unittest.TestCase):
    def test_selected_provider_defaults_to_claude(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("processors.llm_router.PROVIDER_DEFAULT", "claude"):
                self.assertEqual(selected_provider(), "claude")

    def test_selected_provider_accepts_codex(self):
        with patch.dict(os.environ, {"BLACKSITE_LLM_PROVIDER": "codex"}):
            self.assertEqual(selected_provider(), "codex")

    def test_codex_login_status_not_logged_in(self):
        fake = SimpleNamespace(returncode=1, stdout="Not logged in\n", stderr="")
        with patch("processors.llm_router.subprocess.run", return_value=fake):
            res = codex_login_status()
        self.assertFalse(res.ok)
        self.assertEqual(res.provider, "codex")

    def test_run_codex_reads_output_last_message(self):
        def fake_run(cmd, **_kwargs):
            out_path = Path(cmd[cmd.index("--output-last-message") + 1])
            out_path.write_text("OK", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch("processors.llm_router.subprocess.run", side_effect=fake_run):
            res = run_codex("say OK", tier="fast", model="gpt-5.4-mini", timeout_s=5)
        self.assertTrue(res.ok)
        self.assertEqual(res.text, "OK")
        self.assertEqual(res.model, "gpt-5.4-mini")

    def test_run_codex_passes_image_and_schema(self):
        seen = {}

        def fake_run(cmd, **_kwargs):
            seen["cmd"] = cmd
            out_path = Path(cmd[cmd.index("--output-last-message") + 1])
            out_path.write_text('{"ok": true}', encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as schema:
            schema_path = schema.name
        with patch("processors.llm_router.subprocess.run", side_effect=fake_run):
            res = run_codex(
                "judge",
                tier="audit",
                model="gpt-5.5",
                image_path="x.jpg",
                output_schema=schema_path,
                timeout_s=5,
            )
        self.assertTrue(res.ok)
        self.assertIn("--image", seen["cmd"])
        self.assertIn("--output-schema", seen["cmd"])


if __name__ == "__main__":
    unittest.main()
