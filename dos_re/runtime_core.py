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
    cpu.replacement_hooks[BIOS_INT9_ENTRY] = dos.bios_int9_keyboard
    cpu.hook_names[BIOS_INT9_ENTRY] = "bios_int9_keyboard"
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
# Dedicated power-on INT 09h (IRQ1) entry.  IVT[9] points here so a game that
# saves and chains to "the previous keyboard ISR" reaches the native BIOS
# keyboard handler installed at this address (create_runtime).  F000:E987 is the
# classic IBM BIOS INT 9 entry point.
BIOS_INT9_ENTRY = (0xF000, 0xE987)
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
