"""De-SMC analysis + transformation (dos_re.lift.smc, ``liftemit --desmc``).

The ordinary lift REFUSES mutable code (test_lift_selfmod.py) -- that baseline
stays.  This layer decides which refusals can be rehabilitated by modeling the
mutation as data flow: a patched IMMEDIATE (or absolute far-transfer pointer)
becomes a live read of the patched code bytes, which is what the real CPU
decodes.  Everything else stays refused.
"""
from __future__ import annotations

from dos_re.lift.cfg import scan_function
from dos_re.lift.emit import emit_function
from dos_re.lift.irgen_core import build_document
from dos_re.lift.smc import analyze_smc


def _fetch(code: bytes, base: int = 0):
    def fetch(off: int) -> int:
        i = off - base
        return code[i] if 0 <= i < len(code) else 0x90
    return fetch


def _scans(image: bytes, entries):
    return [(0x1010, ip, scan_function(_fetch(image), ip)) for ip in entries]


def test_self_patching_immediate_is_a_candidate() -> None:
    # 0000: mov cs:[0021], al      ; patches push imm8's operand (below)
    # 0004: mov cs:[0025], ax      ; patches add ax,imm16's operand
    # 0008..001F: nop sled
    # 0020: push imm8 (6a 05)
    # 0022: ret -- wait, keep the patched insts >16 bytes past entry
    image = bytearray(b"\x90" * 0x40)
    image[0x00:0x08] = bytes.fromhex("2ea221002ea32300")
    image[0x20:0x27] = bytes.fromhex("6a05050a00c3")
    scans = _scans(bytes(image), [0x0000])
    verdicts = analyze_smc(scans)
    v = verdicts[(0x1010, 0x0000)]
    assert v.status == "desmc-candidate"
    assert sorted(s.field_addr for s in v.slots) == [0x0021, 0x0023]
    assert {s.field_kind for s in v.slots} == {"imm"}
    ops = v.patched_operands()
    assert ops[0x0020] == ("imm", 0x0021, 1)
    assert ops[0x0022] == ("imm", 0x0023, 2)


def test_cross_function_patch_is_a_candidate_on_the_victim() -> None:
    # writer @0000: mov cs:[0121], al ; ret
    # victim @0100: 32 nops, then push imm8 @0120; ret
    image = bytearray(b"\x90" * 0x200)
    image[0x000:0x005] = bytes.fromhex("2ea22101c3")
    image[0x120:0x123] = bytes.fromhex("6a07c3")
    verdicts = analyze_smc(_scans(bytes(image), [0x0000, 0x0100]))
    assert (0x1010, 0x0000) not in verdicts          # the writer is not patched
    v = verdicts[(0x1010, 0x0100)]
    assert v.status == "desmc-candidate"
    assert v.slots[0].patcher_entry == 0x0000
    assert v.slots[0].target_ip == 0x0120


def test_multiple_patchers_of_one_slot_all_recorded() -> None:
    image = bytearray(b"\x90" * 0x200)
    image[0x000:0x005] = bytes.fromhex("2ea22101c3")     # patcher A
    image[0x040:0x045] = bytes.fromhex("2ea22101c3")     # patcher B
    image[0x120:0x123] = bytes.fromhex("6a07c3")
    verdicts = analyze_smc(_scans(bytes(image), [0x0000, 0x0040, 0x0100]))
    v = verdicts[(0x1010, 0x0100)]
    assert v.status == "desmc-candidate"
    assert sorted(s.patcher_entry for s in v.slots) == [0x0000, 0x0040]


def test_write_outside_the_operand_field_is_unsupported() -> None:
    # The patch hits the OPCODE byte of push imm8 -- instruction-shape
    # mutation, not an operand patch.
    image = bytearray(b"\x90" * 0x200)
    image[0x000:0x005] = bytes.fromhex("2ea22001c3")     # -> 0120 (the opcode)
    image[0x120:0x123] = bytes.fromhex("6a07c3")
    v = analyze_smc(_scans(bytes(image), [0x0000, 0x0100]))[(0x1010, 0x0100)]
    assert v.status == "desmc-unsupported"
    assert v.slots[0].status == "write-outside-operand-field"


