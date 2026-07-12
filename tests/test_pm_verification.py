"""PM hook dispatch + differential verifier tests (game-free by construction).

A synthesized 386 function is hooked with a Python replacement; the strict
auto-continuation verifier interprets the original ASM beside it and diffs
the full machine.  A correct hook passes; a subtly wrong one (off-by-one
result, stray memory write, wrong flags) must raise.
"""
from __future__ import annotations

import struct

import pytest

from dos_re.cpu386 import CPU386, FlatMemory, EAX
from dos_re.cpu import HaltExecution
from dos_re.dos4gw import DOS4GWHost
from dos_re.runtime import PMRuntime
from dos_re.pm_verification import (PMHookVerifyDivergence, install_pm_hook_verifier)

CODE = 0x1000
FUNC = 0x2000
DATA = 0x3000
STACK = 0x8000

# main: call FUNC ; call FUNC ; hlt
MAIN = bytes.fromhex("E8FB0F0000") + bytes.fromhex("E8F60F0000") + b"\xF4"
# FUNC: mov eax,[0x3000] ; add eax,5 ; mov [0x3004],eax ; ret
FUNC_CODE = bytes.fromhex("A100300000" "83C005" "A304300000" "C3".replace(" ", ""))


def make_rt() -> PMRuntime:
    mem = FlatMemory(size=0x10000 * 8)
    mem.load(CODE, MAIN)
    mem.load(FUNC, FUNC_CODE)
    mem.w32(DATA, 7)
    cpu = CPU386(mem, eip=CODE, esp=STACK)
    dos = DOS4GWHost(mem, ".")
    cpu.interrupt_handler = dos.interrupt
    cpu.idt = dos.pm_vectors
    dos._cpu = cpu
    return PMRuntime(image=None, cpu=cpu, dos=dos, mem=mem)


def run_to_halt(rt, max_steps=10_000):
    try:
        rt.cpu.run(max_steps)
    except HaltExecution:
        pass


def correct_hook(cpu):
    v = cpu.mem.r32(DATA) + 5
    cpu.set_reg(EAX, 4, v)
    cpu._flags_add(v - 5, 5, v, 32)        # the ADD's real flag effect
    cpu.mem.w32(DATA + 4, v)
    cpu.eip = cpu.pop(4)                   # RET


def test_hook_dispatch_without_verifier():
    rt = make_rt()
    calls = []

    def hook(cpu):
        calls.append(cpu.eip)
        correct_hook(cpu)
    rt.cpu.replacement_hooks[FUNC] = hook
    run_to_halt(rt)
    assert len(calls) == 2
    assert rt.mem.r32(DATA + 4) == 12


def test_verifier_passes_correct_hook():
    rt = make_rt()
    rt.cpu.replacement_hooks[FUNC] = correct_hook
    rt.cpu.hook_names[FUNC] = "func"
    verifier = install_pm_hook_verifier(rt)
    run_to_halt(rt)
    assert verifier.total_verified == 2
    assert rt.mem.r32(DATA + 4) == 12


@pytest.mark.parametrize("bad", [
    # wrong result value
    lambda cpu: (cpu.set_reg(EAX, 4, 13), cpu.mem.w32(DATA + 4, 13),
                 cpu._flags_add(8, 5, 13, 32),
                 setattr(cpu, "eip", cpu.pop(4))),
    # stray memory write outside the routine's contract
    lambda cpu: (correct_hook(cpu), cpu.mem.w8(DATA + 0x100, 0xAA)),
    # forgotten flag update (ADD sets flags; hook leaves stale ones)
    lambda cpu: (cpu.set_reg(EAX, 4, 12), cpu.mem.w32(DATA + 4, 12),
                 setattr(cpu, "eip", cpu.pop(4))),
])
def test_verifier_catches_wrong_hooks(bad):
    rt = make_rt()
    # Make pre-hook flags differ from the ADD's post-flags so the stale-flags
    # case is detectable: set CF+ZF before entry.
    rt.cpu.eflags |= 0x41
    rt.cpu.replacement_hooks[FUNC] = bad
    rt.cpu.hook_names[FUNC] = "bad"
    install_pm_hook_verifier(rt)
    with pytest.raises(PMHookVerifyDivergence):
        run_to_halt(rt)


def test_clone_is_independent():
    from dos_re.pm_snapshot import clone_pm_runtime
    rt = make_rt()
    rt.cpu.run(1)                          # step into the first call
    clone = clone_pm_runtime(rt)
    assert clone.cpu.eip == rt.cpu.eip
    clone.cpu.run(5)                       # advance only the clone
    clone.mem.w8(DATA, 99)
    assert rt.mem.data[DATA] == 7          # live untouched
    assert clone.cpu.instruction_count == rt.cpu.instruction_count + 5
