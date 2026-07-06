"""TEMPLATE — replacement hooks: thin VM adapters over pure recovered rules.

A hook is a minimal boundary adapter, never where logic accumulates.  A good
hook only: (1) reads the relevant state from original memory/registers,
(2) calls a clean recovered function that knows nothing about the CPU,
(3) writes the result back, (4) reproduces the exact return mechanics:

    near routine:    cpu.s.ip = cpu.pop()
    far routine:     cpu.s.ip = cpu.pop(); cpu.s.cs = cpu.pop()
    internal block:  cpu.s.ip = <exact continuation IP>

Every hook needs oracle evidence (a trace of what the original really did) and
a HookStop entry in verification.py.  Never add a hook because it looks right.
"""
from __future__ import annotations

from dos_re.cpu import CPU8086
from dos_re.hooks import registry

# TODO: derive your game's code segment from the loaded program, never hard-code
# it blindly (the MZ loader's segment layout is deterministic per framework).
CODE_SEG = 0x1010  # placeholder


# TODO: replace this sample with your first recovered routine.
# @registry.replace(CODE_SEG, 0x1234, "my_first_recovered_routine")
def _sample_hook(cpu: CPU8086) -> None:
    # 1. read inputs from VM state
    value = cpu.s.ax & 0xFFFF
    # 2. call the pure recovered rule (lives in recovered/, unit-tested)
    result = _pure_rule_placeholder(value)
    # 3. write outputs back
    cpu.s.ax = result & 0xFFFF
    # 4. exact return mechanics (near RET here)
    cpu.s.ip = cpu.mem.rw(cpu.s.ss, cpu.s.sp)
    cpu.s.sp = (cpu.s.sp + 2) & 0xFFFF


def _pure_rule_placeholder(value: int) -> int:
    # TODO: move real rules into recovered/ — pure, VM-free, unit-testable.
    return value
