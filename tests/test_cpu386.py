"""CPU386 / FlatMemory / VGASequencer focused tests (game-free by construction).

Each test hand-assembles a tiny 386 code blob, runs it on the flat core, and
asserts registers/memory/flags — the test_core.py style applied to the
protected-mode interpreter.
"""
from __future__ import annotations

from dos_re.cpu386 import CPU386, FlatMemory, EAX, EBX, ECX, EDX, ESP
from dos_re.dos4gw import VGASequencer
from dos_re.cpu import CF, ZF, SF, HaltExecution

import pytest

CODE = 0x1000
STACK = 0x8000


def run_blob(blob: bytes, *, max_steps: int = 1000, setup=None):
    mem = FlatMemory(size=0x20000)
    mem.load(CODE, blob + b"\xF4")          # HLT terminator
    cpu = CPU386(mem, eip=CODE, esp=STACK)
    if setup:
        setup(cpu, mem)
    try:
        cpu.run(max_steps)
    except HaltExecution:
        pass
    return cpu, mem


def test_mov_alu_32bit():
    # mov eax, 0x11223344 ; mov ebx, 0x100 ; add eax, ebx ; sub eax, 4
    cpu, _ = run_blob(bytes.fromhex("B844332211 BB00010000 01D8 83E804".replace(" ", "")))
    assert cpu.r[EAX] == 0x11223440
    assert not cpu.get_flag(CF)


def test_operand_size_prefix_16bit():
    # mov eax, -1 ; 66 mov ax, 0x1234  (high half preserved)
    cpu, _ = run_blob(bytes.fromhex("B8FFFFFFFF 66B83412".replace(" ", "")))
    assert cpu.r[EAX] == 0xFFFF1234


def test_push_pop_stack():
    # push 0xDEAD ; pop eax
    cpu, _ = run_blob(bytes.fromhex("68ADDE0000 58".replace(" ", "")))
    assert cpu.r[EAX] == 0xDEAD
    assert cpu.r[ESP] == STACK


def test_sib_addressing_and_flags():
    # mov ecx, 4 ; mov edx, 0x2000 ; mov dword [edx+ecx*4], 0x55 ;
    # mov eax, [edx+ecx*4] ; cmp eax, 0x55
    blob = bytes.fromhex("B904000000 BA00200000 C7048A55000000 8B048A 3D55000000".replace(" ", ""))
    cpu, mem = run_blob(blob)
    assert mem.r32(0x2010) == 0x55
    assert cpu.r[EAX] == 0x55
    assert cpu.get_flag(ZF)


def test_string_rep_movsd():
    # mov esi, 0x2000 ; mov edi, 0x3000 ; mov ecx, 4 ; rep movsd
    def setup(cpu, mem):
        mem.load(0x2000, bytes(range(16)))
    blob = bytes.fromhex("BE00200000 BF00300000 B904000000 F3A5".replace(" ", ""))
    cpu, mem = run_blob(blob, setup=setup)
    assert mem.block(0x3000, 16) == bytes(range(16))
    assert cpu.r[ECX] == 0


def test_selector_base_resolution():
    # mov ax, 0x80 ; mov es, ax ; mov byte es:[0x10], 0xAB
    def setup(cpu, mem):
        cpu.selector_bases[0x80] = 0x5000
    blob = bytes.fromhex("66B88000 8EC0 26C60510000000AB".replace(" ", ""))
    cpu, mem = run_blob(blob, setup=setup)
    assert mem.data[0x5010] == 0xAB          # based selector: 0x5000 + 0x10
    assert mem.data[0x10] == 0               # flat address untouched


def test_irq_delivery_and_iret():
    # Handler at 0x4000: inc ebx ; iretd.  Main: sti ; nop ; nop.
    fired = []

    def setup(cpu, mem):
        mem.load(0x4000, bytes.fromhex("43CF"))
        cpu.idt[0x08] = (cpu.seg["cs"], 0x4000)
        def pending():
            if not fired:
                fired.append(1)
                return 0                     # IRQ0 -> vector 8
            return None
        cpu.pending_irq = pending
    cpu, _ = run_blob(bytes.fromhex("FB9090"), setup=setup)
    assert cpu.r[EBX] == 1                   # handler ran exactly once
    assert cpu.eip == CODE + 4               # resumed and hit the HLT


