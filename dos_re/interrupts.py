"""Synchronous delivery of hardware interrupts to the interpreted game.

The interpreter has no asynchronous interrupt mechanism, but a DOS game can install
real ISRs (timer on INT 08h, keyboard on INT 09h) and drives input through them.
At a safe instruction boundary a front-end can ask the runtime to *deliver* an
interrupt exactly the way hardware would: push FLAGS/CS/IP, clear IF/TF, jump to
the vector from the IVT, and run the interpreter until the matching ``iret``
returns to the original instruction.

This keeps the game as the oracle -- the game's own ISR runs and updates its own
state (e.g. the keyboard scan-code table) -- instead of guessing that state.
"""
from __future__ import annotations

from .cpu import IF, TF
from .observable import HARDWARE_INTERRUPT
from .runtime import BIOS_INT9_ENTRY, Runtime


def read_vector(rt: Runtime, num: int) -> tuple[int, int]:
    """Return (segment, offset) of interrupt vector ``num`` from the real IVT."""
    mem = rt.cpu.mem
    off = mem.rw(0, (num * 4) & 0xFFFFF)
    seg = mem.rw(0, (num * 4 + 2) & 0xFFFFF)
    return seg, off


def deliver_interrupt(rt: Runtime, num: int, *, max_steps: int = 200_000) -> bool:
    """Invoke the installed handler for interrupt ``num`` and run it to its iret.

    Returns False (a no-op) if no handler is installed.  Must be called at an
    instruction boundary, i.e. between ``rt.cpu.run(...)`` batches, never from
    inside a step.
    """
    cpu = rt.cpu
    seg, off = read_vector(rt, num)
    if seg == 0 and off == 0:
        return False

    sink = getattr(rt.dos, "observable_effect_sink", None)
    if sink is not None:
        # Delivery is the observable timing seam.  Do not include CS:IP or
        # guest instruction count: a semantic backend must deliver the same
        # interrupt at the same declared yield, not mimic assembler progress.
        sink.record(HARDWARE_INTERRUPT, num & 0xFF)

    ret_cs, ret_ip = cpu.s.cs & 0xFFFF, cpu.s.ip & 0xFFFF
    sp0 = cpu.s.sp & 0xFFFF
    # Hardware interrupt entry sequence.
    cpu.push(cpu.s.flags)
    cpu.push(ret_cs)
    cpu.push(ret_ip)
    cpu.call_depth += 1
    cpu.set_flag(IF, False)
    cpu.set_flag(TF, False)
    cpu.s.cs, cpu.s.ip = seg & 0xFFFF, off & 0xFFFF

    steps = 0
    while not (cpu.s.sp == sp0 and cpu.addr() == (ret_cs, ret_ip)):
        cpu.step()
        steps += 1
        if steps > max_steps:
            raise RuntimeError(f"INT {num:02X}h handler did not return (cs:ip={cpu.s.cs:04X}:{cpu.s.ip:04X})")
    return True


def deliver_scancode(rt: Runtime, scancode: int, *, max_steps: int = 200_000) -> bool:
    """Present a raw keyboard scan code on port 60h and run the INT 9 handler.

    ``scancode`` is an XT make code (e.g. 0x1C Enter, 0x48 Up) for a press, or the
    make code OR 0x80 for a release.  The game's own ISR translates it into its
    key-state table, so no game-side key semantics are reimplemented here.

    Also updates the BIOS-visible type-ahead buffer directly (``DOSMachine
    .note_bios_keystroke``) when needed -- on real hardware a single physical
    keypress is seen by both the game's own INT 9 handler AND (if it chains,
    which is the common pattern) the BIOS ISR that fills the type-ahead
    buffer INT 16h/INT 21h AH=0Bh read from. Emulating only the game's own
    key-state table left "press any key" prompts that poll the BIOS buffer
    directly unable to ever see input, even after the game's own menus
    clearly responded to the same key delivered this way (found in SkyRoads:
    level-select navigation worked, but its post-selection "press any key"
    screen never advanced).

    If IVT[9] still points at the stock BIOS handler (``BIOS_INT9_ENTRY`` --
    the game never installed its own, e.g. SkyRoads), ``deliver_interrupt``
    below invokes ``note_bios_keystroke`` itself as part of running that
    handler, so calling it again here first would double-queue every
    keystroke (found the hard way: a handful of keypresses left FOUR-plus
    duplicate entries in the type-ahead buffer, confusing menu code that
    reads one entry per logical press). Only call it directly when some
    *other*, presumably-custom handler is installed, matching the "may or
    may not chain" case the docstring above describes.
    """
    rt.dos.current_scancode = scancode & 0xFF
    rt.dos.kbd_output_buffer_full = True
    if read_vector(rt, 0x09) != BIOS_INT9_ENTRY:
        rt.dos.note_bios_keystroke(scancode)
    return deliver_interrupt(rt, 0x09, max_steps=max_steps)
