"""Runtime support imported by generated 32-bit (CPU386) lifted hooks.

The flat protected-mode counterpart of :mod:`.runtime` -- same three
delegation primitives, same fail-loud contract:

* :func:`emulate_call32` -- run a callee through the VM (any hook installed at
  the callee dispatches automatically, so lifting order never matters).
* :func:`emulate_int32` -- service a software interrupt exactly as the
  interpreter's INT path does (the DOS/4GW host services in Python and
  returns; a vectored in-memory ISR runs to its IRET).
* :func:`interp_one32` -- execute ONE instruction at a pinned EIP through the
  interpreter (the emitter's fallback for instructions without native forms;
  non-transfer only).

Plus :func:`check_signature` -- the self-modifying-code tripwire every
generated hook runs at entry.
"""
from __future__ import annotations

MAX_NESTED_STEPS = 20_000_000


class LiftRuntimeError(RuntimeError):
    """A lifted hook's VM delegation did not terminate as expected."""


def check_signature(cpu, entry: int, signature: bytes, name: str) -> None:
    if bytes(cpu.mem.data[entry:entry + len(signature)]) != signature:
        raise LiftRuntimeError(
            f"{name}: entry bytes at 0x{entry:X} no longer match the lifted "
            f"signature (self-modifying code or wrong image)")


def interp_one32(cpu, ip: int) -> None:
    """Execute exactly one instruction at flat ``ip`` through the interpreter.

    A replacement hook at exactly ``ip`` is suppressed for this one step --
    when a lifted function's own entry instruction is a fallback, ``step()``
    would otherwise recurse into the hook forever.
    """
    cpu.eip = ip & 0xFFFFFFFF
    hook = cpu.replacement_hooks.pop(ip & 0xFFFFFFFF, None)
    try:
        cpu.step()
    finally:
        if hook is not None:
            cpu.replacement_hooks[ip & 0xFFFFFFFF] = hook


def _run_until(cpu, done, max_steps: int, what: str) -> None:
    for _ in range(max_steps):
        if done():
            return
        cpu.step()
    raise LiftRuntimeError(f"{what} did not return within {max_steps:,} steps "
                           f"(at eip=0x{cpu.eip:X})")


def _returned(esp: int, esp_after_ret: int) -> bool:
    """Stack unwound AT or ABOVE the pre-call depth (wrap-safe half-range).

    ``ret n`` pops arguments too (Watcom's register convention still uses it
    for stack-args functions), ending ABOVE the caller's pre-call ESP; a
    mid-body pass through the return EIP keeps ESP below it."""
    return ((esp - esp_after_ret) & 0xFFFFFFFF) < 0x80000000


def call_linked32(cpu, target: int, default_callee, ret_ip: int) -> None:
    """Direct lifted->lifted NEAR call: no interpreter in the path.

    The flat-EIP mirror of ``dos_re.hooks.call_installed_hook_like_near_call``:
    original CALL/RET stack semantics (push ``ret_ip``, the callee's emitted
    ``ret`` pops it into EIP), an installed replacement hook at ``target``
    takes precedence over the LINKS default (a better implementation wins),
    and when live hook verification is active the child routes through the
    verifier at its real EIP boundary -- a bad lifted child cannot hide inside
    a larger verified parent.  Only all-near-RET-exit callees are safe to link
    (the link pass enforces this); tail-jump / IRET callees stay
    ``emulate_call32``.
    """
    handler = cpu.replacement_hooks.get(target, default_callee)
    name = cpu.hook_names.get(target, getattr(handler, "__name__", "linked"))
    cpu.push(ret_ip & 0xFFFFFFFF, 4)
    cpu.eip = target & 0xFFFFFFFF
    # Virtual-time parity with the emulate_call32 path: step() counts a
    # dispatched hook as one instruction; a linked call bypasses step(), so
    # count the dispatch here or the instruction-count clock (Sound Blaster
    # pacing, replay identity) drifts between linked and unlinked graphs.
    cpu.instruction_count += 1
    verifier = getattr(cpu, "hook_verifier", None)
    if (verifier is not None
            and getattr(cpu, "hook_verifier_verify_nested_calls", True)
            and target not in getattr(cpu, "hook_verifier_passthrough", set())):
        verifier(cpu, target, handler, name)
    else:
        handler(cpu)
    if cpu.eip != (ret_ip & 0xFFFFFFFF):
        raise LiftRuntimeError(
            f"linked call to 0x{target:X} did not return to 0x{ret_ip:X} "
            f"(eip=0x{cpu.eip:X}) -- callee is not a pure near-ret function")


def emulate_call32(cpu, target: int, ret_ip: int,
                   max_steps: int = MAX_NESTED_STEPS) -> None:
    """NEAR call in the flat model: push ``ret_ip``, run the callee."""
    esp_after_ret = cpu.r[4]
    cpu.push(ret_ip & 0xFFFFFFFF, 4)
    cpu.eip = target & 0xFFFFFFFF

    def done() -> bool:
        return cpu.eip == (ret_ip & 0xFFFFFFFF) and _returned(cpu.r[4], esp_after_ret)

    _run_until(cpu, done, max_steps, f"emulated call to 0x{target:X}")


def emulate_int32(cpu, num: int, ret_ip: int,
                  max_steps: int = MAX_NESTED_STEPS) -> None:
    """Service ``INT num`` exactly as the interpreter's INT path does."""
    if cpu.interrupt_handler is None:
        from dos_re.cpu import UnsupportedInstruction
        raise UnsupportedInstruction(f"INT {num:02X}h not hooked")
    cpu.eip = ret_ip & 0xFFFFFFFF     # EIP already past the INT, as step() has it
    esp_before = cpu.r[4]
    cpu.interrupt_handler(cpu, num & 0xFF)
    if cpu.eip == (ret_ip & 0xFFFFFFFF):
        return                        # serviced in Python; nothing pushed

    def done() -> bool:
        return cpu.eip == (ret_ip & 0xFFFFFFFF) and cpu.r[4] == esp_before

    _run_until(cpu, done, max_steps, f"emulated INT {num:02X}h")
