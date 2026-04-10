"""Shared test path bootstrap.

Tests import `sigmond` without requiring the package to be installed.
This mirrors what bin/smd does at runtime.
"""

import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / 'lib'
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
