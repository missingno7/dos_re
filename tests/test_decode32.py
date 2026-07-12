"""decode32 unit tests: hand-encoded instructions with known shapes."""
from __future__ import annotations

from dos_re.lift.decode32 import decode32
from dos_re.lift.decode import SEQ, JCC, JMP, CALL, CALL_IND, RET, INT, HLT


def dec(hexstr: str, ip: int = 0x1000):
    blob = bytes.fromhex(hexstr.replace(" ", ""))
    buf = {ip + i: b for i, b in enumerate(blob)}
    return decode32(lambda a: buf.get(a, 0), ip)


def test_mov_imm32():
    i = dec("B8 44 33 22 11")
    assert (i.length, i.kind, i.imm, i.opsize) == (5, SEQ, 0x11223344, 4)


def test_operand_size_prefix():
    i = dec("66 B8 34 12")
    assert (i.length, i.imm, i.opsize) == (4, 0x1234, 2)


def test_modrm_sib_disp8():
    # mov eax, [ebx+ecx*4+0x10]
    i = dec("8B 44 8B 10")
    assert (i.length, i.sib, i.disp) == (4, 0x8B, 0x10)


def test_modrm_sib_base5_mod0_disp32():
    # mov eax, [ecx*4 + 0x12345678]  (SIB base=101, mod=00 -> disp32)
    i = dec("8B 04 8D 78 56 34 12")
    assert (i.length, i.disp) == (7, 0x12345678)


def test_modrm_disp32_direct():
    # mov eax, [0x00048430]
    i = dec("A1 30 84 04 00")
    assert (i.length, i.imm) == (5, 0x48430)


def test_call_rel32_target():
    # KE 0x256EF: call 0x25722 (disp32 = 0x2E)
    i = dec("E8 2E 00 00 00", ip=0x256EF)
    assert (i.kind, i.target) == (CALL, 0x25722)


def test_jcc_short_backward():
    i = dec("75 F4", ip=0x2000)
    assert (i.kind, i.target) == (JCC, 0x2000 + 2 - 12)


def test_jcc_near_32():
    i = dec("0F 84 10 00 00 00", ip=0x3000)
    assert (i.kind, i.target, i.length) == (JCC, 0x3016, 6)


def test_grp5_indirect_call_vs_push():
    assert dec("FF D0").kind == CALL_IND            # call eax
    assert dec("FF 75 08").kind == SEQ              # push [ebp+8]
    assert dec("FF 75 08").mnemonic == "push"


def test_grp3_test_imm_vs_not():
    assert dec("F7 C0 10 00 00 00").length == 6     # test eax, imm32
    assert dec("F7 D0").length == 2                 # not eax


def test_string_rep_and_seg_prefix():
    i = dec("F3 A5")
    assert (i.length, i.rep) == (2, 0xF3)
    i = dec("26 8A 06")
    assert (i.length, i.seg_override) == (3, "es")


def test_x87_is_seq_with_length():
    i = dec("D9 2D 7C 87 04 00")                    # fldcw [0x4877c]
    assert (i.kind, i.length) == (SEQ, 6)


def test_ret_int_hlt():
    assert dec("C3").kind == RET
    assert dec("C2 08 00").imm == 8
    assert (dec("CD 21").kind, dec("CD 21").int_no) == (INT, 0x21)
    assert dec("F4").kind == HLT


def test_movzx_and_shld():
    assert dec("0F B6 C3").length == 3              # movzx eax, bl
    assert dec("0F A4 D0 04").length == 4           # shld eax, edx, 4


def test_jmp_short():
    i = dec("EB 47", ip=0x26520)
    assert (i.kind, i.target) == (JMP, 0x26569)
