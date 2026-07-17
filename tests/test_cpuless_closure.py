"""Runtime-closure measurement: a resume address inside a promoted function.

A boundary head / snapshot entry / dispatch arrival is an offset INSIDE a
function, not its own IR entry. When that function is promoted, its recovered
body serves the resume (the plat.boundary observer / dispatch registry fires
there), so the address is COVERED -- not a spurious "not-in-ir" frontier gap.
An address inside NO promoted function is still a real frontier item.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from cpuless_closure import walk_closure  # noqa: E402


def _fn(insts):
    return {"blocks": [{"instructions": insts}]}


# One promoted function 1010:0100 spanning 0100..0106 (two 3-byte insts), with
# a resume head at 0103 (inside it, not an entry).
_IR = {
    "functions": {
        "1010:0100": _fn([
            {"ip": "0100", "bytes": "b80000", "mnemonic": "mov r16,imm16",
             "kind": "seq"},
            {"ip": "0103", "bytes": "b90000", "mnemonic": "mov r16,imm16",
             "kind": "seq"},
        ]),
    }
}
_PROMOTED = {"1010:0100"}


def test_resume_address_inside_a_promoted_function_is_covered() -> None:
    rep = walk_closure(_IR, ["1010:0100", "1010:0103"], _PROMOTED, {})
    # 0103 is inside the promoted 0100 -> not a frontier gap.
    assert "1010:0103" not in rep["frontier"]
    assert rep["frontier"] == {}
    assert rep["closure_complete"] is True
    assert "1010:0100" in [k for k in rep["frontier"]] or rep["promoted_reached"] == 1


def test_resume_address_outside_any_promoted_function_is_a_real_gap() -> None:
    # 0500 is inside NO promoted function -> a genuine not-in-ir frontier item.
    rep = walk_closure(_IR, ["1010:0100", "1010:0500"], _PROMOTED, {})
    assert rep["frontier"] == {"1010:0500": "not-in-ir"}
    assert rep["closure_complete"] is False
