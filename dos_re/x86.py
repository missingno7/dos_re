"""Shared x86 constants + control exceptions -- the leaf both the interpreter
(``dos_re.cpu``) and the pure device model (``dos_re.dos``) depend on.

Extracting these here breaks the ``dos -> cpu`` import edge: the DOS/hardware
device model no longer imports the interpreter, so the standalone CPUless
platform runtime (``dos_re.lift.platform.CPUlessPlatformRuntime``) can own a
device model without pulling ``CPU8086`` into its import graph (dos_re_2.0.md
section 4; the play_cpuless import guard forbids the interpreter).
"""
from __future__ import annotations

from dataclasses import dataclass, field

# FLAGS register bits.
CF = 0x0001
PF = 0x0004
AF = 0x0010
ZF = 0x0040
SF = 0x0080
TF = 0x0100
IF = 0x0200
DF = 0x0400
OF = 0x0800

#: Even-parity lookup for the low byte (PF set when the number of 1 bits is even).
PARITY = [bin(i).count("1") % 2 == 0 for i in range(256)]


class UnsupportedInstruction(NotImplementedError):
    pass


class HaltExecution(Exception):
    pass


@dataclass(slots=True)
class CPUState:
    """The REGISTER FILE -- ISA data, not the interpreter.

    Lives here for the same reason the flag bits and control exceptions do: a
    register record is a value, and needing one must not drag CPU8086 into the
    import graph.  The CPUless runner holds its register file in a boundary
    park and serializes it to a snapshot; before this move it imported the
    whole interpreter to name the dataclass, which is precisely the edge the
    standalone contract forbids (measured 2026-07-17).  ``dos_re.cpu``
    re-exports it, so every existing importer is unaffected.
    """
    # slots=True: register fields are read/written on virtually every emulated
    # instruction, and slotted attribute access is measurably faster than
    # __dict__ lookup.  Consequence: no ad-hoc attributes, and clones use
    # dataclasses.replace() instead of CPUState(**s.__dict__) (verification.py
    # and snapshot cloning were the only such sites in the ecosystem).
    ax: int = 0
    bx: int = 0
    cx: int = 0
    dx: int = 0
    sp: int = 0
    bp: int = 0
    si: int = 0
    di: int = 0
    cs: int = 0
    ds: int = 0
    es: int = 0
    ss: int = 0
    ip: int = 0
    flags: int = 0x0202
    # x87 state (added for Win16 inline-8087 code; doubles stand in for the
    # 80-bit registers -- the documented precision caveat lives in execute_fpu).
    fst: list = field(default_factory=list)     # ST(0) is fst[-1]
    fsw: int = 0
    fcw: int = 0x037F

    def snapshot(self) -> str:
        return (
            f"AX={self.ax:04X} BX={self.bx:04X} CX={self.cx:04X} DX={self.dx:04X} "
            f"SI={self.si:04X} DI={self.di:04X} BP={self.bp:04X} SP={self.sp:04X} "
            f"CS:IP={self.cs:04X}:{self.ip:04X} DS={self.ds:04X} ES={self.es:04X} SS={self.ss:04X} "
            f"FLAGS={self.flags:04X}"
        )
