"""Puts tests/ on sys.path so shared helpers (mongo_fakes) import from any test directory."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
