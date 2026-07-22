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
    # Handler at 0x4000: inc ebx ; iretd.  Main: sti ; nop x 40.
    # The IRQ source is polled every 16 instructions (the decimation the
    # measured hot path earned), so give delivery a full window.
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
    cpu, _ = run_blob(bytes.fromhex("FB" + "90" * 40), setup=setup)
    assert cpu.r[EBX] == 1                   # handler ran exactly once
    assert cpu.eip == CODE + 42              # resumed and hit the HLT


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


def test_vga_geometry_from_crtc():
    vga = VGASequencer()
    assert vga.geometry() == (320, 200)          # unprogrammed fallback
    # mode 13h: HDE 0x4F, VDE 399 (overflow bit1), doubled scanlines
    vga.crtc[0x01] = 0x4F
    vga.crtc[0x07] = 0x1F
    vga.crtc[0x09] = 0x41
    vga.crtc[0x12] = 0x8F
    assert vga.geometry() == (320, 200)
    # Mode X 320x400: scanline repeat 1
    vga.crtc[0x09] = 0x40
    assert vga.geometry() == (320, 400)
    # Mode X 320x240: VDE 479 (0x1DF), doubled
    vga.crtc[0x09] = 0x41
    vga.crtc[0x12] = 0xDF
    vga.crtc[0x07] = 0x1F                        # bit1 set -> VDE bit 8
    assert vga.geometry() == (320, 240)


def test_mouse_norm_maps_onto_game_range():
    from dos_re.dos4gw import DOS4GWHost
    host = DOS4GWHost(FlatMemory(size=0x1000), ".")
    # MS defaults: full window spans 0..639 / 0..199
    host.set_mouse_norm(0.5, 0.5)
    assert (host.mouse_x, host.mouse_y) == (319, 99)
    # The game narrows the box (a breakout pad row): mapping follows it
    host.mouse_range = [16, 623, 180, 180]
    host.set_mouse_norm(0.0, 0.9)
    assert (host.mouse_x, host.mouse_y) == (16, 180)
    host.set_mouse_norm(1.0, 0.1)
    assert (host.mouse_x, host.mouse_y) == (623, 180)
    host.set_mouse_norm(2.5, -3.0)          # clamped
    assert (host.mouse_x, host.mouse_y) == (623, 180)


def _string_state(seed, *, planar_dst=False, planar_src=False, wm=0, op=0xA5, count=37):
    import random
    rng = random.Random(seed)
    mem = FlatMemory(size=0x110000)
    cpu = CPU386(mem, eip=CODE, esp=STACK)
    vga = VGASequencer()
    vga.chain4 = False
    vga.write_mode = wm
    vga.map_mask = rng.randrange(1, 16)
    vga.read_map = rng.randrange(4)
    for pl in vga.planes:
        pl[:0x400] = bytes(rng.randrange(256) for _ in range(0x400))
    mem.vga = vga
    for a in range(0x20000, 0x20400):
        mem.data[a] = rng.randrange(256)
    cpu.r[1] = count                                   # ecx
    cpu.r[6] = 0xA0000 + 0x40 if planar_src else 0x20000   # esi
    cpu.r[7] = 0xA0000 + 0x200 if planar_dst else 0x20200  # edi
    cpu.r[0] = 0xDEADBEEF                              # eax (stos fill)
    blob = {0xA4: bytes((0xF3, 0xA4)), 0xA5: bytes((0xF3, 0xA5)),
            0xAA: bytes((0xF3, 0xAA)), 0xAB: bytes((0xF3, 0xAB))}[op]
    mem.load(CODE, blob + bytes((0xF4,)))
    return cpu, mem, vga


def _digest(cpu, mem, vga):
    import zlib
    return (tuple(cpu.r), cpu.eflags,
            zlib.crc32(bytes(mem.data)), zlib.crc32(b"".join(vga.planes)),
            tuple(vga.latches))


