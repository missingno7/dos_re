"""Shared 8086-style arithmetic and string helpers for lifted routines.

Lifted routines (Python reimplementations of original ASM running against VM
memory/registers) must reproduce exact flag/register side effects.  These
helpers encode common 8086 instruction semantics (INC/DEC preserving CF,
REP MOVSB/STOSB/MOVSW fast paths that respect the EGA planar aperture, LOOP
count wrap, ...) so generated and authored implementations can share one proven
implementation instead of re-deriving flag behaviour per adapter.
"""
from __future__ import annotations

from .cpu import CF, DF, ZF
from .memory import EGA_CPU_APERTURE, EGA_PLANE_WINDOW


def loop_count(cx: int) -> int:
    """Return 8086 LOOP iteration count; CX=0 means 65536 iterations."""
    count = cx & 0xFFFF
    return 0x10000 if count == 0 else count

def _inc_reg16_preserve_cf(cpu, reg_idx: int) -> None:
    old = cpu.get_reg16(reg_idx)
    old_cf = cpu.get_flag(CF)
    cpu.set_reg16(reg_idx, (old + 1) & 0xFFFF)
    cpu.set_add_flags(old, 1, old + 1, 16)
    cpu.set_flag(CF, old_cf)


def _dec_reg16_preserve_cf(cpu, reg_idx: int) -> None:
    old = cpu.get_reg16(reg_idx)
    old_cf = cpu.get_flag(CF)
    cpu.set_reg16(reg_idx, (old - 1) & 0xFFFF)
    cpu.set_sub_flags(old, 1, old - 1, 16)
    cpu.set_flag(CF, old_cf)


def _add_reg16(cpu, reg_idx: int, value: int) -> None:
    old = cpu.get_reg16(reg_idx)
    result = old + (value & 0xFFFF)
    cpu.set_reg16(reg_idx, result)
    cpu.set_add_flags(old, value & 0xFFFF, result, 16)


def _sub_reg16(cpu, reg_idx: int, value: int) -> None:
    old = cpu.get_reg16(reg_idx)
    result = old - (value & 0xFFFF)
    cpu.set_reg16(reg_idx, result)
    cpu.set_sub_flags(old, value & 0xFFFF, result, 16)


def _add_mem_word(cpu, seg: int, off: int, value: int) -> None:
    old = cpu.mem.rw(seg, off)
    result = old + (value & 0xFFFF)
    cpu.mem.ww(seg, off, result)
    cpu.set_add_flags(old, value & 0xFFFF, result, 16)


def _sub_mem_word(cpu, seg: int, off: int, value: int) -> None:
    old = cpu.mem.rw(seg, off)
    result = old - (value & 0xFFFF)
    cpu.mem.ww(seg, off, result)
    cpu.set_sub_flags(old, value & 0xFFFF, result, 16)


def _and_mem_word(cpu, seg: int, off: int, value: int) -> None:
    result = cpu.mem.rw(seg, off) & (value & 0xFFFF)
    cpu.mem.ww(seg, off, result)
    cpu.set_logic_flags(result, 16)


def _inc_mem_byte_preserve_cf(cpu, seg: int, off: int) -> int:
    old = cpu.mem.rb(seg, off)
    old_cf = cpu.get_flag(CF)
    result = (old + 1) & 0xFF
    cpu.mem.wb(seg, off, result)
    cpu.set_add_flags(old, 1, old + 1, 8)
    cpu.set_flag(CF, old_cf)
    return result


def _dec_mem_byte_preserve_cf(cpu, seg: int, off: int) -> int:
    old = cpu.mem.rb(seg, off)
    old_cf = cpu.get_flag(CF)
    result_full = old - 1
    result = result_full & 0xFF
    cpu.mem.wb(seg, off, result)
    cpu.set_sub_flags(old, 1, result_full, 8)
    cpu.set_flag(CF, old_cf)
    return result


def _inc_mem_word_preserve_cf(cpu, seg: int, off: int) -> int:
    old = cpu.mem.rw(seg, off)
    old_cf = cpu.get_flag(CF)
    result_full = old + 1
    result = result_full & 0xFFFF
    cpu.mem.ww(seg, off, result)
    cpu.set_add_flags(old, 1, result_full, 16)
    cpu.set_flag(CF, old_cf)
    return result

def _dec_mem_word_preserve_cf(cpu, seg: int, off: int) -> int:
    old = cpu.mem.rw(seg, off)
    old_cf = cpu.get_flag(CF)
    result_full = old - 1
    result = result_full & 0xFFFF
    cpu.mem.ww(seg, off, result)
    cpu.set_sub_flags(old, 1, result_full, 16)
    cpu.set_flag(CF, old_cf)
    return result

