"""CPU-ABI inference basics (dos_re.lift.cpuless) -- the M3 analysis layer.

Locks the per-instruction register-effects table and the per-function ABI
aggregation on small synthetic functions, so census results stay stable while
the emitter grows around them.
"""
from __future__ import annotations

from dos_re.lift.decode import Inst, decode_one
from dos_re.lift.cfg import FunctionScan
from dos_re.lift.cpuless import abi_scan, register_effects
from dos_re.lift.emit_cpuless import check_promotable, emit_recovered


def _scan(insts):
    s = FunctionScan(entry=insts[0].ip)
    for i in insts:
        s.insts[i.ip] = i
    return s


def _scan_bytes(hexbytes, exits_at=()):
    """Decode a straight-line function from hex bytes into a FunctionScan.
    ``exits_at`` names the ip(s) whose decoded instruction is a return exit."""
    raw = bytes.fromhex(hexbytes.replace(" ", ""))
    scan = FunctionScan(entry=0)
    ip = 0
    while ip < len(raw):
        inst = decode_one(lambda off, _r=raw: _r[off], ip)
        scan.insts[ip] = inst
        if inst.kind == "int":
            scan.ints.add(inst.int_no)
        if ip in exits_at:
            scan.exits.append(inst)
        ip += inst.length
    return scan


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


def test_xlat_declares_the_segment_it_reads():
    """xlat reads [seg:bx+al] and the EMITTER honours a segment-override
    prefix; the effects model must declare the SAME register or the emitted
    body names an input the function never took (NameError at runtime).

    Regression: `2E D7` (xlat cs:) emitted mem.rb(cs, ...) into a function
    without cs -- lemmings 1010:1462, the machine-type-2 video probe.
    """
    plain = register_effects(Inst(ip=0, length=1, kind="seq", mnemonic="xlat",
                                  raw=b"\xd7", op=0xD7))
    assert "ds" in plain.reads and "ax" in plain.reads and "bx" in plain.reads

    for prefix, seg in ((0x2E, "cs"), (0x26, "es"), (0x36, "ss"), (0x3E, "ds")):
        e = register_effects(Inst(ip=0, length=2, kind="seq", mnemonic="xlat",
                                  raw=bytes((prefix, 0xD7)), op=0xD7,
                                  prefixes=(prefix,)))
        assert seg in e.reads, f"xlat with {prefix:#04x} must declare {seg}"
        assert "ax" in e.writes


def test_platform_int_forces_flags_livein():
    """A PLATFORM INT (plat.intr) makes the function flags-live-in, so the
    emitted body seeds the INT's flag word from the caller's FLAGS.

    Regression (SimAnt _profstart 275F:003C = `mov ax,4503h; int 2Fh; retf`):
    INT/IRET preserves FLAGS except where the handler edits the stacked copy,
    so the INT 2Fh multiplex with no TSR is an IRET that returns FLAGS
    unchanged.  The emitter models the INT's flag output via plat.intr, which
    reads its `_flags` input back out -- so without flags_livein the body seeds
    that input from the zero default and CLOBBERS the caller's preserved flags
    (state divergence vs the interpreted oracle over the whole demo, bisected
    to exactly this leaf).  A platform INT must trigger flags_livein just like
    a game-vectored INT does.
    """
    scan = _scan_bytes("B8 03 45 CD 2F CB", exits_at=(5,))    # mov ax,4503; int 2F; retf
    spec = check_promotable(scan)
    assert spec.ret_kind == "far" and spec.flags_livein is True
    src = emit_recovered(scan, spec.abi, "275F:003C",
                         recovered_import_base="x", needs_plat=spec.needs_plat,
                         df_livein=spec.df_livein, sp_output=spec.sp_output,
                         flags_livein=spec.flags_livein)
    # the flag locals seed from the caller word, and the INT bundle threads it
    assert "_flags_in" in src and "cf = (_flags_in & 0x1) != 0" in src
    assert "'_flags': (" in src            # plat.intr receives the live flags

    # A leaf with NO interrupt stays flags-dead-in (the trigger is specific).
    plain = _scan_bytes("B8 03 45 C3", exits_at=(3,))          # mov ax,4503; ret
    assert check_promotable(plain).flags_livein is False
