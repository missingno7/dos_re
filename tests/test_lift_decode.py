"""dos_re.lift.decode: static lengths/kinds/targets, and the interpreter cross-check.

All code sequences are synthetic (hand-assembled) — no game bytes, per this
repo's game-free-tests rule. The last test is the load-bearing one: it runs
the same fetch8-counting probe liftgen uses and requires the static decoder
to agree with the interpreter (the semantic authority) on every length.
"""
from __future__ import annotations

import pytest

from dos_re.cpu import CPU8086, CPUState
from dos_re.lift.decode import (CALL, CALL_FAR, CALL_IND, INT, IRET, JCC, JMP,
                                JMP_FAR, JMP_IND, RET, RETF, SEQ, UNSUPPORTED,
                                decode_one)
from dos_re.memory import Memory


def _decode(hexbytes: str, ip: int = 0x100):
    raw = bytes.fromhex(hexbytes.replace(" ", ""))
    return decode_one(lambda off: raw[(off - ip) & 0xFFFF], ip)


@pytest.mark.parametrize("hexbytes,length,kind", [
    ("B8 34 12", 3, SEQ),          # mov ax, imm16
    ("B0 7F", 2, SEQ),             # mov al, imm8
    ("01 D8", 2, SEQ),             # add ax, bx (mod=11)
    ("8B 07", 2, SEQ),             # mov ax, [bx] (mod=00)
    ("8B 47 04", 3, SEQ),          # mov ax, [bx+4] (mod=01 disp8)
    ("8B 87 34 12", 4, SEQ),       # mov ax, [bx+0x1234] (mod=10 disp16)
    ("8B 06 34 12", 4, SEQ),       # mov ax, [0x1234] (mod=00 rm=110 disp16)
    ("26 8B 07", 3, SEQ),          # es: prefix
    ("F3 A4", 2, SEQ),             # rep movsb
    ("83 C4 08", 3, SEQ),          # add sp, imm8sx
    ("81 C4 34 12", 4, SEQ),       # add sp, imm16
    ("F7 07 34 12", 4, SEQ),       # test word [bx], imm16  (grp3 /0 has imm)
    ("F7 27", 2, SEQ),             # mul word [bx]          (grp3 /4 has no imm)
    ("F6 06 34 12 80", 5, SEQ),    # test byte [0x1234], imm8
    ("A0 34 12", 3, SEQ),          # mov al, [moffs16]
    ("C6 07 41", 3, SEQ),          # mov byte [bx], imm8
    ("C7 07 34 12", 4, SEQ),       # mov word [bx], imm16
    ("C8 04 00 00", 4, SEQ),       # enter 4, 0
    ("D4 0A", 2, SEQ),             # aam
    ("D1 E0", 2, SEQ),             # shl ax, 1
    ("C1 E0 04", 3, SEQ),          # shl ax, imm8 (186)
    ("6A 05", 2, SEQ),             # push imm8 (186)
    ("69 C3 34 12", 4, SEQ),       # imul r16, rm, imm16 (186)
    ("C3", 1, RET),
    ("C2 08 00", 3, RET),
    ("CB", 1, RETF),
    ("CF", 1, IRET),
    ("CC", 1, INT),
    ("F1", 1, UNSUPPORTED),
    ("D8 C1", 2, UNSUPPORTED),     # x87 esc: length still decodes via modrm
    ("0F 84 10 00", 1, UNSUPPORTED),  # 0f-escape refused at the first byte
])
def test_lengths_and_kinds(hexbytes, length, kind):
    inst = _decode(hexbytes)
    assert (inst.length, inst.kind) == (length, kind), inst


def test_branch_targets():
    assert _decode("EB 05").target == 0x107          # jmp short +5
    assert _decode("EB FE").target == 0x100          # jmp self
    assert _decode("75 FD", ip=0x10C).target == 0x10B  # jnz backwards
    assert _decode("E9 00 10").target == 0x1103      # jmp rel16
    assert _decode("E8 05 00").target == 0x108       # call rel16
    assert _decode("E2 F0").kind == JCC              # loop is conditional
    far = _decode("9A CC BB AA 99")
    assert far.kind == CALL_FAR and far.far_target == (0x99AA, 0xBBCC)
    jfar = _decode("EA 00 01 10 10")
    assert jfar.kind == JMP_FAR and jfar.far_target == (0x1010, 0x0100)
    assert _decode("CD 21").int_no == 0x21


def test_grp5_kinds_depend_on_reg_field():
    assert _decode("FF D3").kind == CALL_IND         # call bx        (/2)
    assert _decode("FF 16 34 12").kind == CALL_IND   # call [0x1234]  (/2, disp16)
    assert _decode("FF 1F").kind == CALL_IND         # call far [bx]  (/3)
    assert _decode("FF E3").kind == JMP_IND          # jmp bx         (/4)
    assert _decode("FF 27").kind == JMP_IND          # jmp [bx]       (/4)
    assert _decode("FF 37").kind == SEQ              # push [bx]      (/6)
    assert _decode("FF 07").kind == SEQ              # inc word [bx]  (/0)


def test_wraparound_fetch_is_callers_semantics():
    raw = bytes.fromhex("B83412")
    inst = decode_one(lambda off: raw[(off - 0xFFFE) % 3], 0xFFFE)
    assert inst.length == 3 and inst.next_ip == 0x0001


def test_static_lengths_agree_with_interpreter():
    """The §4 self-check, in-tree: the liftgen probe contract. For every
    non-transfer instruction, one interpreter step()'s IP delta must equal the
    statically decoded length (decode/operand fetches advance s.ip
    byte-by-byte, including the interpreter's inlined fast paths). Transfer
    encodings are fixed-size and asserted in the parametrized cases above."""
    code = bytes.fromhex(
        "B83412"      # mov ax, 0x1234
        "BB0200"      # mov bx, 2
        "01D8"        # add ax, bx
        "50"          # push ax
        "59"          # pop cx
        "268B0E3412"  # mov cx, es:[0x1234]
        "83C102"      # add cx, 2
        "F7E1"        # mul cx
        "C70734FF"    # mov word [bx], 0xFF34
        "AB"          # stosw
        "F3AA"        # rep stosb (cx drives it; fine)
        "8B871000"    # mov ax, [bx+0x0010]
        "F6C380"      # test bl, 0x80
        "D1E0"        # shl ax, 1
        "C8040000"    # enter 4, 0
        "C9"          # leave
    )
    mem = Memory()
    mem.load(0x1000, 0x0100, code)
    cpu = CPU8086(mem, CPUState(cs=0x1000, ip=0x0100, ds=0x2000, es=0x2000,
                                ss=0x3000, sp=0x0FFE, cx=1, di=0x10))
    cpu.trace_enabled = False

    ip = 0x0100
    end = 0x0100 + len(code)
    checked = 0
    while ip < end:
        inst = decode_one(lambda off: mem.rb(0x1000, off & 0xFFFF), ip)
        assert inst.kind == "seq", inst
        cpu.s.cs, cpu.s.ip = 0x1000, ip
        cpu.s.cx = 1        # keep REP bounded and the step re-runnable
        cpu.s.bp = 0x0F00   # keep enter/leave's stack frame sane
        cpu.s.sp = 0x0FFE
        cpu.step()
        delta = (cpu.s.ip - ip) & 0xFFFF
        assert delta == inst.length, (
            f"decoder disagrees with interpreter at {ip:04X}: "
            f"static={inst.length} interpreter-delta={delta} "
            f"bytes={inst.raw.hex()} ({inst.mnemonic})")
        checked += 1
        ip += inst.length
    assert checked == 16
