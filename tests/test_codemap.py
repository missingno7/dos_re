"""Focused tests for tools/codemap.py — observed-execution → entry derivation.

The discovery step of the automatic recovery pipeline: dynamic call targets,
INT-vectored ISR entries, and installed IVT vectors become the census entry
list; addresses that were never executed are rejected (a target that never
ran is a mis-observed transfer, not a function)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
_spec = importlib.util.spec_from_file_location("codemap", _TOOLS / "codemap.py")
codemap = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("codemap", codemap)
_spec.loader.exec_module(codemap)


def _observed():
    return {
        "executed": ["1010:0100", "1010:0200", "1010:0300", "2000:0040"],
        "call_targets": {"1010:0200": 5, "1010:0300": 1, "1010:0400": 3},
        "int_entries": ["2000:0040", "3000:0000"],
        "ivt_game_vectors": {"08": "1010:0100", "60": "4000:0000"},
    }


def test_call_targets_require_execution():
    entries = codemap.derive_entries(_observed())
    # 1010:0400 was a recorded target but never executed -> rejected.
    assert (0x1010, 0x0400) not in entries
    assert (0x1010, 0x0200) in entries
    assert (0x1010, 0x0300) in entries


def test_int_and_ivt_entries_require_execution_too():
    entries = codemap.derive_entries(_observed())
    assert (0x2000, 0x0040) in entries       # executed ISR entry
    assert (0x3000, 0x0000) not in entries   # never executed
    assert (0x1010, 0x0100) in entries       # executed IVT vector
    assert (0x4000, 0x0000) not in entries   # never executed


def test_min_calls_filters_rare_targets_but_not_isrs():
    entries = codemap.derive_entries(_observed(), min_calls=2)
    assert (0x1010, 0x0300) not in entries   # called once < 2
    assert (0x1010, 0x0200) in entries
    assert (0x2000, 0x0040) in entries       # ISR entries bypass min_calls


def test_segment_filter_and_extra_entries():
    entries = codemap.derive_entries(
        _observed(), segments={0x1010}, extra=((0x1010, 0x0777),))
    assert all(cs == 0x1010 for cs, _ in entries)
    assert (0x1010, 0x0777) in entries       # explicit extra kept
    assert (0x2000, 0x0040) not in entries   # filtered by segment
    assert entries == sorted(entries)
