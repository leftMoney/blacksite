"""Daemon shim — invokes processors.ocr_gemini.main() as a script."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import os
if os.environ.get('OCR_BACKEND', 'qwen_local').lower() == 'qwen_local':
    from processors.ocr_qwen_local import main
else:
    from processors.ocr_gemini import main  # noqa: E402

if __name__ == "__main__":
    main()
