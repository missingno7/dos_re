"""CPU-ABI inference basics (dos_re.lift.cpuless) -- the M3 analysis layer.

Locks the per-instruction register-effects table and the per-function ABI
aggregation on small synthetic functions, so census results stay stable while
the emitter grows around them.
"""
from __future__ import annotations

from dos_re.lift.decode import Inst
from dos_re.lift.cfg import FunctionScan
from dos_re.lift.cpuless import abi_scan, register_effects


def _scan(insts):
    s = FunctionScan(entry=insts[0].ip)
    for i in insts:
        s.insts[i.ip] = i
    return s


def test_mov_imm_then_push_pop_ret_abi():
    insts = [
        Inst(ip=0, length=3, kind="seq", mnemonic="mov r16,imm16",
             raw=b"\xbb\x34\x12", op=0xBB, imm=0x1234),          # mov bx, 1234
        Inst(ip=3, length=1, kind="seq", mnemonic="push",
             raw=b"\x53", op=0x53),                               # push bx
        Inst(ip=4, length=1, kind="seq", mnemonic="pop",
             raw=b"\x58", op=0x58),                               # pop ax
        Inst(ip=5, length=1, kind="ret", mnemonic="ret",
             raw=b"\xc3", op=0xC3),                               # ret
    ]
    r = abi_scan(_scan(insts))
    assert r.tier == "leaf" and not r.refusals
    # bx is written before any read -> NOT an input; the stack machinery is.
    assert "bx" not in r.inputs and {"sp", "ss"} <= set(r.inputs)
    assert {"ax", "bx", "sp"} <= set(r.outputs)
    assert r.max_stack_use == 2          # one word pushed at the deepest point


def test_memory_read_pulls_in_base_reg_and_segment():
    # mov ax, [bx+si] -> reads bx, si, ds; writes ax
    e = register_effects(Inst(ip=0, length=2, kind="seq", mnemonic="mov",
                              raw=b"\x8b\x00", op=0x8B, modrm=0x00))
    assert {"bx", "si", "ds"} <= set(e.reads) and "ax" in e.writes
    assert e.mem_read and not e.mem_write


def test_bp_frame_ea_defaults_to_ss():
    # mov ax, [bp+6] -> reads bp, SS (not ds)
    e = register_effects(Inst(ip=0, length=3, kind="seq", mnemonic="mov",
                              raw=b"\x8b\x46\x06", op=0x8B, modrm=0x46, disp=6))
    assert {"bp", "ss"} <= set(e.reads) and "ds" not in e.reads


def test_int_effect_and_vectored_int_and_indirect():
    # A native DOS/BIOS INT is a PLATFORM EFFECT (explicit reg bundle), not a
    # refusal (tier 8).
    e = register_effects(Inst(ip=0, length=2, kind="int", mnemonic="int",
                              raw=b"\xcd\x21", op=0xCD, int_no=0x21))
    assert e.refusal is None and e.int_effect == 0x21
    assert {"ax", "bx", "cx", "dx", "ds", "es"} <= set(e.reads) <= set(e.writes | e.reads)
    # A game-installed vector (INT 61h sound driver) is a CALL INTO GAME
    # CODE through the runtime IVT (tier 12): full-bundle effect, the
    # recovered IRET-contract handler composes -- no refusal.
    ev = register_effects(Inst(ip=0, length=2, kind="int", mnemonic="int",
                               raw=b"\xcd\x61", op=0xCD, int_no=0x61))
    assert ev.refusal is None
    assert {"ax", "bx", "ds", "es", "ss"} <= set(ev.reads)
    assert "ss" not in ev.writes and "ax" in ev.writes
    # Any OTHER installed vector still refuses honestly.
    ev2 = register_effects(Inst(ip=0, length=2, kind="int", mnemonic="int",
                                raw=b"\xcd\x62", op=0xCD, int_no=0x62))
    assert ev2.refusal == "vectored-int-call"
    # NEAR indirect transfers are runtime-resolved recovered dispatch
    # (tier 9): conservative full-bundle dataflow, no refusal.
    e2 = register_effects(Inst(ip=0, length=2, kind="jmp_ind", mnemonic="jmp",
                               raw=b"\xff\xe0", op=0xFF, modrm=0xE0))
    assert e2.refusal is None
    assert {"ax", "bx", "ds", "es", "ss"} <= set(e2.reads)
    assert "ss" not in e2.writes and "ax" in e2.writes
    # FAR indirect jmp chains to a saved interrupt vector: the ISR-chain
    # tail (tier 13) -- full-bundle effect dispatched through HANDLERS on
    # the invoker's interrupt frame, no refusal.
    e3 = register_effects(Inst(ip=0, length=4, kind="jmp_ind", mnemonic="jmp",
                               raw=b"\xff\x2e\xbe\x1f", op=0xFF, modrm=0x2E,
                               disp=0x1FBE))
    assert e3.refusal is None
    assert {"ax", "ds", "es", "ss"} <= set(e3.reads)
    assert "ss" not in e3.writes and "ax" in e3.writes
    # FAR indirect call still refuses.
    e4 = register_effects(Inst(ip=0, length=2, kind="call_ind", mnemonic="call",
                               raw=b"\xff\xd8", op=0xFF, modrm=0xD8))
    assert e4.refusal == "indirect-or-far-transfer"


def test_loop_reads_and_writes_cx():
    e = register_effects(Inst(ip=0, length=2, kind="jcc", mnemonic="loop",
                              raw=b"\xe2\xfe", op=0xE2, target=0))
    assert "cx" in e.reads and "cx" in e.writes
