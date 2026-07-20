from __future__ import annotations

from typing import Callable

from .cpu import CPU8086


Hook = Callable[[CPU8086], None]


def call_installed_hook_like_near_call(
    cpu: CPU8086,
    key: tuple[int, int],
    default_handler: Hook,
    return_ip: int,
) -> None:
    """Run an installed child hook with original near-CALL stack semantics.

    Source-port parent hooks often compose child routines directly instead of
    letting the VM execute an actual CALL instruction.  This helper preserves the
    CALL/RET stack effect and, when live hook verification is active, routes the
    child through the verifier at its real CS:IP boundary.  Without this, a bad
    lifted child can hide inside a larger verified parent and surface only as a
    later frame/state divergence.
    """
    handler = cpu.replacement_hooks.get(key, default_handler)
    name = cpu.hook_names.get(key, getattr(handler, "__name__", "replacement"))
    call_site = (cpu.s.cs & 0xFFFF, cpu.s.ip & 0xFFFF)
    previous_call_site = getattr(cpu, "hook_call_site", None)
    cpu.hook_call_site = (call_site[0], call_site[1], key[0] & 0xFFFF, key[1] & 0xFFFF, return_ip & 0xFFFF)
    cpu.push(return_ip & 0xFFFF)
    cpu.s.cs = key[0] & 0xFFFF
    cpu.s.ip = key[1] & 0xFFFF
    verifier = getattr(cpu, "hook_verifier", None)
    try:
        if (
            verifier is not None
            and getattr(cpu, "hook_verifier_verify_nested_calls", True)
            and key not in getattr(cpu, "hook_verifier_passthrough", set())
        ):
            verifier(cpu, key, handler, name)
        else:
            handler(cpu)
    finally:
        if previous_call_site is None:
            try:
                delattr(cpu, "hook_call_site")
            except AttributeError:
                pass
        else:
            cpu.hook_call_site = previous_call_site


def call_installed_hook_like_far_call(
    cpu: CPU8086,
    key: tuple[int, int],
    default_handler: Hook,
    return_cs: int,
    return_ip: int,
) -> None:
    """Run an installed child hook with original FAR-CALL stack semantics.

    The far mirror of :func:`call_installed_hook_like_near_call` (the linker
    seam): pushes ``return_cs`` then ``return_ip`` (the 8086 far frame), jumps
    to the callee, and runs the installed hook (or the linked default).  Only
    a callee whose every exit is ``retf`` is equivalent to the original far
    CALL — the link tool enforces that edge rule, exactly as it enforces
    near-``ret`` exits for near links.
    """
    handler = cpu.replacement_hooks.get(key, default_handler)
    name = cpu.hook_names.get(key, getattr(handler, "__name__", "replacement"))
    call_site = (cpu.s.cs & 0xFFFF, cpu.s.ip & 0xFFFF)
    previous_call_site = getattr(cpu, "hook_call_site", None)
    cpu.hook_call_site = (call_site[0], call_site[1], key[0] & 0xFFFF, key[1] & 0xFFFF, return_ip & 0xFFFF)
    cpu.push(return_cs & 0xFFFF)
    cpu.push(return_ip & 0xFFFF)
    cpu.s.cs = key[0] & 0xFFFF
    cpu.s.ip = key[1] & 0xFFFF
    verifier = getattr(cpu, "hook_verifier", None)
    try:
        if (
            verifier is not None
            and getattr(cpu, "hook_verifier_verify_nested_calls", True)
            and key not in getattr(cpu, "hook_verifier_passthrough", set())
        ):
            verifier(cpu, key, handler, name)
        else:
            handler(cpu)
    finally:
        if previous_call_site is None:
            try:
                delattr(cpu, "hook_call_site")
            except AttributeError:
                pass
        else:
            cpu.hook_call_site = previous_call_site


# ---- live-code signature guards ------------------------------------------------------------------
# Games patch parts of their live code segment at runtime (self-modifying code, loader decor,
# variant dispatch). A Python replacement bypasses the live bytes at CS:IP, so a hook that assumed
# a fixed routine shape would silently run WRONG code when the bytes changed. The proven rule
# (Overkill's runtime-code staticization): every accepted live-byte body is named and guarded by
# signature; an unknown variant fails loud, never silently falls back.


def signature_matches(live: bytes, expected: bytes | tuple[bytes, ...]) -> bool:
    """Whether ``live`` starts with any of the expected signature variants."""
    variants = expected if isinstance(expected, tuple) else (expected,)
    return any(live[: len(sig)] == sig for sig in variants)