def _and_mem_byte(cpu, seg: int, off: int, value: int) -> int:
    result = cpu.mem.rb(seg, off) & (value & 0xFF)
    cpu.mem.wb(seg, off, result)
    cpu.set_logic_flags(result, 8)
    return result


def _add_mem_byte(cpu, seg: int, off: int, value: int) -> int:
    old = cpu.mem.rb(seg, off)
    result_full = old + (value & 0xFF)
    result = result_full & 0xFF
    cpu.mem.wb(seg, off, result)
    cpu.set_add_flags(old, value & 0xFF, result_full, 8)
    return result


def _ega_aperture_overlap(seg: int, off: int, count: int) -> bool:
    """Return True when a flat byte transfer touches the emulated EGA aperture.

    Real EGA memory is not a linear bytearray: reads come from the selected read
    plane and writes land in the planes selected by the sequencer map mask.
    Slice-copy fast paths must therefore avoid this range, otherwise only one
    shadow plane is updated/read and moving EGA sprites leave coloured ghosts.
    The fast callers already restrict transfers to non-wrapping 16-bit offsets,
    so a simple physical interval check is enough here.
    """
    if count <= 0:
        return False
    start = (((seg & 0xFFFF) << 4) + (off & 0xFFFF)) & 0xFFFFF
    end = start + count
    ega_start = EGA_CPU_APERTURE
    ega_end = EGA_CPU_APERTURE + EGA_PLANE_WINDOW
    return start < ega_end and end > ega_start


def _cmp_word(cpu, a: int, b: int) -> None:
    cpu.set_sub_flags(a & 0xFFFF, b & 0xFFFF, (a & 0xFFFF) - (b & 0xFFFF), 16)


def _test_word(cpu, a: int, b: int) -> None:
    cpu.set_logic_flags((a & 0xFFFF) & (b & 0xFFFF), 16)


def _xor_al_al(cpu) -> None:
    cpu.set_reg8(0, 0)
    cpu.set_logic_flags(0, 8)


def _rep_movsb(cpu, count: int) -> None:
    count &= 0xFFFF
    if count == 0:
        cpu.s.cx = 0
        return

    # Fast path for the normal forward, non-wrapping case used by the render
    # blitters.  REP MOVSB does not alter FLAGS, so a bytearray slice copy is
    # behavior-equivalent as long as the 16-bit source/destination offsets and
    # 20-bit physical addresses do not wrap inside the transfer.
    if not cpu.get_flag(DF):
        si = cpu.s.si & 0xFFFF
        di = cpu.s.di & 0xFFFF
        if si + count <= 0x10000 and di + count <= 0x10000 \
                and not cpu.mem.write_watchers \
                and not (cpu.mem.ega_planar and (
                    _ega_aperture_overlap(cpu.s.ds, si, count)
                    or _ega_aperture_overlap(cpu.s.es, di, count)
                )):
            src = (((cpu.s.ds & 0xFFFF) << 4) + si) & 0xFFFFF
            dst = (((cpu.s.es & 0xFFFF) << 4) + di) & 0xFFFFF
            if src + count <= len(cpu.mem.data) and dst + count <= len(cpu.mem.data):
                cpu.mem.data[dst:dst + count] = cpu.mem.data[src:src + count]
                cpu.s.si = (si + count) & 0xFFFF
                cpu.s.di = (di + count) & 0xFFFF
                cpu.s.cx = 0
                return

    delta = -1 if cpu.get_flag(DF) else 1
    for _ in range(count):
        cpu.mem.wb(cpu.s.es, cpu.s.di, cpu.mem.rb(cpu.s.ds, cpu.s.si))
        cpu.s.si = (cpu.s.si + delta) & 0xFFFF
        cpu.s.di = (cpu.s.di + delta) & 0xFFFF
    cpu.s.cx = 0


def _rep_stosb(cpu, count: int) -> None:
    count &= 0xFFFF
    if count == 0:
        cpu.s.cx = 0
        return
    value = cpu.get_reg8(0)
    if not cpu.get_flag(DF):
        di = cpu.s.di & 0xFFFF
        if di + count <= 0x10000 and not cpu.mem.write_watchers \
                and not (cpu.mem.ega_planar and _ega_aperture_overlap(cpu.s.es, di, count)):
            dst = (((cpu.s.es & 0xFFFF) << 4) + di) & 0xFFFFF
            if dst + count <= len(cpu.mem.data):
                cpu.mem.data[dst:dst + count] = bytes([value]) * count
                cpu.s.di = (di + count) & 0xFFFF
                cpu.s.cx = 0
                return
    delta = -1 if cpu.get_flag(DF) else 1
    for _ in range(count):
        cpu.mem.wb(cpu.s.es, cpu.s.di, value)
        cpu.s.di = (cpu.s.di + delta) & 0xFFFF
    cpu.s.cx = 0


