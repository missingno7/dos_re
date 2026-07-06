"""TEMPLATE — boundary-less input-wait loop registry (READ docs/demos_and_snapshots.md).

Some original code busy-waits on the keyboard WITHOUT reaching a timer/retrace/
present boundary (e.g. "press FIRE to start" polls).  The demo clock is frozen
inside such a loop, so a recorded key release keyed to a later boundary is never
delivered — the loop waits forever.  EVERY driver (interactive play, headless
verifier, frame verifier) must recognize these loops via THIS one shared
registry and treat them as a boundary; duplicating detectors per-driver
guarantees they drift and voids the demo proof.

Detect each loop at its canonical HEAD address and check it every step, so the
reference and candidate runtimes stop at the identical instruction.
"""
from __future__ import annotations

# TODO: fill with (cs, ip) canonical head addresses of your game's input polls.
INPUT_WAIT_HEADS: dict[tuple[int, int], str] = {
    # (0x1010, 0x0162): "title screen: wait for FIRE press/release",
}


def is_input_wait(addr: tuple[int, int]) -> bool:
    return addr in INPUT_WAIT_HEADS
