"""Platform-effect binding for CPUless recovered functions (M3 stage 2, tier 6).

A recovered CPUless function computes game behaviour over ``(mem, plat, *regs)``
and receives NO CPU object.  When it must reach the machine -- a port read/write,
later an interrupt -- it calls the abstract ``plat`` interface; the recovered
module imports nothing and stays CPU-carrier-free (docs/dos_re_2.0.md section 4:
DOS services and hardware become reusable platform adapters, not CPU state).

The generated CPU-ABI adapter binds ``plat`` to :class:`CpuPlatform`, which
drives the real machine through the interpreter's own port hooks.  Virtual time
is threaded exactly: the recovered body passes the instruction offset of each
effect (``cost`` = instructions executed before it), and the platform sets
``cpu.instruction_count`` accordingly so a time-dependent read (PIT, VGA
retrace latch) returns the same value the interpreter would.
"""
from __future__ import annotations


class CpuPlatform:
    """Bind the abstract ``plat`` effect interface to a live CPU/DOS machine.

    Constructed by a CPU-ABI adapter around the interpreter's ``port_reader`` /
    ``port_writer`` (and, later, interrupt handler).  ``entry`` is the CPU's
    ``instruction_count`` at function entry, so each effect's absolute virtual
    time is ``entry + cost``."""

    __slots__ = ("cpu", "entry")

    def __init__(self, cpu, entry: int):
        self.cpu = cpu
        self.entry = entry

    def inp(self, port: int, width: int, cost: int) -> int:
        cpu = self.cpu
        cpu.instruction_count = self.entry + cost
        bits = 16 if width == 2 else 8
        if cpu.port_reader is None:
            return 0
        return cpu.port_reader(cpu, port & 0xFFFF, bits) & (0xFFFF if width == 2 else 0xFF)

    def outp(self, port: int, value: int, width: int, cost: int) -> None:
        cpu = self.cpu
        cpu.instruction_count = self.entry + cost
        bits = 16 if width == 2 else 8
        if cpu.port_writer is not None:
            cpu.port_writer(cpu, port & 0xFFFF, value & (0xFFFF if width == 2 else 0xFF), bits)


def make_cpu_platform(cpu):
    """Adapter helper: a :class:`CpuPlatform` bound to ``cpu`` at its current
    ``instruction_count`` (the function-entry virtual time)."""
    return CpuPlatform(cpu, cpu.instruction_count)
