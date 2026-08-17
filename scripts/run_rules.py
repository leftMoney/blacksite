"""
Daemon shim — invokes processors.run.main() as a script.

The daemon's run_script() helper resolves paths relative to project root;
to run a -m module via the same helper we'd need a special case. This
one-line shim is cleaner than that.

Args after this script's name are forwarded to processors.run argparse.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from processors.run import main  # noqa: E402

if __name__ == "__main__":
    main()
