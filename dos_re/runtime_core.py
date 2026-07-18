"""EXE-free runtime core — the Runtime shell + the data-only image constructor.

This module holds everything needed to build and hold a real-mode runtime that
does NOT require an executable: the :class:`Runtime` container, the power-on
BIOS environment, and :func:`create_runtime_from_image`, which materializes a
runtime from a raw memory image + register state.

It deliberately does NOT import ``load_mz_program`` / ``parse_mz`` / ``load_le``.
The EXE loader (:func:`dos_re.runtime.create_runtime`) lives in
:mod:`dos_re.runtime` and imports FROM here — so the strict-VMless import graph
(``scripts/lint_vmless_independence.py``) reaches this core but never the loader
that parses the binary (dos_re/docs/dos_re_2.0.md §"The EXE-independence wall").
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .cpu import CPU8086, CPUState
from .dos import DOSMachine
from .memory import LoadedProgram, Memory
from .hooks import registry


@dataclass
class Runtime:
    program: LoadedProgram
    cpu: CPU8086
    dos: DOSMachine


#: The dummy-IRET stub as a hook entry.  The byte at _BIOS_IRET_STUB is an IRET
#: that the INTERPRETER can simply execute -- but a strict-VMless runtime may
#: not interpret ANY x86, and a game that chains an IRQ vector to "the previous
#: handler" (the universal idiom) jumps straight here on every single IRQ.  So
#: the power-on environment needs a NATIVE form of it too, or the EXE-free
#: runtime cannot service a chained interrupt at all.
BIOS_IRET_ENTRY = (0xF000, 0xFF53)


def bios_iret_stub(cpu) -> None:
    """The power-on BIOS dummy IRQ handler (F000:FF53): a bare IRET.

    Native equivalent of the single 0xCF byte ``_init_bios_environment`` writes
    there -- same one instruction, same cost, so an interpreted and a VMless
    run keep the same instruction timeline.
    """
    s = cpu.s
    s.ip = cpu.pop()
    s.cs = cpu.pop()
    s.flags = cpu.pop() | 0x0002


def install_bios_environment_hooks(cpu, dos) -> None:
    """Give the power-on BIOS handlers their NATIVE form.

    THE RULE: every ROM-BIOS entry a game can vector to must exist both as the
    byte the interpreter executes AND as a native hook -- because a game is
    free to reach it from either kind of runtime, and the two must model the
    same machine.

    Not a VMless-only concern, which is exactly how this drifted: the EXE path
    and the EXE-free path each wired these by hand, INT 09h got both, and the
    IRET stub got only the EXE-free one.  The symptom was invisible until a
    snapshot-resumed run armed the wall -- a snapshot is restored through
    ``create_runtime`` (the EXE path), so it inherited the interpreted-only
    stub and failed the wall on the first chained timer IRQ, at F000:FF53,
    with nothing in the game to blame.  One function, both callers, no drift.
    """
    cpu.replacement_hooks[BIOS_INT9_ENTRY] = dos.bios_int9_keyboard
    cpu.hook_names[BIOS_INT9_ENTRY] = "bios_int9_keyboard"
    cpu.replacement_hooks[BIOS_IRET_ENTRY] = bios_iret_stub
    cpu.hook_names[BIOS_IRET_ENTRY] = "bios_iret_stub"


def use_real_console_input(rt) -> None:
    """Make blocking DOS console reads wait for a real key instead of Esc.

    DOSMachine defaults ``console_input_fallback`` to 0x011B (Esc) so a bare
    ``cpu.run()`` with no driver loop cannot hang on a blocking read. Any driver
    with a frame loop catches ``ConsoleInputWouldBlock`` and does not need it --
    and for a game that reads menu keys via INT 21h AH=07h (SkyRoads does) the
    synthesis is actively harmful: it receives a phantom Esc, reads it as
    "quit", and calls exit(0), presenting as the program quitting itself
    seconds after the menu appears with no keypress.

    Lives HERE, not in player.py, because a strict-VMless runner needs it and
    must not import the player (which reaches the loader). That is exactly how
    the bug it prevents got into scripts/play_vmless.py: the helper existed but
    was unreachable from behind the wall, so the one path that needed it most
    was the one path that could not call it.
    """
    rt.dos.console_input_fallback = None

def enable_sound_blaster(rt: Runtime, *, base: int = 0x220, irq: int = 7, dma: int = 1,
                         detection_only: bool = False):
    """Attach an emulated Sound Blaster + PIC so the program detects and uses it.

    Opt-in (an interactive front-end calls this); the deterministic demo/test path
    leaves the hardware absent so its timing is unchanged.  The front-end decides
    *how* to deliver IRQs: at batch boundaries (``pic.acknowledge`` + a forced
    ``deliver_interrupt``) to avoid interrupting the game mid-render, or inline via
    ``rt.cpu.pending_irq`` for tight detection loops.

    ``detection_only`` attaches a *detection stub* (see :class:`SoundBlaster`): the
    program detects a digital device and emits its audio commands, but no PCM is
    streamed and no playback IRQs fire — for front-ends that produce the audio with
    their own (e.g. recovered/native) engine and only need the command stream.
    """
    from .pic import PIC8259
    from .sblaster import SoundBlaster

    pic = PIC8259(imr=0x00)  # nothing masked; only IRQ0/IRQ7 are ever raised here
    sb = SoundBlaster(
        base=base, irq=irq, dma=dma,
        raise_irq=pic.raise_irq,
        read_mem=lambda a: rt.cpu.mem.data[a & 0xFFFFF],
        detection_only=detection_only,
    )
    rt.dos.pic = pic
    rt.dos.sound_blaster = sb
    # Resuming a snapshot taken mid-playback: restore the DSP/DMA programming and
    # re-arm a block IRQ so the driver's refill ISR fires and streaming continues.
    # (The PIC is left fresh — imr=0x00 is the proven cold-boot state and the game
    # re-syncs its mask via port 0x21 at runtime.)
    saved = getattr(rt.dos, "sound_blaster_snapshot", None)
    if saved:
        sb.restore_state(saved)
        sb.rearm_after_restore()
        rt.dos.sound_blaster_snapshot = None
    return sb


def create_runtime_from_image(
    memory_image: bytes | bytearray,
    state: CPUState,
    *,
    game_root: str | Path,
    psp_segment: int = 0,
    program_meta: dict | None = None,
) -> Runtime:
    """Build a :class:`Runtime` from a raw memory image — with NO executable file.

    The EXE-free analogue of :func:`dos_re.runtime.create_runtime`.  Where
    ``create_runtime`` calls ``load_mz_program`` to parse and load the original
    binary, this takes an already-materialized 1 MB memory image (a *generated,
    data-only boot image*) and the CPU register state, and wires up exactly the
    same CPU + DOS + BIOS environment around them.  Nothing here reads or parses
    an executable; ``program.exe`` is ``None`` by construction.

    This is the load path for the strict-VMless runtime, which must be
    *physically* independent of the original binary.  The image is assumed to
    already contain a booted machine state (PSP, IVT, BIOS data area,
    decompressed program), so this does NOT re-seed the PSP/BIOS the way a cold
    EXE load would — restoring those from the image is the caller's job (see
    ``dos_re.snapshot_headless.load_snapshot_headless``).
    """
    mem = Memory()
    if len(memory_image) != len(mem.data):
        raise ValueError(
            f"boot image is {len(memory_image)} bytes; expected {len(mem.data)} "
            "(a full real-mode memory image)")
    mem.data[:] = memory_image
    cpu = CPU8086(mem, state)
    root = Path(game_root)
    dos = DOSMachine(root)
    cpu.interrupt_handler = dos.interrupt
    cpu.port_reader = dos.port_read
    cpu.port_writer = dos.port_write
    install_bios_environment_hooks(cpu, dos)
    registry.install(cpu)
    program = LoadedProgram(
        exe=None,
        memory=mem,
        psp_segment=psp_segment,
        load_segment=(program_meta or {}).get("load_segment", (psp_segment + 0x10) & 0xFFFF),
        entry_cs=(program_meta or {}).get("entry_cs", state.cs),
        entry_ip=(program_meta or {}).get("entry_ip", state.ip),
        initial_ss=(program_meta or {}).get("initial_ss", state.ss),
        initial_sp=(program_meta or {}).get("initial_sp", state.sp),
        overlay=b"",
    )
    return Runtime(program, cpu, dos)


# A real BIOS leaves the machine in a known state before a program runs: the
# hardware-IRQ interrupt vectors point at an IRET stub, and the BIOS data area
# holds the video config.  Programs rely on both (e.g. chaining the previous IRQ
# vector, or reading the CRTC base port at 0040:0063).  None of this is
# program-specific — it is the power-on environment any DOS binary expects.
_BIOS_IRET_STUB = 0xFFF53  # F000:FF53, the conventional BIOS dummy IRET
# The power-on INT 09h (IRQ1) entry IVT[9] points at (create_runtime installs the
# native BIOS keyboard handler there, so a game that chains to "the previous
# keyboard ISR" reaches it).  Defined in the CPU-FREE dos_re.keyboard leaf and
# re-exported here: CPU-free front-ends need it to test whether a game installed
# its own INT 09h, and must not import this CPU-carrying module to get it.
from .keyboard import BIOS_INT9_ENTRY  # noqa: E402  (re-export)
_BIOS_INT9_LINEAR = 0xFE987


def _init_bios_environment(memory) -> None:
    data = memory.data
    data[_BIOS_IRET_STUB] = 0xCF  # IRET (written directly; F000 is ROM-protected via wb/ww)
    data[_BIOS_INT9_LINEAR] = 0xCF  # IRET fallback if executed without the native handler
    seg, off = 0xF000, 0xFF53
    for vec in (*range(0x08, 0x10), *range(0x70, 0x78)):  # IRQ0-7 (INT 08-0F), IRQ8-15 (INT 70-77)
        base = vec * 4
        if data[base:base + 4] == b"\x00\x00\x00\x00":
            if vec == 0x09:  # keyboard IRQ1 -> the native BIOS keyboard handler
                kb_seg, kb_off = BIOS_INT9_ENTRY
                data[base], data[base + 1] = kb_off & 0xFF, (kb_off >> 8) & 0xFF
                data[base + 2], data[base + 3] = kb_seg & 0xFF, (kb_seg >> 8) & 0xFF
                continue
            data[base], data[base + 1] = off & 0xFF, (off >> 8) & 0xFF
            data[base + 2], data[base + 3] = seg & 0xFF, (seg >> 8) & 0xFF
    # BIOS data area: CRTC base port (color) — read by retrace-wait code via
    # flat 0463h.  Kept minimal; the game manages the rest of its video state.
    data[0x463], data[0x464] = 0xD4, 0x03   # 0040:0063 = 03D4h
