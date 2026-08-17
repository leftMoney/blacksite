"""Daemon shim — invokes processors.asr_whisper.main() as a script."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from processors.asr_whisper import main  # noqa: E402

if __name__ == "__main__":
    main()
    os._exit(0)
