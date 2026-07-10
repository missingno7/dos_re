"""dos_re.lift.manifest: the lifter's proof ledger, and emitted block coverage."""
from __future__ import annotations

import random

import pytest

from dos_re.cpu import CPU8086, CPUState
from dos_re.lift.cfg import scan_function
from dos_re.lift.emit import emit_function
from dos_re.lift.manifest import STATUSES, LiftManifest, LiftRecord
from dos_re.memory import Memory


def test_record_rejects_unknown_status():
    with pytest.raises(ValueError):
        LiftRecord(entry="1010:0100", module="x.py", status="RECOVERED")  # islands vocab, not ours


def test_manifest_roundtrips(tmp_path):
    m = LiftManifest()
    m.put(LiftRecord(entry="1010:0100", module="a.py", status="ORACLE_PASSING",
                     instructions=10, blocks=3, blocks_covered=3, calls=7, verified=7))
    m.put(LiftRecord(entry="1010:0200", module="b.py", status="NOT_REACHED"))
    p = tmp_path / "manifest.json"
    m.save(p)
    back = LiftManifest.load(p)
    assert back.records["1010:0100"].verified == 7
    assert back.records["1010:0100"].fully_covered
    assert back.summary() == {"ORACLE_PASSING": 1, "NOT_REACHED": 1}


def test_fully_covered_needs_all_blocks():
    rec = LiftRecord(entry="1010:0100", module="a.py", status="ORACLE_PASSING",
                     blocks=3, blocks_covered=2)
    assert not rec.fully_covered


def test_lift_statuses_are_disjoint_from_island_statuses():
    from dos_re.islands import STATUSES as ISLAND_STATUSES
    assert not (set(STATUSES) & set(ISLAND_STATUSES))   # metrics-honesty rule (§7)


def test_emitted_coverage_tracks_executed_blocks():
    # A diamond: block 0 branches to 1 or 2, both fall into 3 (ret).
    code = bytes.fromhex(
        "39D8"      # cmp ax, bx        block 0
        "7304"      # jnb +4 -> 0x0108
        "01D8"      # add ax, bx        block 1
        "EB02"      # jmp +2 -> 0x010A
        "29D8"      # sub ax, bx        block 2 (0x0108)
        "C3")       # ret               block 3 (0x010A)
    fetch = lambda off: code[(off - 0x100)] if 0 <= off - 0x100 < len(code) else 0x90
    scan = scan_function(fetch, 0x100)
    src = emit_function(scan, 0x1000, "lifted", signature=code[:6], coverage=True)
    ns: dict = {}
    exec(compile(src, "<lifted>", "exec"), ns)   # noqa: S102
    assert ns["BLOCK_COUNT"] == 4

    def run(ax, bx):
        mem = Memory(); mem.load(0x1000, 0x100, code)
        cpu = CPU8086(mem, CPUState(ax=ax, bx=bx, cs=0x1000, ip=0x100,
                                    ss=0x3000, sp=0x2000))
        cpu.trace_enabled = False
        cpu.push(0xBEEF)
        ns["lifted"](cpu)
        return ns["coverage"]()

    # One path covers 3 of 4 blocks — a single call is PARTIAL coverage.
    seen1, total = run(5, 3)     # ax>=bx: blocks 0 -> 2 -> 3
    assert total == 4 and seen1 == 3
    # Coverage ACCUMULATES across calls; the other branch fills in block 1.
    run(1, 9)                    # ax<bx: blocks 0 -> 1 -> 3
    seen2, _ = ns["coverage"]()
    assert seen2 == 4 and ns["BLOCKS_SEEN"] == {0, 1, 2, 3}
