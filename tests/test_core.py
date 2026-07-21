from pathlib import Path

import pytest

from dos_re.cpu import CPU8086, CPUState, UnsupportedInstruction
from dos_re.dos import DOSMachine
from dos_re.memory import Memory


def run_bytes(code: bytes, steps: int = 10):
    mem = Memory()
    mem.load(0x1000, 0, code)
    cpu = CPU8086(mem, CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    cpu.run(steps)
    return cpu


def test_mov_add_ret():
    cpu = run_bytes(bytes.fromhex("b8 34 12 05 01 00 f4"), 3)
    assert cpu.s.ax == 0x1235


def test_x87_opcodes_grown_for_simant():
    import struct
    # FLD1; FLD1; FADDP; FSQRT -> sqrt(2)
    cpu = run_bytes(bytes.fromhex("d9e8 d9e8 dec1 d9fa"), 4)
    assert abs(cpu.s.fst[-1] - 2 ** 0.5) < 1e-9
    # FLD1; FCHS -> -1.0
    cpu = run_bytes(bytes.fromhex("d9e8 d9e0"), 2)
    assert cpu.s.fst[-1] == -1.0
    # FLDZ; FXAM; FNSTSW AX -> C3 (0x4000) set for zero
    cpu = run_bytes(bytes.fromhex("d9ee d9e5 dfe0"), 3)
    assert cpu.s.ax & 0x4700 == 0x4000
    # FLD1; FTST; FNSTSW AX -> all condition bits clear (1.0 > 0)
    cpu = run_bytes(bytes.fromhex("d9e8 d9e4 dfe0"), 3)
    assert cpu.s.ax & 0x4700 == 0
    # FXCH: FLD1; FLDZ; FXCH ST(1) -> top back to 1.0
    cpu = run_bytes(bytes.fromhex("d9e8 d9ee d9c9"), 3)
    assert cpu.s.fst[-1] == 1.0 and cpu.s.fst[-2] == 0.0
    # single-precision m32 round-trip: FLD dword[0x200]; FSTP dword[0x204]
    mem = Memory()
    mem.load(0x1000, 0, bytes.fromhex("d9 06 00 02 d9 1e 04 02"))
    mem.load(0x1000, 0x200, struct.pack("<f", 3.5))
    cpu = CPU8086(mem, CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    cpu.run(2)
    out = struct.unpack("<f", bytes(mem.rb(0x1000, 0x204 + i) for i in range(4)))[0]
    assert out == 3.5 and not cpu.s.fst


def test_pusha_popa_roundtrip():
    # PUSHA (60) saves AX,CX,DX,BX,SP,BP,SI,DI; scribble the regs; POPA (61)
    # restores them (SP's stack slot is discarded).  Seed via mov ax..di.
    cpu = run_bytes(bytes.fromhex(
        "b8 11 11"        # mov ax,1111
        "b9 22 22"        # mov cx,2222
        "ba 33 33"        # mov dx,3333
        "bb 44 44"        # mov bx,4444
        "bd 55 55"        # mov bp,5555
        "be 66 66"        # mov si,6666
        "bf 77 77"        # mov di,7777
        "60"              # pusha
        "b8 00 00 b9 00 00 ba 00 00 bb 00 00 bd 00 00 be 00 00 bf 00 00"  # zero regs
        "61"              # popa
        "f4"), 17)
    assert (cpu.s.ax, cpu.s.cx, cpu.s.dx, cpu.s.bx) == (0x1111, 0x2222, 0x3333, 0x4444)
    assert (cpu.s.bp, cpu.s.si, cpu.s.di) == (0x5555, 0x6666, 0x7777)
    assert cpu.s.sp == 0xFFFE            # balanced


def test_memory_operand_decoded_once():
    cpu = run_bytes(bytes.fromhex("c7 06 00 01 34 12 81 06 00 01 01 00 f4"), 3)
    assert cpu.mem.rw(0x1000, 0x0100) == 0x1235
    assert cpu.s.ip == 0x000D


def test_hook_verify_range_diff_keeps_exact_mismatch_report():
    from dos_re.verification import HookVerifier, MemoryRange

    asm = bytearray(b"\x00" * 64)
    hook = bytearray(asm)
    rng = MemoryRange("probe", 8, 32)

    assert HookVerifier._range_diff(asm, hook, rng) is None

    hook[12] = 0x34
    hook[30] = 0x56
    report = HookVerifier._range_diff(asm, hook, rng)
    assert report is not None
    assert "differing bytes: 2" in report
    assert "first diff: 0000C asm=00 hook=34" in report


def test_hook_verify_defaults_to_full_memory_image():
    from types import SimpleNamespace
    from dos_re.verification import HookVerifier, HookVerifierConfig

    mem = Memory()
    hv = HookVerifier.__new__(HookVerifier)
    hv.config = HookVerifierConfig()
    rt = SimpleNamespace(
        program=SimpleNamespace(memory=mem),
        cpu=SimpleNamespace(s=CPUState(cs=0x1010, ds=0x2000, es=0x2000, ss=0x2000)),
    )

    ranges = hv._memory_ranges(rt)

    assert len(ranges) == 1
    assert ranges[0].name == "full memory"
    assert ranges[0].start == 0
    assert ranges[0].size == len(mem.data)


def test_rep_movsb_backward():
    mem = Memory()
    mem.load(0x1000, 0, bytes([1, 2, 3, 4]))
    code = bytes.fromhex("fd b9 04 00 be 03 00 bf 13 00 f3 a4 f4")
    mem.load(0x2000, 0, code)
    cpu = CPU8086(mem, CPUState(cs=0x2000, ds=0x1000, es=0x1000, ss=0x2000, sp=0xFFFE))
    cpu.run(6)
    assert mem.block(0x1000, 0x10, 4) == bytes([1, 2, 3, 4])


def test_outsb_and_rep_outsb_advance_si_and_write_ports():
    mem = Memory()
    mem.load(0x1000, 0, bytes([0x12, 0x34, 0x56]))
    code = bytes.fromhex("ba c8 03 6e b9 02 00 f3 6e f4")
    mem.load(0x2000, 0, code)
    log = []
    cpu = CPU8086(mem, CPUState(cs=0x2000, ds=0x1000, es=0x1000, ss=0x2000, sp=0xFFFE))
    cpu.port_writer = lambda _cpu, port, value, bits: log.append((port, value, bits))
    cpu.run(5)
    assert log == [(0x03C8, 0x12, 8), (0x03C8, 0x34, 8), (0x03C8, 0x56, 8)]
    assert cpu.s.si == 3




def test_386_operand_size_prefix_is_ignored_for_pre2_probe_low_word():
    cpu = run_bytes(bytes.fromhex("b8 34 12 66 33 c0 f4"), 3)
    assert cpu.s.ax == 0
    assert cpu.halted


def test_vga_dac_palette_roundtrip_for_pre2_probe():
    mem = Memory()
    cpu = CPU8086(mem, CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    dos = DOSMachine(root=Path('.'))

    dos.port_write(cpu, 0x03C8, 5, 8)
    dos.port_write(cpu, 0x03C9, 0x12, 8)
    dos.port_write(cpu, 0x03C9, 0x23, 8)
    dos.port_write(cpu, 0x03C9, 0x34, 8)
    dos.port_write(cpu, 0x03C7, 5, 8)

    assert dos.port_read(cpu, 0x03C9, 8) == 0x12
    assert dos.port_read(cpu, 0x03C9, 8) == 0x23
    assert dos.port_read(cpu, 0x03C9, 8) == 0x34


def test_ega_latch_rotate_or_write_mode_for_pre2_vga_probe():
    mem = Memory()
    cpu = CPU8086(mem, CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    dos = DOSMachine(root=Path('.'))
    dos.video_mode = 0x0D

    dos.port_write(cpu, 0x03C4, 0x0102, 16)  # sequencer map-mask: plane 0 only
    mem.wb(0xA000, 0x2000, 0x11)
    dos.port_write(cpu, 0x03CE, 0x0004, 16)  # graphics-controller read plane 0
    assert mem.rb(0xA000, 0x2000) == 0x11  # loads all four VGA latches

    dos.port_write(cpu, 0x03CE, 0x1103, 16)  # rotate right 1, logical OR with latch
    mem.wb(0xA000, 0x2000, 0xA0)

    assert mem.rb(0xA000, 0x2000) == 0x51


def test_ega_write_mode_1_copies_latches_to_destination_planes():
    from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE

    mem = Memory()
    cpu = CPU8086(mem, CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    dos = DOSMachine(root=Path('.'))
    dos.video_mode = 0x0D

    dos.port_write(cpu, 0x03C4, 0x0F02, 16)  # sequencer map-mask: all planes
    source = 0x1234
    dest = 0x2345
    for plane, value in enumerate((0x11, 0x22, 0x44, 0x88)):
        mem.data[EGA_APERTURE + EGA_PLANE_STRIDE * plane + source] = value
        mem.data[EGA_APERTURE + EGA_PLANE_STRIDE * plane + dest] = 0x00

    dos.port_write(cpu, 0x03CE, 0x0004, 16)  # read plane 0; read loads all latches
    assert mem.rb(0xA000, source) == 0x11
    dos.port_write(cpu, 0x03CE, 0x0105, 16)  # graphics-controller write mode 1
    mem.wb(0xA000, dest, 0xFF)               # CPU byte is ignored in write mode 1

    for plane, value in enumerate((0x11, 0x22, 0x44, 0x88)):
        assert mem.data[EGA_APERTURE + EGA_PLANE_STRIDE * plane + dest] == value


def test_ega_write_mode_1_respects_map_mask():
    from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE

    mem = Memory()
    cpu = CPU8086(mem, CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    dos = DOSMachine(root=Path('.'))
    dos.video_mode = 0x0D

    source = 0x0100
    dest = 0x0200
    for plane, value in enumerate((0xA1, 0xB2, 0xC3, 0xD4)):
        mem.data[EGA_APERTURE + EGA_PLANE_STRIDE * plane + source] = value
        mem.data[EGA_APERTURE + EGA_PLANE_STRIDE * plane + dest] = 0xEE

    dos.port_write(cpu, 0x03CE, 0x0004, 16)
    mem.rb(0xA000, source)
    dos.port_write(cpu, 0x03CE, 0x0105, 16)
    dos.port_write(cpu, 0x03C4, 0x0A02, 16)  # planes 1 and 3 only
    mem.wb(0xA000, dest, 0x00)

    for plane, value in enumerate((0xEE, 0xB2, 0xEE, 0xD4)):
        assert mem.data[EGA_APERTURE + EGA_PLANE_STRIDE * plane + dest] == value



def test_int33_mouse_driver_reports_present_and_tracks_position():
    """Once opted IN, INT 33h reports the mouse PRESENT on reset (AX=0 -> AX=FFFF)
    and returns the front-end-fed position/buttons via AX=0003, mapped through the
    program's own coordinate range (AX=7/8).  A mouse-driven game (VGA Lemmings)
    only enables pointer control when detection succeeds -- so its front-end must
    set ``mouse_present`` (the interactive viewer does so unconditionally; replay
    takes the recording's explicit ReplayArtifact ``mouse_present`` metadata).

    The mouse is opt-in rather than ambient because detecting one changes a game's
    startup control flow, which would silently diverge any recording made without
    it; see test_real_mode_mouse.py for the default-absent contract."""
    from dos_re.cpu import CPU8086, CPUState
    cpu = CPU8086(Memory(), CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    dos = DOSMachine(root=Path('.'), mouse_present=True)

    cpu.s.ax = 0x0000
    dos.int33(cpu)
    assert cpu.s.ax == 0xFFFF and cpu.s.bx == 2       # present, 2 buttons

    # Game narrows its coordinate box, then the front-end feeds a centred mouse.
    cpu.s.ax, cpu.s.cx, cpu.s.dx = 0x0007, 8, 328; dos.int33(cpu)   # horiz range
    cpu.s.ax, cpu.s.cx, cpu.s.dx = 0x0008, 8, 200; dos.int33(cpu)   # vert range
    dos.set_mouse_norm(0.5, 0.5, buttons=0x01)

    cpu.s.ax = 0x0003
    dos.int33(cpu)
    assert cpu.s.bx == 0x01                            # left button held
    assert 8 <= cpu.s.cx <= 328 and cpu.s.cx == 8 + int(0.5 * (328 - 8))
    assert 8 <= cpu.s.dx <= 200 and cpu.s.dx == 8 + int(0.5 * (200 - 8))


def test_unhandled_int_dispatches_to_installed_ivt_handler():
    """A soft interrupt the framework does not emulate, but for which the program
    installed its own IVT handler (VGA Lemmings' sound driver on INT 60h/61h),
    must dispatch to that handler like a real `int` — push flags/cs/ip, clear
    IF, jump to the vector — not fail loud."""
    from dos_re.cpu import CPU8086, CPUState, IF
    mem = Memory()
    # Install a handler for INT 60h at 3000:0100 via the IVT.
    mem.ww(0, 0x60 * 4, 0x0100)
    mem.ww(0, 0x60 * 4 + 2, 0x3000)
    cpu = CPU8086(mem, CPUState(cs=0x1000, ip=0x0050, ss=0x2000, sp=0xFFFE, flags=0x0202))
    dos = DOSMachine(root=Path('.'))
    dos.interrupt(cpu, 0x60)
    # Jumped to the handler, with the return frame pushed and IF cleared.
    assert (cpu.s.cs, cpu.s.ip) == (0x3000, 0x0100)
    assert not cpu.get_flag(IF)
    assert cpu.s.sp == 0xFFF8  # flags + cs + ip pushed (3 words)
    assert mem.rw(0x2000, 0xFFF8) == 0x0050  # return ip (top of stack)
    assert mem.rw(0x2000, 0xFFFA) == 0x1000  # return cs
    assert mem.rw(0x2000, 0xFFFC) == 0x0202  # saved flags
    assert cpu.call_depth == 1


def test_indirect_calls_update_continuation_call_depth():
    from dos_re.cpu import CPU8086, CPUState

    # call word [0010h] -> 0104h; ret
    near_mem = Memory()
    near_mem.load(0x1000, 0, bytes.fromhex("FF161000"))
    near_mem.ww(0x1000, 0x0010, 0x0104)
    near = CPU8086(
        near_mem,
        CPUState(cs=0x1000, ip=0, ds=0x1000, ss=0x2000, sp=0x1000),
    )
    near.step()
    assert near.call_depth == 1

    # call far [0010h] -> 3000:0104h
    far_mem = Memory()
    far_mem.load(0x1000, 0, bytes.fromhex("FF1E1000"))
    far_mem.ww(0x1000, 0x0010, 0x0104)
    far_mem.ww(0x1000, 0x0012, 0x3000)
    far = CPU8086(
        far_mem,
        CPUState(cs=0x1000, ip=0, ds=0x1000, ss=0x2000, sp=0x1000),
    )
    far.step()
    assert far.call_depth == 1


def test_unhandled_int_with_null_vector_still_fails_loud():
    from dos_re.cpu import CPU8086, CPUState
    cpu = CPU8086(Memory(), CPUState(cs=0x1000, ip=0x0050, ss=0x2000, sp=0xFFFE))
    dos = DOSMachine(root=Path('.'))
    with pytest.raises(UnsupportedInstruction):
        dos.interrupt(cpu, 0x63)  # vector 0000:0000 -> genuinely unhandled


def test_vga_status_display_enable_bit0_toggles_so_scanline_loops_progress():
    """The 0x3DA input-status register must toggle bit 0 (Display Enable), not
    only the vertical-retrace bit.  VGA Lemmings times a delay by polling bit 0
    for ~60 scan lines before loading the level palette; if bit 0 never sets, the
    game hangs on a black screen.  A finite poll must observe bit 0 both set and
    clear (so wait-for-set and wait-for-clear both terminate)."""
    from dos_re.cpu import CPU8086, CPUState
    cpu = CPU8086(Memory(), CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    dos = DOSMachine(root=Path('.'))
    reads = [dos.port_read(cpu, 0x03DA, 8) for _ in range(8)]
    assert any(r & 0x01 for r in reads), "display-enable bit 0 never set -> scanline loop hangs"
    assert any(not (r & 0x01) for r in reads), "display-enable bit 0 never clear"
    # The vertical-retrace bit (3) still toggles too (PRE2's vsync waits).
    assert any(r & 0x08 for r in reads) and any(not (r & 0x08) for r in reads)


def test_ega_set_reset_writes_per_plane_colour_through_bit_mask():
    """Write mode 0 with Enable Set/Reset: each enabled plane's source byte is
    the Set/Reset constant (expanded), and the Bit Mask selects which bits take
    the write vs stay latched.  This is how VGA Lemmings draws its 16-colour
    graphics; without it a colour write lands identically in every plane."""
    from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE

    mem = Memory()
    cpu = CPU8086(mem, CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    dos = DOSMachine(root=Path('.'))
    dos.video_mode = 0x10
    off = 0x1000

    dos.port_write(cpu, 0x03C4, 0x0F02, 16)          # map mask: all planes writable
    # Background = colour 15 in every pixel (all planes 0xFF), then read to latch it.
    for plane in range(4):
        mem.data[EGA_APERTURE + EGA_PLANE_STRIDE * plane + off] = 0xFF
    assert mem.rb(0xA000, off) == 0xFF               # loads latches = [FF,FF,FF,FF]

    dos.port_write(cpu, 0x03CE, 0x0F01, 16)          # enable set/reset: all planes
    dos.port_write(cpu, 0x03CE, 0x0500, 16)          # set/reset = colour 5 (planes 0,2)
    dos.port_write(cpu, 0x03CE, 0xF008, 16)          # bit mask = 0xF0 (top 4 pixels only)
    mem.wb(0xA000, off, 0x00)                         # CPU data ignored under set/reset

    # Top nibble takes colour 5 (planes 0,2 set); bottom nibble stays colour 15.
    assert mem.data[EGA_APERTURE + EGA_PLANE_STRIDE * 0 + off] == 0xFF  # (F0 sr) | (0F latch)
    assert mem.data[EGA_APERTURE + EGA_PLANE_STRIDE * 1 + off] == 0x0F  # (00 sr) | (0F latch)
    assert mem.data[EGA_APERTURE + EGA_PLANE_STRIDE * 2 + off] == 0xFF
    assert mem.data[EGA_APERTURE + EGA_PLANE_STRIDE * 3 + off] == 0x0F


def test_ega_bit_mask_preserves_latched_bits_on_cpu_data_write():
    """With Enable Set/Reset off, the source is the CPU byte, but the Bit Mask
    still preserves the unmasked bits from the latch (the read-modify-write
    pixel primitive)."""
    from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE

    mem = Memory()
    cpu = CPU8086(mem, CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    dos = DOSMachine(root=Path('.'))
    dos.video_mode = 0x10
    off = 0x1000

    dos.port_write(cpu, 0x03C4, 0x0102, 16)          # map mask: plane 0 only
    mem.data[EGA_APERTURE + off] = 0xAA              # latch background pattern
    assert mem.rb(0xA000, off) == 0xAA               # load latches

    dos.port_write(cpu, 0x03CE, 0x0008, 16)          # bit mask = 0x00 -> keep all latch bits
    mem.wb(0xA000, off, 0xFF)
    assert mem.data[EGA_APERTURE + off] == 0xAA      # fully preserved

    dos.port_write(cpu, 0x03CE, 0x0F08, 16)          # bit mask = 0x0F -> low nibble from CPU
    mem.rb(0xA000, off)                              # reload latch (0xAA)
    mem.wb(0xA000, off, 0xFF)
    assert mem.data[EGA_APERTURE + off] == 0xAF      # high nibble latched, low nibble = CPU


def test_cmpsw_compares_ds_si_with_es_di_and_advances():
    mem = Memory()
    mem.ww(0x1000, 0x0100, 0x1234)
    mem.ww(0x2000, 0x0200, 0x1234)
    code = bytes.fromhex("be 00 01 bf 00 02 a7 f4")
    mem.load(0x3000, 0, code)
    cpu = CPU8086(mem, CPUState(cs=0x3000, ds=0x1000, es=0x2000, ss=0x3000, sp=0xFFFE))
    cpu.run(4)
    assert cpu.s.si == 0x0102
    assert cpu.s.di == 0x0202
    assert cpu.get_flag(0x0040)

def test_int1a_set_ticks_rebases_subsequent_reads():
    # AH=01h sets the BIOS time-of-day counter from CX:DX; AH=00h reads must
    # continue from it (VGA Lemmings writes the counter when leaving a level).
    cpu = CPU8086(Memory(), CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    dos = DOSMachine(root=Path('.'))
    cpu.s.ax = 0x0100
    cpu.s.cx = 0x0012
    cpu.s.dx = 0x3456
    dos.interrupt(cpu, 0x1A)
    cpu.s.ax = 0x0000
    dos.interrupt(cpu, 0x1A)          # AH=00h read (the getter pre-increments)
    ticks = ((cpu.s.cx & 0xFFFF) << 16) | (cpu.s.dx & 0xFFFF)
    assert ticks == 0x00123457


def test_dos_version_returns_al_major_ah_minor():
    from dos_re.cpu import CF
    cpu = CPU8086(Memory(), CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    dos = DOSMachine(root=Path('.'))
    cpu.s.ax = 0x3000
    dos.interrupt(cpu, 0x21)
    assert cpu.s.ax == 0x0005
    assert not cpu.get_flag(CF)




def test_int2f_xms_probe_reports_driver_absent():
    cpu = CPU8086(Memory(), CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    dos = DOSMachine(root=Path('.'))
    cpu.s.ax = 0x4300
    dos.interrupt(cpu, 0x2F)
    assert cpu.s.ax == 0x4300

def test_ega_crtc_display_start_tracks_indexed_port_writes():
    mem = Memory()
    cpu = CPU8086(mem, CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    dos = DOSMachine(root=Path('.'))

    dos.port_write(cpu, 0x03D4, 0x120C, 16)
    dos.port_write(cpu, 0x03D4, 0x340D, 16)
    assert mem.ega_display_start == 0x1234

    dos.port_write(cpu, 0x03D4, 0x0C, 8)
    dos.port_write(cpu, 0x03D5, 0x20, 8)
    assert mem.ega_display_start == 0x2034


def test_int67_ems_probe_reports_driver_absent():
    from dos_re.cpu import CPU8086, CPUState
    from dos_re.dos import DOSMachine
    from dos_re.memory import Memory

    cpu = CPU8086(Memory(), CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    dos = DOSMachine(root=Path('.'))
    cpu.s.ax = 0x4000  # EMS get status
    dos.interrupt(cpu, 0x67)
    assert (cpu.s.ax >> 8) == 0x80


def test_80186_push_immediate_words():
    cpu = run_bytes(bytes.fromhex("68 34 12 6a ff 58 5b f4"), 5)
    assert cpu.s.ax == 0xFFFF
    assert cpu.s.bx == 0x1234


def test_80186_shift_immediate_group2():
    cpu = run_bytes(bytes.fromhex("b0 81 c0 e8 01 bb 00 81 c1 eb 04 f4"), 5)
    assert cpu.s.ax & 0xFF == 0x40
    assert cpu.s.bx == 0x0810


def test_shift_count_zero_preserves_flags():
    cpu = run_bytes(bytes.fromhex("f9 b0 81 c0 e8 20 f4"), 4)
    assert cpu.s.ax & 0xFF == 0x81
    assert cpu.get_flag(0x0001)


def test_rotate_does_not_touch_zero_sign_parity_flags():
    cpu = run_bytes(bytes.fromhex("b0 80 0a c0 d0 d0 f4"), 5)
    # OR AL,AL set SF and clears ZF; RCL AL,1 may change CF but must leave SF/ZF/PF alone.
    assert cpu.get_flag(0x0080)
    assert not cpu.get_flag(0x0040)



def test_segment_override_applies_to_string_source():
    mem = Memory()
    mem.wb(0x1000, 0x0100, 0x11)
    mem.wb(0x2000, 0x0100, 0x22)
    # ES: MOVSB copies from ES:SI to ES:DI. The destination segment is still ES;
    # only the string source segment is overridden.
    mem.load(0x3000, 0, bytes.fromhex("be 00 01 bf 00 02 26 a4 f4"))
    cpu = CPU8086(mem, CPUState(cs=0x3000, ds=0x1000, es=0x2000, ss=0x3000, sp=0xFFFE))
    cpu.run(4)
    assert mem.rb(0x2000, 0x0200) == 0x22
    assert cpu.s.si == 0x0101
    assert cpu.s.di == 0x0201


def _planes_any_nonzero(mem):
    from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE, EGA_PLANE_WINDOW
    return [any(mem.data[EGA_APERTURE + EGA_PLANE_STRIDE * p:
                         EGA_APERTURE + EGA_PLANE_STRIDE * p + EGA_PLANE_WINDOW])
            for p in range(4)]


def test_mode_set_clears_planar_shadow_planes():
    """A BIOS Set Video Mode to a planar EGA mode (0Dh) clears the four shadow
    planes — where planar pixels actually live — not just the 0A000h aperture.

    Regression: clearing only 0A000h was a no-op for planar pixels, so the previous
    screen survived a mode transition (menu->map scrolled the old image in instead
    of black). See dos.DOSMachine._clear_graphics_vram_for_mode.
    """
    from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE

    mem = Memory()
    cpu = CPU8086(mem, CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    dos = DOSMachine(root=Path('.'))

    # a stale "previous screen" in every shadow plane
    for plane in range(4):
        base = EGA_APERTURE + EGA_PLANE_STRIDE * plane
        mem.data[base:base + 0x4000] = b"\xAB" * 0x4000
    assert _planes_any_nonzero(mem) == [True, True, True, True]

    cpu.s.ax = 0x000D                 # AH=00 Set Video Mode, AL=0Dh (planar, clear)
    dos.int10(cpu)
    assert _planes_any_nonzero(mem) == [False, False, False, False]


def test_mode_set_no_clear_bit_preserves_planar_planes():
    """AL bit 7 ('do not clear') must leave the shadow planes intact."""
    from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE

    mem = Memory()
    cpu = CPU8086(mem, CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    dos = DOSMachine(root=Path('.'))
    for plane in range(4):
        base = EGA_APERTURE + EGA_PLANE_STRIDE * plane
        mem.data[base:base + 0x100] = b"\xAB" * 0x100

    cpu.s.ax = 0x008D                 # AL=0Dh | 80h => no clear
    dos.int10(cpu)
    assert _planes_any_nonzero(mem) == [True, True, True, True]


def test_leave_restores_frame():
    # push bp; mov bp,sp; sub sp,8; leave; hlt  — the MSC Win16 epilogue shape.
    cpu = run_bytes(bytes.fromhex("55 89 e5 83 ec 08 c9 f4"), 5)
    assert cpu.s.sp == 0xFFFE
    assert cpu.s.bp == 0x0000


def test_cwd_sign_extends_ax_into_dx():
    # mov ax,8000h; cwd; mov ax,7FFFh; cwd; hlt
    cpu = run_bytes(bytes.fromhex("b8 00 80 99 f4"), 2)
    assert cpu.s.dx == 0xFFFF
    cpu = run_bytes(bytes.fromhex("b8 ff 7f 99 f4"), 2)
    assert cpu.s.dx == 0x0000


def test_imul_three_operand_imm8_and_imm16():
    # mov ax,0100h; imul bx,ax,3; imul cx,ax,-2; hlt   (0x6B imm8 forms)
    cpu = run_bytes(bytes.fromhex("b8 00 01 6b d8 03 6b c8 fe f4"), 4)
    assert cpu.s.bx == 0x0300
    assert cpu.s.cx == 0xFE00          # -512
    # imul dx,ax,0200h (0x69 imm16): 0x100*0x200 = 0x20000 overflows -> CF/OF
    cpu = run_bytes(bytes.fromhex("b8 00 01 69 d0 00 02 f4"), 3)
    assert cpu.s.dx == 0x0000
    assert cpu.s.flags & 0x0001 and cpu.s.flags & 0x0800  # CF and OF set


def test_wait_is_noop_without_fpu():
    # wait; mov ax,1234h; hlt
    cpu = run_bytes(bytes.fromhex("9b b8 34 12 f4"), 3)
    assert cpu.s.ax == 0x1234


def test_x87_integer_multiply_chain():
    # The MSC inline-8087 shape: fild [0x100]; fild [0x104]; fmulp; fistp [0x108]
    # with the control word set to truncate (the __ftol pattern).
    code = bytes.fromhex(
        "9b db 06 00 01"        # fild dword [0x0100]   (6)
        "9b db 06 04 01"        # fild dword [0x0104]   (7)
        "9b de c9"              # fmulp st(1),st
        "9b d9 3e 10 01"        # fstcw [0x0110]
        "c7 06 12 01 ff 0f"     # mov word [0x0112], 0x0FFF  (RC=11 truncate)
        "9b d9 2e 12 01"        # fldcw [0x0112]
        "9b df 3e 08 01"        # fistp qword [0x0108]
        "f4"                    # hlt
    )
    mem = Memory()
    mem.load(0x1000, 0, code)
    mem.ww(0x1000, 0x0100, 6); mem.ww(0x1000, 0x0102, 0)
    mem.ww(0x1000, 0x0104, 7); mem.ww(0x1000, 0x0106, 0)
    cpu = CPU8086(mem, CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    cpu.run(40)
    result = int.from_bytes(bytes(mem.rb(0x1000, 0x0108 + i) for i in range(8)), "little")
    assert result == 42
    assert cpu.s.fst == []          # stack fully popped


def test_x87_integer_memory_arithmetic():
    # DA-group (m32int) and DE-group (m16int) arithmetic straight from memory,
    # the shape SimAnt's caste-allocation click uses: fild; fiadd dword; fidiv
    # dword; then a DE m16int fisub, each fistp'd back.
    code = bytes.fromhex(
        "9b db 06 00 01"        # fild  dword [0x0100]  (=10)
        "9b da 06 04 01"        # fiadd dword [0x0104]  (+5 -> 15)   DA /0
        "9b da 36 08 01"        # fidiv dword [0x0108]  (/3 ->  5)   DA /6
        "9b db 1e 20 01"        # fistp dword [0x0120]
        "9b db 06 0c 01"        # fild  dword [0x010c]  (=20)
        "9b de 26 10 01"        # fisub word  [0x0110]  (-8 -> 12)   DE /4
        "9b db 1e 24 01"        # fistp dword [0x0124]
        "f4")
    mem = Memory()
    mem.load(0x1000, 0, code)
    for off, v in ((0x0100, 10), (0x0104, 5), (0x0108, 3), (0x010c, 20)):
        mem.ww(0x1000, off, v); mem.ww(0x1000, off + 2, 0)
    mem.ww(0x1000, 0x0110, 8)                    # m16int operand for FISUB
    cpu = CPU8086(mem, CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    cpu.run(40)
    assert int.from_bytes(bytes(mem.rb(0x1000, 0x0120 + i) for i in range(4)), "little") == 5
    assert int.from_bytes(bytes(mem.rb(0x1000, 0x0124 + i) for i in range(4)), "little") == 12
    assert cpu.s.fst == []


def test_x87_compare_and_status_word():
    # fild [0x100]=3; fild [0x104]=5 ; fcomp st(1) -> ST0(5) > ST1(3): C0=0,C3=0
    code = bytes.fromhex(
        "9b db 06 00 01"
        "9b db 06 04 01"
        "9b d8 d9"              # fcomp st(1)
        "9b dd 3e 20 01"        # fnstsw [0x0120]
        "f4")
    mem = Memory()
    mem.load(0x1000, 0, code)
    mem.ww(0x1000, 0x0100, 3); mem.ww(0x1000, 0x0102, 0)
    mem.ww(0x1000, 0x0104, 5); mem.ww(0x1000, 0x0106, 0)
    cpu = CPU8086(mem, CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    cpu.run(20)
    sw = mem.rw(0x1000, 0x0120)
    assert sw & 0x4500 == 0         # not equal, not less, not unordered


def test_x87_f80_roundtrip():
    # fild -> fstp tbyte -> fld tbyte -> fistp preserves small integers.
    code = bytes.fromhex(
        "9b db 06 00 01"        # fild dword [0x0100]
        "9b db 3e 30 01"        # fstp tbyte [0x0130]
        "9b db 2e 30 01"        # fld tbyte [0x0130]
        "9b df 3e 40 01"        # fistp qword [0x0140]
        "f4")
    mem = Memory()
    mem.load(0x1000, 0, code)
    mem.ww(0x1000, 0x0100, 12345); mem.ww(0x1000, 0x0102, 0)
    cpu = CPU8086(mem, CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    cpu.run(20)
    result = int.from_bytes(bytes(mem.rb(0x1000, 0x0140 + i) for i in range(8)), "little")
    assert result == 12345


def test_enter_leave_frame_nesting0():
    # enter 8,0 ; leave ; hlt  — the 80186 frame prologue/epilogue pair.
    cpu = run_bytes(bytes.fromhex("c8 08 00 00 c9 f4"), 3)
    # enter: push bp(0), bp=sp, sp-=8 ; leave: sp=bp, pop bp -> both restored
    assert cpu.s.sp == 0xFFFE and cpu.s.bp == 0x0000


def test_selector_translation_lifts_1mb_ceiling():
    # A 2MB memory with a selector map: selector 0x1000 -> linear 0x150000
    # (past the 1MB real-mode ceiling), 0x2000 -> 0x180000.  Unmapped
    # selectors fall back to real-mode seg<<4.
    mem = Memory(size=0x200000, sel_base={0x1000: 0x150000, 0x2000: 0x180000})
    mem.ww(0x1000, 0x0010, 0xBEEF)
    assert mem.data[0x150010] == 0xEF and mem.data[0x150011] == 0xBE
    assert mem.rw(0x1000, 0x0010) == 0xBEEF
    mem.wb(0x2000, 0x0000, 0x42)
    assert mem.rb(0x2000, 0x0000) == 0x42
    # unmapped selector -> real-mode seg<<4 (low memory)
    mem.wb(0x0100, 0x0004, 0x99)
    assert mem.data[0x1004] == 0x99 and mem.rb(0x0100, 0x0004) == 0x99
    # load()/block() honour the selector map too
    mem.load(0x1000, 0x0100, b"\x01\x02\x03")
    assert mem.block(0x1000, 0x0100, 3) == b"\x01\x02\x03"
    assert bytes(mem.data[0x150100:0x150103]) == b"\x01\x02\x03"
    # RPL bits are ignored for descriptor resolution: an alias selector (same
    # index + TI, different RPL) hits the same mapping.  Win16 huge-pointer
    # arithmetic flips RPL bits, so 0x1002 (0x1000 with RPL 2) must resolve to
    # the same linear block as 0x1000 — not fall through to real-mode.
    mem.ww(0x1002, 0x0020, 0x1234)
    assert mem.rw(0x1000, 0x0020) == 0x1234 and mem.data[0x150020] == 0x34
    assert mem._xlat(0x1003, 0x0020) == mem._xlat(0x1000, 0x0020)


def test_cmc_toggles_carry():
    from dos_re.cpu import CF
    # STC; CMC -> CF cleared; CMC -> CF set again.
    cpu = run_bytes(bytes.fromhex("f9 f5 f5 f4"), 3)
    assert cpu.get_flag(CF) is True
    cpu = run_bytes(bytes.fromhex("f9 f5 f4"), 2)         # STC then CMC
    assert cpu.get_flag(CF) is False


def test_int10_read_display_combination_reports_vga():
    # AH=1Ah is the standard VGA-presence probe (AEPROG.EXE keys mode 13h on it):
    # AL=1Ah function supported, BL=08h colour analog VGA, BH=00h no alternate.
    from dos_re.cpu import CF
    cpu = CPU8086(Memory(), CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    dos = DOSMachine(root=Path('.'))
    cpu.s.ax = 0x1A00
    dos.interrupt(cpu, 0x10)
    assert cpu.s.ax & 0xFF == 0x1A
    assert cpu.s.bx & 0xFF == 0x08
    assert not cpu.get_flag(CF)


def test_int15_system_config_unsupported_on_8086():
    # AH=C0h (get system configuration) on a PC/XT-class machine: CF set,
    # AH=86h "function not supported" -- detection code reads this as "old PC".
    from dos_re.cpu import CF
    cpu = CPU8086(Memory(), CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    dos = DOSMachine(root=Path('.'))
    cpu.s.ax = 0xC000
    dos.interrupt(cpu, 0x15)
    assert (cpu.s.ax >> 8) & 0xFF == 0x86
    assert cpu.get_flag(CF)


def test_int21_ioctl_get_device_info_file_and_std_handles():
    # AX=4400h: DX bit 7 clear = block-device file (drive bits 2 = C:),
    # std handles answer as character devices, unknown handle = CF + error 6.
    from dos_re.cpu import CF
    from dos_re.dos import FileHandle
    cpu = CPU8086(Memory(), CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    dos = DOSMachine(root=Path('.'))
    dos.files[5] = FileHandle(Path('X.DAT'), bytearray(b'x'))
    cpu.s.ax, cpu.s.bx = 0x4400, 5
    dos.interrupt(cpu, 0x21)
    assert cpu.s.dx & 0x80 == 0
    assert not cpu.get_flag(CF)
    cpu.s.ax, cpu.s.bx = 0x4400, 1
    dos.interrupt(cpu, 0x21)
    assert cpu.s.dx & 0x80
    cpu.s.ax, cpu.s.bx = 0x4400, 42
    dos.interrupt(cpu, 0x21)
    assert cpu.get_flag(CF) and cpu.s.ax == 6


def test_file_handles_reuse_lowest_free_slot(tmp_path):
    # Real DOS returns the lowest free handle (>=5) and reuses closed ones.  A
    # monotonic counter instead lets handles climb without bound; a game that
    # indexes a fixed per-handle table by the handle value then overruns it
    # (Ancient Empires' DS:38CC handle table into its DS:3904 colour table).
    from dos_re.cpu import CF
    (tmp_path / "A.DAT").write_bytes(b"aaaa")
    (tmp_path / "B.DAT").write_bytes(b"bbbb")
    cpu = CPU8086(Memory(), CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    dos = DOSMachine(root=tmp_path)

    def _open(name: str) -> int:
        addr = 0x200
        for i, ch in enumerate(name.encode() + b"\x00"):
            cpu.mem.wb(cpu.s.ds, addr + i, ch)
        cpu.s.ax, cpu.s.dx = 0x3D00, addr
        dos.interrupt(cpu, 0x21)
        assert not cpu.get_flag(CF)
        return cpu.s.ax

    h1 = _open("A.DAT")
    h2 = _open("B.DAT")
    assert (h1, h2) == (5, 6)          # first two files
    cpu.s.bx = h1                       # close the first
    cpu.s.ax = 0x3E00
    dos.interrupt(cpu, 0x21)
    h3 = _open("A.DAT")
    assert h3 == 5                     # handle 5 reused, not 7


def test_trace_defaults_off_and_run_does_not_grow_trace():
    """Tracing must default OFF: an undrained run loop with it ON grows
    cpu.trace one string per instruction -> gigabytes (the pilot's runaway-RAM
    bug). run() must leave trace empty by default."""
    mem = Memory()
    # tight self-loop: jmp $  (EB FE) -> many instructions, zero real work
    mem.load(0x1000, 0, bytes.fromhex("EBFE"))
    cpu = CPU8086(mem, CPUState(cs=0x1000, ds=0x1000, es=0x1000, ss=0x1000, sp=0xFFFE))
    assert cpu.trace_enabled is False
    cpu.run(5000)
    assert cpu.trace == []            # nothing accumulated
    assert cpu.instruction_count >= 5000