def _stosw(cpu) -> None:
    """Store AX to ES:DI and advance DI exactly like STOSW."""
    cpu.mem.ww(cpu.s.es, cpu.s.di, cpu.s.ax)
    cpu.s.di = (cpu.s.di + (-2 if cpu.get_flag(DF) else 2)) & 0xFFFF


def _rep_stosw_preserve_flags(cpu, count: int) -> None:
    """Execute REP STOSW without changing FLAGS."""
    count &= 0xFFFF
    if count == 0:
        cpu.s.cx = 0
        return
    delta = -2 if cpu.get_flag(DF) else 2
    value = cpu.s.ax & 0xFFFF
    for _ in range(count):
        cpu.mem.ww(cpu.s.es & 0xFFFF, cpu.s.di & 0xFFFF, value)
        cpu.s.di = (cpu.s.di + delta) & 0xFFFF
    cpu.s.cx = 0


def _ega_next_scanline_di(cpu) -> None:
    """Advance DI to the next planar 80-byte EGA/VGA scanline (2000h-interleave idiom).

    This mirrors the classic interleaved row-advance sequence (ADD DI,2000h /
    TEST DI,4000h / conditional ADD DI,0C050h) as used by Overkill's renderer;
    other games may use different interleave constants -- verify against your
    oracle before reusing."""
    _add_reg16(cpu, 7, 0x2000)  # ADD DI,2000h
    _test_word(cpu, cpu.s.di, 0x4000)
    if not cpu.get_flag(ZF):
        _add_reg16(cpu, 7, 0xC050)


def _cmp_byte(cpu, a: int, b: int) -> None:
    cpu.set_sub_flags(a & 0xFF, b & 0xFF, (a & 0xFF) - (b & 0xFF), 8)


def _rep_movsw(cpu, count: int) -> None:
    count &= 0xFFFF
    if count == 0:
        cpu.s.cx = 0
        return

    byte_count = count * 2
    if not cpu.get_flag(DF):
        si = cpu.s.si & 0xFFFF
        di = cpu.s.di & 0xFFFF
        if si + byte_count <= 0x10000 and di + byte_count <= 0x10000 \
                and not cpu.mem.write_watchers \
                and not (cpu.mem.ega_planar and (
                    _ega_aperture_overlap(cpu.s.ds, si, byte_count)
                    or _ega_aperture_overlap(cpu.s.es, di, byte_count)
                )):
            src = (((cpu.s.ds & 0xFFFF) << 4) + si) & 0xFFFFF
            dst = (((cpu.s.es & 0xFFFF) << 4) + di) & 0xFFFFF
            if src + byte_count <= len(cpu.mem.data) and dst + byte_count <= len(cpu.mem.data):
                cpu.mem.data[dst:dst + byte_count] = cpu.mem.data[src:src + byte_count]
                cpu.s.si = (si + byte_count) & 0xFFFF
                cpu.s.di = (di + byte_count) & 0xFFFF
                cpu.s.cx = 0
                return

    delta = -2 if cpu.get_flag(DF) else 2
    for _ in range(count):
        cpu.mem.ww(cpu.s.es, cpu.s.di, cpu.mem.rw(cpu.s.ds, cpu.s.si))
        cpu.s.si = (cpu.s.si + delta) & 0xFFFF
        cpu.s.di = (cpu.s.di + delta) & 0xFFFF
    cpu.s.cx = 0


def _inc_reg8_preserve_cf(cpu, idx: int) -> None:
    old_cf = cpu.get_flag(CF)
    old = cpu.get_reg8(idx)
    result = old + 1
    cpu.set_reg8(idx, result)
    cpu.set_add_flags(old, 1, result, 8)
    cpu.set_flag(CF, old_cf)


def _out_dx_ax(cpu) -> None:
    if cpu.port_writer:
        cpu.port_writer(cpu, cpu.s.dx & 0xFFFF, cpu.s.ax & 0xFFFF, 16)


def _out_dx_al(cpu) -> None:
    if cpu.port_writer:
        cpu.port_writer(cpu, cpu.s.dx & 0xFFFF, cpu.get_reg8(0), 8)
