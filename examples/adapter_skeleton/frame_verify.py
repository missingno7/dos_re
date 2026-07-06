"""TEMPLATE — the game adapter side of the frame verifier.

The framework's dos_re.frame_verify.run_frame_verifier is game-independent; the
adapter supplies everything that is game knowledge:

- boundary_hooks: the (cs, ip) addresses of the present/timer/retrace routines
  where a frame is considered complete,
- sample_builder: how to capture one comparable frame (framebuffer bytes +
  decoded RGB + any state you want diffed),
- reference_env_hooks: the hardware-wait hooks the ORACLE side must keep so the
  original ASM does not spin forever on a flag a real IRQ would clear,
- pump_inputs / input-wait detection: shared with input_waits.py.

Start with framebuffer + visible VRAM, then widen the sample until it covers
ALL observable state (objects, RNG, score, timers) — if it is not in the
sample, divergence can hide there.
"""
from __future__ import annotations

# TODO: your game's frame geometry and video memory layout.
WIDTH, HEIGHT = 320, 200

# TODO: fill after locating the present/timer/retrace routines (see
# docs/porting_new_game.md step 3).
BOUNDARY_HOOKS: tuple[tuple[tuple[int, int], str], ...] = (
    # ((0x1010, 0x5BDC), "present"),
)

REFERENCE_ENV_HOOKS: set[tuple[int, int]] = {
    # (0x1010, 0x0679),  # PIT tick wait
    # (0x1010, 0x50C9),  # CRTC retrace wait
}
