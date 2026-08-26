"""Shared pytest configuration for the backend test-suite.

Ensures the ``backend/`` directory is importable so ``app.*`` absolute imports
resolve when running ``pytest`` from the repo root.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))
