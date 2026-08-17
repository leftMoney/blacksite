"""Daemon shim — invokes processors.funnel_edges.main() as a script."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from processors.funnel_edges import main  # noqa: E402

if __name__ == "__main__":
    main()
