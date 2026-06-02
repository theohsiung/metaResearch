"""Shared pytest configuration for the meta-research test suite.

Ensures the repository root is importable so ``import meta_research`` works even
when pytest is not invoked with ``PYTHONPATH=.`` from the repo root.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
