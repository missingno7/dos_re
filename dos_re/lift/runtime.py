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


class LiftStuck(LiftRuntimeError):
    """A lifted function is spinning and cannot make progress on its own.

    Distinct from "this loop is long": see :func:`stuck_error` for why that
    difference is the whole point of this class.
    """


#: How the tick/wait diagnosis is phrased.  Kept here (not in the emitted
#: modules) so improving the advice never means regenerating the corpus.
_ADVICE = """
WHY THIS HAPPENS: a lifted function runs SYNCHRONOUSLY to completion. The
interpreter interleaves the outside world between instructions -- timer IRQs
land, a port's value changes -- but inside a lifted body nothing external can
happen, so a loop that WAITS for the world to change waits forever. It is not
a bug in the lift; the loop is faithfully doing what the original does. What
is missing is a place for time to pass.

HOW TO FIX IT, in order of likelihood:

 1. The loop head is an environment wait that no one declared. Give the
    address below to `irgen --boundary-heads` (and `liftemit --boundary-heads`)
    and the emitter turns it into a boundary observer + RESUME_ENTRIES: the
    body PARKS there and the host resumes it next frame. This is the fix ~all
    of the time.

 2. The host is not delivering what the loop waits for. If the counter below
    is advanced by an ISR (a timer tick, a keyboard scancode), the driver must
    deliver that interrupt between frames -- through the game's OWN recovered
    ISR. A driver that never delivers IRQ0 makes every tick-wait unwinnable.

 3. It genuinely is a long loop and the guard is simply too low (a
    decompressor, a full-screen blit). Raise `liftemit --max-iterations`. Note
    the no-progress detector below does NOT fire on these: a loop whose
    registers advance is never reported as stuck.
"""


def stuck_error(name, cpu, bb, block_addrs, *, iterations, no_progress):
    """Build the diagnosis for a lifted function that will not terminate.

    The old message said only "exceeded MAX_ITERATIONS (unbounded internal
    loop -- likely an environment wait; hook it by hand)". That is a GUESS
    dressed as a finding, and it is the wrong thing to hand someone: it names
    no address, distinguishes a real spin from a merely slow decompressor not
    at all, and its advice ("hook it by hand") is usually not even the right
    fix -- declaring a boundary head is. Chasing that guess cost this project
    hours on two separate loops.

    So say what is actually known: WHERE it is spinning, whether it is
    provably making no progress, and what the machine looks like there.
    """
    cs, ip = (cpu.s.cs, block_addrs.get(bb)) if block_addrs else (cpu.s.cs, None)
    where = f"{cs:04X}:{ip:04X}" if ip is not None else f"block {bb}"
    s = cpu.s
    nl = chr(10)
    if no_progress:
        head = (f"{name}: STUCK at {where} -- no progress." + nl +
                f"  The dispatcher returned to the SAME block with IDENTICAL "
                f"registers after {no_progress:,} iterations, so this loop "
                f"cannot exit on its own: nothing it reads is changing.")
    else:
        head = (f"{name}: ran {iterations:,} iterations without returning "
                f"(guard limit), currently at {where}." + nl +
                f"  Registers WERE still changing, so this may be a genuinely "
                f"long loop rather than a spin -- see fix 3 below.")
    regs = (f"  state: ax={s.ax:04X} bx={s.bx:04X} cx={s.cx:04X} dx={s.dx:04X} "
            f"si={s.si:04X} di={s.di:04X} bp={s.bp:04X} "
            f"ds={s.ds:04X} es={s.es:04X} flags={s.flags:04X} "
            f"IF={'1' if s.flags & 0x200 else '0'}")
    if not (s.flags & 0x200):
        regs += (nl + "  NOTE: IF=0 -- interrupts are DISABLED here, so no ISR can "
                 "run even if the host delivers one. If this loop waits on a "
                 "value an ISR updates, that is the contradiction to explain.")
    return LiftStuck(head + nl + regs + nl + _ADVICE +
                     nl + f"  the address to declare: {where}" + nl)


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
    cpu.call_depth += 1
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
    cpu.call_depth += 1
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
