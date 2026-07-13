#!/usr/bin/env python3
"""Replay a PM input-demo bundle with the differential hook verifier on.

The standard proof loop for protected-mode routine recovery: take a recorded
gameplay demo (the verification corpus), install the port's replacement hooks,
and re-run the demo with ``PMHookVerifier`` diffing every hooked call against
the interpreted original.  Prints per-hook verified call counts; exits nonzero
on the first divergence (with the verifier's full state diff).

Usage (from the port root, so the game package imports):
    python dos_re/tools/pm_verify_demo.py --exe assets/GAME.EXE \
        --demo artifacts/demos/demo_X \
        --install kegg.render_hooks:install_render_hooks \
        --install kegg.logic_hooks:install_logic_hooks \
        [--focus 0x11B5DF ...] [--frames N]

``--install mod:func`` (repeatable) names installers called with ``cpu``.
``--focus ADDR`` (repeatable) verifies only those hooks, passing the rest
through unverified — the fast loop while recovering ONE routine.  Without
``--focus`` every installed hook is verified (the pre-commit full pass).
Composed hooks proven by the observable-state verifier (pm_composition)
should NOT be in ``--install`` here: this tool's verifier full-diffs.

When to use: after writing/changing a recovered hook (focused), and before
committing a recovery (unfocused).  The demo must be a post-determinism-fix
recording (per-frame digests present) or at least replay reproducibly.
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (os.getcwd(), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from dos_re.pm_input_demo import PMInputDemo, FrameClock, FramePaced  # noqa: E402
from dos_re.pm_snapshot import load_pm_snapshot  # noqa: E402
from dos_re.pm_verification import (PMHookVerifierConfig,  # noqa: E402
                                    PMHookVerifyDivergence,
                                    install_pm_hook_verifier)


def _resolve(spec: str):
    mod, _, func = spec.partition(":")
    if not func:
        raise SystemExit(f"--install needs mod:func, got {spec!r}")
    return getattr(importlib.import_module(mod), func)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--exe", required=True)
    ap.add_argument("--demo", required=True, help="demo bundle directory")
    ap.add_argument("--install", action="append", default=[],
                    metavar="MOD:FUNC", help="hook installer, called with cpu")
    ap.add_argument("--focus", action="append", default=[], metavar="ADDR",
                    help="verify only these hook EIPs (hex); rest pass through")
    ap.add_argument("--frames", type=int, default=None,
                    help="replay only the first N frames (default: whole demo)")
    args = ap.parse_args(argv)

    demo = PMInputDemo.load(args.demo)
    if demo.frame_tick_addr is None:
        print("demo has no frame_tick_addr; cannot replay")
        return 1
    snap = Path(args.demo) / "snapshot"
    rt = load_pm_snapshot(args.exe, str(snap) if snap.exists() else None)
    cpu, dos = rt.cpu, rt.dos

    for spec in args.install:
        _resolve(spec)(cpu)
    focus = {int(a, 16) for a in args.focus}
    if focus:
        unknown = focus - set(cpu.replacement_hooks)
        if unknown:
            print("--focus not installed: " + ", ".join(hex(a) for a in unknown))
            return 1
        for k in list(cpu.replacement_hooks):
            if k not in focus:
                cpu.hook_verifier_passthrough.add(k)
    v = install_pm_hook_verifier(rt, PMHookVerifierConfig(samples=None))

    from dos_re.pm_player import send_key   # late: pygame-free until needed
    by_frame = demo.by_frame()
    end = demo.total_frames if args.frames is None else min(args.frames,
                                                            demo.total_frames)

    def on_frame(frame):
        for kind, payload in by_frame.get(frame, ()):
            if kind == "key":
                send_key(dos, payload[1], payload[0])
            elif kind == "mouse":
                dos.set_mouse_norm(payload[0], payload[1])
                dos.mouse_buttons = payload[2]

    clock = FrameClock(cpu, demo.frame_tick_addr, on_frame)
    try:
        while clock.frame < end and not cpu.halted:
            clock.stop_at = clock.frame + 1
            try:
                cpu.run(8_000_000)
            except FramePaced:
                pass
    except PMHookVerifyDivergence as e:
        print(e)
        return 1

    print(f"replayed {clock.frame} frames; verified hook calls:")
    verified = focus or set(cpu.replacement_hooks) - cpu.hook_verifier_passthrough
    failed = False
    for k in sorted(verified):
        n = v.calls_per_hook.get(k, 0)
        name = cpu.hook_names.get(k, "?")
        status = "ORACLE_PASSING" if n else "NOT_REACHED"
        print(f"  0x{k:06X} {name:34s} {n:6d}  {status}")
        failed |= n == 0
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
