"""Boss-facing CLI shim: re-exports processors.history_log CLI.

  py scripts/history.py ls --since 24h --scope bigo
  py scripts/history.py stats --since 7d
  py scripts/history.py show 42
  py scripts/history.py add --actor boss --kind directive --scope gpu \\
       --title "5070 上路 trigger phrase confirmed" \\
       --body "boss approved phrase. engine docs/SWITCHOVER_5070.md primed."
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from processors.history_log import main  # noqa: E402

if __name__ == "__main__":
    main()