def test_vga_planar_write_and_readback():
    vga = VGASequencer()
    vga.chain4 = False
    vga.map_mask = 0b0101                    # planes 0 and 2
    vga.write(0x100, 0x7E)
    assert vga.planes[0][0x100] == 0x7E
    assert vga.planes[2][0x100] == 0x7E
    assert vga.planes[1][0x100] == 0x00
    vga.read_map = 2
    assert vga.read(0x100) == 0x7E


def test_vga_write_mode1_latch_copy():
    vga = VGASequencer()
    for p in range(4):
        vga.planes[p][0x10] = 0x40 + p
    vga.read(0x10)                           # load latches
    vga.write_mode = 1
    vga.map_mask = 0x0F
    vga.write(0x20, 0xFF)                    # CPU byte ignored; latches copied
    assert [vga.planes[p][0x20] for p in range(4)] == [0x40, 0x41, 0x42, 0x43]


def test_flatmemory_routes_aperture_when_attached():
    mem = FlatMemory(size=0xB1000)
    vga = VGASequencer()
    vga.chain4 = False
    vga.map_mask = 0x02
    mem.vga = vga
    mem.w8(0xA0005, 0x99)
    assert vga.planes[1][5] == 0x99
    assert mem.data[0xA0005] == 0            # linear byte untouched while planar
    mem.vga = None
    mem.w8(0xA0005, 0x33)
    assert mem.data[0xA0005] == 0x33         # chained: direct linear


def test_shld_shrd_and_bt():
    # mov eax, 0x80000001 ; mov edx, 0xF0000000 ; shld eax, edx, 4 -> 0x1F
    cpu, _ = run_blob(bytes.fromhex("B801000080 BA000000F0 0FA4D004".replace(" ", "")))
    assert cpu.r[EAX] == 0x1F
    # bt: mov ebx, 8 ; bt ebx, 3 -> CF=1
    cpu, _ = run_blob(bytes.fromhex("BB08000000 0FBAE303".replace(" ", "")))
    assert cpu.get_flag(CF)


def test_x87_masked_divide_by_zero_is_inf():
    # fninit ; fld1 ; fldz ; fdivp st(1) — the Watcom cstart infinity probe.
    cpu, _ = run_blob(bytes.fromhex("DBE3 D9E8 D9EE DEF9".replace(" ", "")))
    assert len(cpu.st) == 1
    assert cpu.st[-1] == float("inf")


def test_render_pm_frame_chained_and_planar():
    from dos_re.dos4gw import DOS4GWHost, render_pm_frame
    mem = FlatMemory(size=0xC0000)
    host = DOS4GWHost(mem, ".")
    host.dac[3:6] = bytes((0x3F, 0x00, 0x00))     # palette index 1 = bright red
    # Chained: linear byte at A0000 is pixel (0,0).
    mem.data[0xA0000] = 1
    rgb, w, h = render_pm_frame(host)
    assert (w, h) == (320, 200)
    assert rgb[0:3] == bytes((0xFC, 0x00, 0x00))
    # Unchained Mode X: pixel x=1 comes from plane 1 offset 0.
    host.vga.chain4 = False
    host.vga.planes[1][0] = 1
    rgb, _, _ = render_pm_frame(host)
    assert rgb[3:6] == bytes((0xFC, 0x00, 0x00))


def test_neg_sets_flags():
    # mov eax, 1 ; neg eax
    cpu, _ = run_blob(bytes.fromhex("B801000000 F7D8".replace(" ", "")))
    assert cpu.r[EAX] == 0xFFFFFFFF
    assert cpu.get_flag(CF) and cpu.get_flag(SF)