def test_control_flow_displacement_patch_is_unsupported() -> None:
    # The patch hits a jcc rel8 displacement -- lifted control flow cannot
    # follow a mutated relative target in v1.
    image = bytearray(b"\x90" * 0x200)
    image[0x000:0x005] = bytes.fromhex("2ea22101c3")     # -> 0121 (jz's rel8)
    image[0x120:0x125] = bytes.fromhex("7402c3c3c3")     # jz +2; ret; ret; ret
    v = analyze_smc(_scans(bytes(image), [0x0000, 0x0100]))[(0x1010, 0x0100)]
    assert v.status == "desmc-unsupported"
    assert v.slots[0].status == "unsupported-target-form"


def test_far_jump_pointer_patch_is_a_candidate() -> None:
    # writer patches both words of a jmp far ptr16:16 (the SkyRoads timer-ISR
    # pattern: the installer stores the old vector into the chain jump).
    image = bytearray(b"\x90" * 0x200)
    image[0x000:0x00A] = bytes.fromhex("2ea321012ea32301c3")   # mov cs:[0121],ax; mov cs:[0123],dx
    image[0x120:0x125] = bytes.fromhex("ea53ff00f0")           # jmp far F000:FF53
    v = analyze_smc(_scans(bytes(image), [0x0000, 0x0100]))[(0x1010, 0x0100)]
    assert v.status == "desmc-candidate"
    assert all(s.field_kind == "far-target" and s.field_addr == 0x0121
               for s in v.slots)


def test_data_writes_produce_no_verdict_at_all() -> None:
    # A cs-store landing OUTSIDE every censused function is not SMC evidence
    # (a code-segment data cell); nothing is patched, nothing is refused.
    image = bytearray(b"\x90" * 0x200)
    image[0x000:0x005] = bytes.fromhex("2ea200 41c3".replace(" ", ""))  # -> 4100
    image[0x100:0x101] = b"\xc3"
    verdicts = analyze_smc(_scans(bytes(image), [0x0000, 0x0100]))
    assert verdicts == {}


def test_emit_reads_the_patched_operand_from_live_memory() -> None:
    # push imm8 with a patched_slot must emit a mem read + runtime sign
    # extension, not the frozen constant.
    from dataclasses import replace
    code = bytes.fromhex("6a05c3")
    scan = scan_function(_fetch(code), 0)
    scan.insts[0] = replace(scan.insts[0], patched_slot=("imm", 0x0001, 1))
    src = emit_function(scan, 0x1010, "t", signature=code[:3])
    assert "mem.rb(s.cs, 0x0001)" in src
    assert "0x0005" not in src            # the frozen constant is gone
    # far jmp with a patched pointer reads both words through the OLD cs.
    code2 = bytes.fromhex("ea53ff00f0")
    scan2 = scan_function(_fetch(code2), 0)
    scan2.insts[0] = replace(scan2.insts[0], patched_slot=("far-target", 0x0001, 4))
    src2 = emit_function(scan2, 0x1010, "t2", signature=code2)
    assert "_ti = mem.rw(s.cs, 0x0001)" in src2
    assert "_ts = mem.rw(s.cs, 0x0003)" in src2
    assert "s.cs, s.ip = _ts, _ti" in src2


def test_ir_document_carries_the_smc_verdicts_and_blocks() -> None:
    # The desmc candidate stays LIFTABLE=False (the ordinary lift must keep
    # refusing) but its record carries the smc verdict AND pinned blocks so
    # --desmc can emit from the IR alone.
    image = bytearray(b"\x90" * 0x40)
    image[0x00:0x04] = bytes.fromhex("2ea22100")
    image[0x20:0x23] = bytes.fromhex("6a05c3")
    doc = build_document([(0x1010, 0x0000)],
                         fetch_for=lambda cs: _fetch(bytes(image)),
                         provenance={}, notice="test")
    rec = doc["functions"]["1010:0000"]
    assert rec["liftable"] is False
    assert rec["smc"]["status"] == "desmc-candidate"
    assert rec["blocks"], "desmc candidate must pin its blocks in the IR"
    assert rec["smc"]["slots"][0]["field_addr"] == "0021"
