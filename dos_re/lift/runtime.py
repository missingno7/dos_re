"""Runtime support imported by generated (lifted) hooks.

Three primitives, all of which delegate to the interpreter so a lifted
function never needs its callees, its interrupt handlers, or the long tail of
rare opcodes lifted first (docs/lifting_design.md §1, §6):

* :func:`emulate_call` / :func:`emulate_far_call` — run a callee through the
  VM. Any replacement hook installed at the callee dispatches automatically,
  so lifting order never matters and lifted/hand-written hooks compose.
* :func:`emulate_int` — service a software interrupt exactly as the
  interpreter does, including the case where the handler redirects into a
  real in-memory ISR (then run to its IRET).
* :func:`interp_one` — execute ONE instruction at a pinned CS:IP through the
  interpreter. The emitter uses this for instructions it has no native form
  for yet, which keeps every lifted function byte-exact while the native
  opcode set grows. Only legal for non-transfer instructions (the emitter
  enforces this): the interpreter leaves IP after the instruction, and the
  generated dispatch loop tracks control flow itself.

All of them are fail-loud: a runaway callee/ISR raises rather than silently
truncating.
"""
from __future__ import annotations

from dos_re.cpu import CPU8086

#: default step ceiling for one emulated call/interrupt
MAX_NESTED_STEPS = 20_000_000


class LiftRuntimeError(RuntimeError):
    """A lifted hook's VM delegation did not terminate as expected."""


def interp_one(cpu: CPU8086, cs: int, ip: int) -> None:
    """Execute exactly one instruction at ``cs:ip`` through the interpreter.

    Leaves CS:IP after the instruction (the caller's dispatch loop owns
    control flow) and lets ``step()`` do its own instruction accounting.

    Any replacement hook at exactly ``cs:ip`` is suppressed for this one
    step: when a lifted function's ENTRY instruction is itself a fallback,
    ``step()`` would otherwise dispatch the lifted hook again — infinite
    recursion (found by the first Win16 lift, whose functions enter via
    ``enter``, a fallback op).
    """
    cpu.s.cs = cs & 0xFFFF
    cpu.s.ip = ip & 0xFFFF
    key = (cs & 0xFFFF, ip & 0xFFFF)
    hook = cpu.replacement_hooks.pop(key, None)
    try:
        cpu.step()
    finally:
        if hook is not None:
            cpu.replacement_hooks[key] = hook


def _run_until(cpu: CPU8086, done, max_steps: int, what: str) -> None:
    for _ in range(max_steps):
        if done():
            return
        cpu.step()
    raise LiftRuntimeError(f"{what} did not return within {max_steps:,} steps "
                           f"(at {cpu.s.cs:04X}:{cpu.s.ip:04X})")


def _returned(sp: int, sp_after_ret: int) -> bool:
    """Stack unwound AT or ABOVE the pre-call depth (wrap-safe half-range).

    ``ret n`` / ``retf n`` (pascal convention — every Win16 API and most
    Win16 game code) pops the arguments too, ending ABOVE the caller's
    pre-call SP, so exact equality would never fire and the emulated call
    would run away through the rest of the program. Being at the return
    CS:IP with the frame gone IS the return; a mid-body pass through the
    return address is still excluded because the callee's frame keeps SP
    BELOW the pre-call depth.
    """
    return ((sp - sp_after_ret) & 0xFFFF) < 0x8000


def emulate_call(cpu: CPU8086, cs: int, target: int, ret_ip: int,
                 max_steps: int = MAX_NESTED_STEPS) -> None:
    """NEAR call: push ``ret_ip``, run the callee through the VM, return.

    Terminates when the VM is back at ``cs:ret_ip`` with the stack unwound to
    its pre-call depth or above (``ret n`` cleans the args too; SP alone is
    not enough — a callee may legitimately pass through that SP mid-body).
    """
    s = cpu.s
    sp_after_ret = s.sp & 0xFFFF          # SP a plain RET restores
    cpu.push(ret_ip & 0xFFFF)
    s.cs, s.ip = cs & 0xFFFF, target & 0xFFFF

    def done() -> bool:
        return (s.cs & 0xFFFF) == (cs & 0xFFFF) and (s.ip & 0xFFFF) == (ret_ip & 0xFFFF) \
            and _returned(s.sp & 0xFFFF, sp_after_ret)

    _run_until(cpu, done, max_steps, f"emulated call to {cs:04X}:{target:04X}")


def emulate_far_call(cpu: CPU8086, seg: int, off: int, ret_cs: int, ret_ip: int,
                     max_steps: int = MAX_NESTED_STEPS) -> None:
    """FAR call: push ``ret_cs:ret_ip``, run the callee through the VM."""
    s = cpu.s
    sp_after_ret = s.sp & 0xFFFF
    cpu.push(ret_cs & 0xFFFF)
    cpu.push(ret_ip & 0xFFFF)
    s.cs, s.ip = seg & 0xFFFF, off & 0xFFFF

    def done() -> bool:
        return (s.cs & 0xFFFF) == (ret_cs & 0xFFFF) and (s.ip & 0xFFFF) == (ret_ip & 0xFFFF) \
            and _returned(s.sp & 0xFFFF, sp_after_ret)

    _run_until(cpu, done, max_steps, f"emulated far call to {seg:04X}:{off:04X}")


def emulate_int(cpu: CPU8086, num: int, cs: int, ret_ip: int,
                max_steps: int = MAX_NESTED_STEPS) -> None:
    """Service ``INT num`` exactly as the interpreter's INT path does.

    The interpreter services an INT by calling ``cpu.interrupt_handler``. A
    Python-serviced interrupt (most DOS/BIOS calls) returns with IP already at
    the next instruction. A handler that instead vectors into a real in-memory
    ISR leaves IP inside it; then run the VM until the ISR's IRET brings us
    back. Both cases end at ``cs:ret_ip``.
    """
    s = cpu.s
    if cpu.interrupt_handler is None:
        from dos_re.cpu import UnsupportedInstruction
        raise UnsupportedInstruction(f"INT {num:02X}h not hooked")
    s.cs, s.ip = cs & 0xFFFF, ret_ip & 0xFFFF   # IP already past the INT, as the interpreter has it
    sp_before = s.sp & 0xFFFF
    cpu.interrupt_handler(cpu, num & 0xFF)
    if (s.cs & 0xFFFF) == (cs & 0xFFFF) and (s.ip & 0xFFFF) == (ret_ip & 0xFFFF):
        return                                   # serviced in Python; nothing pushed

    def done() -> bool:
        return (s.cs & 0xFFFF) == (cs & 0xFFFF) and (s.ip & 0xFFFF) == (ret_ip & 0xFFFF) \
            and (s.sp & 0xFFFF) == sp_before

    _run_until(cpu, done, max_steps, f"emulated INT {num:02X}h")
