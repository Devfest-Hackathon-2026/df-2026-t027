"""テストから backend と 04_scripts を import できるようにする。"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

for path in (REPO_ROOT / "02_app", REPO_ROOT / "04_scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