@pytest.mark.parametrize("kw", [
    dict(op=0xA5),                                     # RAM -> RAM movsd
    dict(op=0xA4, planar_dst=True),                    # RAM -> planes movsb
    dict(op=0xA5, planar_dst=True),                    # RAM -> planes movsd
    dict(op=0xA4, planar_src=True),                    # planes -> RAM
    dict(op=0xA4, planar_src=True, planar_dst=True, wm=1),  # wm1 block copy
    dict(op=0xAA, planar_dst=True),                    # stosb -> planes
    dict(op=0xAB),                                     # stosd -> RAM
    dict(op=0xAB, planar_dst=True, count=3),           # small: below any threshold
])
def test_bulk_string_equals_per_unit_loop(kw):
    fast_cpu, fast_mem, fast_vga = _string_state(7, **kw)
    slow_cpu, slow_mem, slow_vga = _string_state(7, **kw)
    slow_cpu._bulk_string = lambda *a: False           # force the per-unit loop
    for c in (fast_cpu, slow_cpu):
        try:
            c.run(10_000)
        except HaltExecution:
            pass
    assert _digest(fast_cpu, fast_mem, fast_vga) == _digest(slow_cpu, slow_mem, slow_vga)


@pytest.mark.parametrize("op, count", [(0xA4, 5), (0xA5, 3)])
def test_bulk_movs_falls_back_for_forward_overlapping_ram(op, count):
    """REP MOVS is ordered read-then-write, not a memmove snapshot copy."""
    fast_cpu, fast_mem, fast_vga = _string_state(7, op=op, count=count)
    slow_cpu, slow_mem, slow_vga = _string_state(7, op=op, count=count)
    for cpu in (fast_cpu, slow_cpu):
        cpu.r[7] = cpu.r[6] + 1
    slow_cpu._bulk_string = lambda *a: False
    for cpu in (fast_cpu, slow_cpu):
        try:
            cpu.run(10_000)
        except HaltExecution:
            pass
    assert _digest(fast_cpu, fast_mem, fast_vga) == _digest(slow_cpu, slow_mem, slow_vga)


@pytest.mark.parametrize("wm", [0, 1])
def test_bulk_movs_planar_overlap_preserves_latch_pipeline(wm):
    fast_cpu, fast_mem, fast_vga = _string_state(
        7, planar_src=True, planar_dst=True, wm=wm, op=0xA4, count=37)
    slow_cpu, slow_mem, slow_vga = _string_state(
        7, planar_src=True, planar_dst=True, wm=wm, op=0xA4, count=37)
    for cpu in (fast_cpu, slow_cpu):
        cpu.r[7] = cpu.r[6] + 1
    slow_cpu._bulk_string = lambda *a: False
    for cpu in (fast_cpu, slow_cpu):
        try:
            cpu.run(10_000)
        except HaltExecution:
            pass
    assert _digest(fast_cpu, fast_mem, fast_vga) == _digest(slow_cpu, slow_mem, slow_vga)


def test_render_numpy_matches_scalar():
    """The numpy render fast path must be byte-identical to the scalar loop."""
    import dos_re.dos4gw as m
    from dos_re.dos4gw import DOS4GWHost, render_pm_frame
    if m._np is None:
        import pytest
        pytest.skip("numpy not installed")
    mem = FlatMemory(size=0xC0000)
    host = DOS4GWHost(mem, ".")
    for i in range(256):
        host.dac[i * 3:i * 3 + 3] = bytes(((i * 7) & 0x3F, (i * 3) & 0x3F, i & 0x3F))
    # Mode X planar content
    host.vga.chain4 = False
    for p in range(4):
        host.vga.planes[p][:0x2000] = bytes((p * 40 + (j % 200)) & 0xFF for j in range(0x2000))
    host.vga.crtc[0x01] = 0x4F
    host.vga.crtc[0x12] = 0x8F
    host.vga.crtc[0x09] = 0x41
    host.vga.crtc[0x13] = 0x28
    np_rgb, w, h = render_pm_frame(host)
    np_mx = host.vga.render_mode_x(w, h)
    saved = m._np
    m._np = None
    try:
        sc_rgb, _, _ = render_pm_frame(host)
        sc_mx = host.vga.render_mode_x(w, h)
    finally:
        m._np = saved
    assert np_mx == sc_mx
    assert np_rgb == sc_rgb
