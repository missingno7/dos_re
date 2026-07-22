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


def test_les_lds_enter_lsl_shape():
    # les esi,[0x2000] / lds esi,[0x2000] (modrm 35 = mod0 reg6 rm5 -> disp32)
    assert (dec("C4 35 00 20 00 00").length, dec("C4 35 00 20 00 00").mnemonic) == (6, "les")
    assert (dec("C5 35 00 20 00 00").length, dec("C5 35 00 20 00 00").mnemonic) == (6, "lds")
    assert (dec("C8 A8 00 00").length, dec("C8 A8 00 00").mnemonic) == (4, "enter")
    assert (dec("0F 03 C0").length, dec("0F 03 C0").mnemonic) == (3, "lsl")
    for h in ("C4 35 00 20 00 00", "C5 35 00 20 00 00", "C8 A8 00 00", "0F 03 C0"):
        assert dec(h).kind == SEQ


def test_new_pm_ops_length_matches_interpreter():
    """The decode-vs-interpreter contract: decode32's length is exactly what
    CPU386 consumes.  A wrong length silently corrupts every later instruction,
    so it is checked against the oracle itself for the ops just added."""
    from dos_re.cpu386 import CPU386, FlatMemory
    for h in ("C4 35 00 20 00 00",     # les esi,[0x2000]
              "C5 35 00 20 00 00",     # lds esi,[0x2000]
              "C8 04 00 00",           # enter 4,0
              "0F 03 C0"):             # lsl eax,eax
        blob = bytes.fromhex(h.replace(" ", ""))
        mem = FlatMemory(size=0x8000)
        mem.load(0x1000, blob)
        cpu = CPU386(mem, eip=0x1000, esp=0x4000)
        cpu.step()
        assert cpu.eip - 0x1000 == decode32(mem.data.__getitem__, 0x1000).length, h
