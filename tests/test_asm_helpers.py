"""Smoke tests for shared 8086 flag/register helpers used by lifted code."""
from __future__ import annotations

from dos_re.asm import _rep_movsb, _rep_stosb, loop_count
from dos_re.cpu import CF, CPU8086, DF
from dos_re.memory import Memory


def _cpu() -> CPU8086:
    mem = Memory()
    cpu = CPU8086(mem)
    return cpu


def test_loop_count_cx_zero_means_65536():
    assert loop_count(0) == 0x10000
    assert loop_count(1) == 1
    assert loop_count(0xFFFF) == 0xFFFF


def test_rep_movsb_forward_copies_and_clears_cx_without_touching_flags():
    cpu = _cpu()
    cpu.s.ds = 0x1000
    cpu.s.es = 0x2000
    cpu.s.si = 0x0000
    cpu.s.di = 0x0010
    cpu.set_flag(DF, False)
    cpu.set_flag(CF, True)  # REP MOVSB must not alter FLAGS
    payload = bytes(range(16))
    for i, b in enumerate(payload):
        cpu.mem.wb(0x1000, i, b)

    _rep_movsb(cpu, 16)

    assert bytes(cpu.mem.rb(0x2000, 0x10 + i) for i in range(16)) == payload
    assert cpu.s.cx == 0
    assert cpu.s.si == 0x0010 and cpu.s.di == 0x0020
    assert cpu.get_flag(CF) is True


def test_rep_stosb_fills_destination():
    cpu = _cpu()
    cpu.s.es = 0x3000
    cpu.s.di = 0x0000
    cpu.set_reg8(0, 0xAB)  # AL
    cpu.set_flag(DF, False)

    _rep_stosb(cpu, 8)

    assert all(cpu.mem.rb(0x3000, i) == 0xAB for i in range(8))
    assert cpu.mem.rb(0x3000, 8) == 0
    assert cpu.s.di == 8
