import os
import unittest
from pathlib import Path
from unittest.mock import patch

from processors.llm_router import LLMResult


class LLMSwitchPointTests(unittest.TestCase):
    def test_stage2_codex_path(self):
        from processors.pipeline import stage2_haiku_precision as stage2

        fake = LLMResult(
            ok=True,
            text='{"kb_admit":true,"kb_value_class":"medium","kb_value_score":55,'
                 '"decision_tags":"gambling,competitor","rationale":"operator ad"}',
            provider="codex",
            model="gpt-5.4-mini",
            duration_ms=10,
        )
        with patch.dict(os.environ, {"BLACKSITE_LLM_PROVIDER": "codex"}):
            with patch.object(stage2, "run_codex", return_value=fake):
                raw, meta = stage2.call_precision("", "ocr", b"fake", Path("x.jpg"))
        self.assertIn("kb_admit", raw)
        self.assertEqual(meta["_provider"], "codex")
        self.assertEqual(meta["_model"], "gpt-5.4-mini")

    def test_stage3_codex_path(self):
        from processors.pipeline import stage3_sonnet_strategic as stage3

        fake = LLMResult(
            ok=True,
            text="COMMERCIAL_ACTION:\nDo X\nCROSS_CASE_PATTERN:\nPattern Y\nCONFIDENCE: high\nRELATED_CASES: none",
            provider="codex",
            model="gpt-5.4",
            duration_ms=10,
        )
        with patch.dict(os.environ, {"BLACKSITE_LLM_PROVIDER": "codex"}):
            with patch.object(stage3, "run_codex", return_value=fake):
                raw, meta = stage3.call_strategic("ocr", "high", 80, "gambling", "rat", "none")
        self.assertIn("COMMERCIAL_ACTION", raw)
        self.assertEqual(meta["_provider"], "codex")

    def test_audit_codex_path(self):
        from processors.pipeline import audit_sonnet as audit

        fake = LLMResult(
            ok=True,
            text='{"your_verdict":"signal","your_kb_admit":true,'
                 '"your_kb_value_class":"medium","your_kb_value_score":60,'
                 '"qwen_correct":true,"haiku_correct":true,'
                 '"failure_mode":"none","comment":"ok"}',
            provider="codex",
            model="gpt-5.5",
            duration_ms=10,
        )
        ctx = {
            "file_path": "missing-ok-for-mock.jpg",
            "ocr_text": "examplebet bonus",
            "stage1": {"verdict": "signal", "confidence": 0.8, "qwen_tags": "[]"},
            "stage2": {"kb_admit": 1, "kb_value_class": "medium",
                       "kb_value_score": 60, "decision_tags": "gambling",
                       "rationale": "operator ad"},
        }
        with patch.dict(os.environ, {"BLACKSITE_LLM_PROVIDER": "codex"}):
            with patch.object(audit, "run_codex", return_value=fake):
                raw, meta = audit.call_audit(ctx)
        self.assertIn("your_verdict", raw)
        self.assertEqual(meta["_model"], "gpt-5.5")


if __name__ == "__main__":
    unittest.main()
