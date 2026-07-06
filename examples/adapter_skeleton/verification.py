"""TEMPLATE — per-hook continuation metadata for the differential verifier.

Each registered hook declares where the original routine legitimately ends
(near RET, far RET, IRET, a fixed continuation IP, a computed dispatch...).
The verifier clones the runtime, runs the ORIGINAL ASM to that target, runs
your hook, and diffs registers + flags + full memory.

Alternative: HookVerifierConfig.strict() (auto-continuation) needs no metadata
at all — slower, ideal while investigating a single routine.
"""
from __future__ import annotations

from dos_re.verification import GenericHookStop

# TODO: one entry per registered hook address.
DEFAULT_STOPS: dict[tuple[int, int], GenericHookStop] = {
    # (0x1010, 0x1234): GenericHookStop("near_ret"),
}