def code_matches(cpu: CPU8086, off: int, expected: bytes | tuple[bytes, ...]) -> bool:
    """Return whether live CS:off bytes still match a lifted hook signature."""
    cs = cpu.s.cs & 0xFFFF
    variants = expected if isinstance(expected, tuple) else (expected,)
    return any(
        all(cpu.mem.rb(cs, (off + i) & 0xFFFF) == b for i, b in enumerate(sig))
        for sig in variants
    )


def self_disable_if_patched(cpu: CPU8086, ip: int, expected: bytes | tuple[bytes, ...], name: str) -> bool:
    """Fail fast when a hook entry no longer matches the lifted ASM bytes.

    Wrappers that assume a fixed routine shape call this on entry and refuse to
    run when the original bytes changed (an unknown runtime-patched variant).
    Synthetic tests often leave code bytes all zero; that fixture case is treated
    as "no live signature available" and remains enabled. Returns False when the
    signature is fine; raises RuntimeError on an unknown variant.
    """
    # Under a poisoned data-only boot image the recovered code bytes are zeroed
    # by design for this detached execution diagnostic; the lifted host
    # function is the
    # authoritative implementation, so the entry-signature comparison is both
    # meaningless and prone to false-alarm on the poisoned bytes.  Skip it.
    if getattr(cpu, "code_poisoned", False):
        return False
    cs = cpu.s.cs & 0xFFFF
    start = ((cs << 4) + (ip & 0xFFFF)) & 0xFFFFF
    variants = expected if isinstance(expected, tuple) else (expected,)
    max_len = max(len(sig) for sig in variants)
    live = bytes(cpu.mem.data[start:start + max_len])
    all_zero = any(all(b == 0 for b in live[: len(sig)]) for sig in variants)
    if signature_matches(live, expected) or all_zero:
        return False

    expected_text = " or ".join(sig.hex(" ") for sig in variants)
    raise RuntimeError(
        f"hook {name} at {cs:04X}:{ip:04X} saw runtime-patched code; "
        f"live bytes {live.hex(' ')} != expected {expected_text}"
    )


def interpret_current_instruction_without_hook(cpu: CPU8086) -> None:
    """Interpret live ASM once when an overlaid hook signature no longer matches.

    Temporarily removes the replacement at the current address, steps the real
    bytes, and reinstalls it — the bounded, *explicit* way to let an original
    variant run (as opposed to a silent fallback)."""
    key = cpu.addr()
    fn = cpu.replacement_hooks.pop(key, None)
    telemetry = getattr(cpu, "coverage_telemetry", None)
    ctx = (
        telemetry.bounded_original(key, "overlaid hook signature mismatch")
        if telemetry is not None and hasattr(telemetry, "bounded_original")
        else None
    )
    try:
        if ctx is not None:
            ctx.__enter__()
        cpu.step()
    finally:
        if ctx is not None:
            ctx.__exit__(None, None, None)
        if fn is not None:
            cpu.replacement_hooks[key] = fn


def jump_installed_hook_boundary(
    cpu: CPU8086,
    key: tuple[int, int],
    default_handler: Hook,
) -> None:
    """Run an installed child hook reached by original JMP/fall-through semantics.

    This is the sibling of :func:`call_installed_hook_like_near_call` for
    original control flow that transfers to another ASM routine without pushing
    a return word.  Lifted parent hooks use it when they manually jump or
    fall through into a registered child boundary.  The child still sees its
    real CS:IP, and live hook verification can therefore diff that child
    independently instead of letting it become a shared black box inside the
    parent transaction.
    """
    handler = cpu.replacement_hooks.get(key, default_handler)
    name = cpu.hook_names.get(key, getattr(handler, "__name__", "replacement"))
    jump_site = (cpu.s.cs & 0xFFFF, cpu.s.ip & 0xFFFF)
    previous_jump_site = getattr(cpu, "hook_jump_site", None)
    cpu.hook_jump_site = (jump_site[0], jump_site[1], key[0] & 0xFFFF, key[1] & 0xFFFF)
    cpu.s.cs = key[0] & 0xFFFF
    cpu.s.ip = key[1] & 0xFFFF
    verifier = getattr(cpu, "hook_verifier", None)
    try:
        if (
            verifier is not None
            and getattr(cpu, "hook_verifier_verify_nested_calls", True)
            and key not in getattr(cpu, "hook_verifier_passthrough", set())
        ):
            verifier(cpu, key, handler, name)
        else:
            handler(cpu)
    finally:
        if previous_jump_site is None:
            try:
                delattr(cpu, "hook_jump_site")
            except AttributeError:
                pass
        else:
            cpu.hook_jump_site = previous_jump_site
