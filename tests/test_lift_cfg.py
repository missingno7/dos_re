"""dos_re.lift.cfg: region discovery, exits, calls, and the refusal taxonomy.

Synthetic hand-assembled functions only (game-free tests rule).
"""
from __future__ import annotations

from dos_re.lift.cfg import scan_function


def _fetch(code: bytes, base: int):
    return lambda off: code[(off - base) & 0xFFFF]


def test_loop_call_and_two_blocks_is_liftable():
    # 0100: mov ax, 0x1234
    # 0103: call 0x0110          (external helper; not part of the region)
    # 0106: dec cx
    # 0107: jnz 0x0106
    # 0109: ret
    code = bytes.fromhex("B83412" "E80A00" "49" "75FD" "C3")
    scan = scan_function(_fetch(code, 0x100), 0x100)
    assert scan.liftable
    assert sorted(scan.insts) == [0x100, 0x103, 0x106, 0x107, 0x109]
    assert [i.kind for i in scan.exits] == ["ret"]
    assert scan.calls_near == {0x110}
    assert scan.block_leaders() == [0x100, 0x106, 0x109]
    assert not scan.refusals


def test_indirect_call_is_liftable_but_recorded():
    # call bx; ret
    code = bytes.fromhex("FFD3" "C3")
    scan = scan_function(_fetch(code, 0x200), 0x200)
    assert scan.liftable and scan.calls_indirect == [0x200]


def test_int_is_liftable_and_recorded():
    # int 0x21; retf
    code = bytes.fromhex("CD21" "CB")
    scan = scan_function(_fetch(code, 0x300), 0x300)
    assert scan.liftable and scan.ints == {0x21}
    assert [i.kind for i in scan.exits] == ["retf"]


def test_indirect_jump_refuses():
    code = bytes.fromhex("FFE3")          # jmp bx
    scan = scan_function(_fetch(code, 0x100), 0x100)
    assert not scan.liftable
    assert [r.reason for r in scan.refusals] == ["indirect-jump"]


def test_x87_refuses_as_unsupported():
    code = bytes.fromhex("D8C1" "C3")     # fadd st1; ret
    scan = scan_function(_fetch(code, 0x100), 0x100)
    assert [r.reason for r in scan.refusals] == ["unsupported-opcode"]


def test_no_exit_refuses():
    code = bytes.fromhex("EBFE")          # jmp self
    scan = scan_function(_fetch(code, 0x100), 0x100)
    assert [r.reason for r in scan.refusals] == ["no-exit"]


def test_region_budget_refuses():
    code = bytes.fromhex("90" * 64 + "C3")
    scan = scan_function(_fetch(code, 0x100), 0x100, max_insts=16)
    assert any(r.reason == "region-budget" for r in scan.refusals)


def test_probe_mismatch_refuses():
    code = bytes.fromhex("B83412" "C3")
    scan = scan_function(_fetch(code, 0x100), 0x100,
                         probe=lambda ip: 2)   # deliberately wrong length
    assert any(r.reason == "decoder-mismatch" for r in scan.refusals)


def test_probe_unable_to_execute_is_recorded_not_fatal():
    code = bytes.fromhex("B83412" "C3")
    scan = scan_function(_fetch(code, 0x100), 0x100, probe=lambda ip: None)
    # Only the SEQ instruction is probed; the RET's fixed encoding is not.
    assert scan.liftable and scan.probe_unchecked == [0x100]


def test_far_jump_is_an_exit():
    # jcxz +5 ; jmp far 1234:0010 ; ret     (both paths exit)
    code = bytes.fromhex("E305" "EA10003412" "C3")
    scan = scan_function(_fetch(code, 0x100), 0x100)
    assert scan.liftable
    assert sorted(i.kind for i in scan.exits) == ["jmp_far", "ret"]
