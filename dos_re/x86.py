"""Shared x86 constants + control exceptions -- the leaf both the interpreter
(``dos_re.cpu``) and the pure device model (``dos_re.dos``) depend on.

Extracting these here breaks the ``dos -> cpu`` import edge: the DOS/hardware
device model no longer imports the interpreter, so the standalone CPUless
platform runtime (``dos_re.lift.platform.CPUlessPlatformRuntime``) can own a
device model without pulling ``CPU8086`` into its import graph (dos_re_2.0.md
section 4; the play_cpuless import guard forbids the interpreter).
"""
from __future__ import annotations

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
