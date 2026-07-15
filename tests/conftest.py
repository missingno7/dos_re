"""Test fixtures / path setup shared across the dos_re suite."""
import sys
from pathlib import Path

# The bit-exact OPL3 core was moved out of the importable package into
# graveyard/ (it is never selected at runtime; opl3_fast replaced it).  A few
# tests still use it as the calibration/golden oracle — put graveyard/ on the
# path so they can ``import opl3_exact``.
_GRAVEYARD = Path(__file__).resolve().parents[1] / "graveyard"
if _GRAVEYARD.is_dir() and str(_GRAVEYARD) not in sys.path:
    sys.path.insert(0, str(_GRAVEYARD))
