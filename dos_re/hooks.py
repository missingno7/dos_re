from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from .cpu import CPU8086


Hook = Callable[[CPU8086], None]


@dataclass(frozen=True)
class Replacement:
    cs: int
    ip: int
    name: str
    handler: Hook


class HookRegistry:
    """Maps original DOS addresses to Python replacements.

    The intended migration path is:
    1. execute original ASM and collect traces,
    2. understand a small routine,
    3. register a replacement at its CS:IP,
    4. let the rest of the original binary continue running.
    """

    def __init__(self) -> None:
        self.replacements: dict[tuple[int, int], Replacement] = {}

    def replace(self, cs: int, ip: int, name: str):
        key = (cs & 0xFFFF, ip & 0xFFFF)

        def deco(fn: Hook) -> Hook:
            # Fail fast on duplicate registrations.  The map is keyed by CS:IP, so
            # a second @replace at the same address would silently shadow the
            # first; that is exactly how superseded hook implementations used to
            # accrete unnoticed.  One address must have exactly one replacement.
            existing = self.replacements.get(key)
            if existing is not None:
                raise ValueError(
                    f"duplicate replacement at {key[0]:04X}:{key[1]:04X} "
                    f"({existing.name!r} then {name!r})"
                )
            self.replacements[key] = Replacement(key[0], key[1], name, fn)
            return fn
        return deco

    def install(self, cpu: CPU8086) -> None:
        # Allow individual hooks to be disabled without code changes, e.g.
        # DOS_RE_DISABLE_HOOKS=<cs>:<ip>,<cs>:<ip>.  Disabled addresses fall
        # back to the interpreted original ASM, which is useful for A/B
        # performance checks and for bisecting a suspected-incorrect hook.
        disabled = _parse_disabled(os.environ.get("DOS_RE_DISABLE_HOOKS", ""))
        for key, repl in self.replacements.items():
            if key in disabled:
                continue
            cpu.replacement_hooks[key] = repl.handler
            cpu.hook_names[key] = repl.name


def _parse_disabled(text: str) -> set[tuple[int, int]]:
    out: set[tuple[int, int]] = set()
    for token in text.replace(";", ",").split(","):
        token = token.strip()
        if not token:
            continue
        cs, _, ip = token.partition(":")
        out.add((int(cs, 16) & 0xFFFF, int(ip, 16) & 0xFFFF))
    return out


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


registry = HookRegistry()


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
    # by design (the EXE-independence wall); the lifted host function is the
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
