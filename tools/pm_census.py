#!/usr/bin/env python3
"""Rank a PM demo's hot call targets — the "what do I recover next" tool.

Replays the first N frames of an input-demo bundle counting every near-call
(``E8``) target, then statically profiles each: instruction count to ``ret``,
nested calls, INTs, port I/O.  Pure leaves (no calls / no INT / no port I/O)
are the recover-first candidates; port-I/O routines are hardware, not game
logic.  Already-hooked addresses are tagged so mined targets drop out.

Usage (from the port root, so the game package imports):
    python dos_re/tools/pm_census.py --exe assets/GAME.EXE \
        --demo artifacts/demos/demo_X \
        [--install kegg.render_hooks:install_render_hooks ...] \
        [--frames 120] [--region 0x110000:0x120000] [--leaf-only] [--top 30]

Pass the SAME ``--install`` list as pm_verify_demo so recovered hooks are
excluded from the ranking (a hook executes atomically — its internal calls
would vanish from the census anyway; tagging keeps the picture honest).

When to use: after each recovery lands, to pick the next target; with
``--region`` narrowed to the game's code (not the RTL) the top pure leaf is
usually the right next slice.
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (os.getcwd(), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from dos_re.lift.decode32 import decode32  # noqa: E402
from dos_re.pm_input_demo import PMInputDemo, FrameClock, FramePaced  # noqa: E402
from dos_re.pm_snapshot import load_pm_snapshot  # noqa: E402

_PORT_OPS = {"ec", "ed", "ee", "ef", "e4", "e5", "e6", "e7",
             "6c", "6d", "6e", "6f"}


def _profile(data, addr, max_ins=300):
    """Linear profile to the first ret: (#ins, #calls, #ints, port_io?)."""
    read = lambda a: data[a]                        # noqa: E731
    a, nins, ncalls, nints, port = addr, 0, 0, 0, False
    while nins < max_ins:
        try:
            ins = decode32(read, a)
        except Exception:                           # noqa: BLE001
            break
        b = bytes(data[a:a + ins.length]).hex()
        if b[:2] == "e8" or b[:4] in ("ff15", "ff10", "ff11", "ff12", "ff13",
                                      "ff50", "ff51", "ff52", "ff53"):
            ncalls += 1
        if b[:2] == "cd":
            nints += 1
        op = b[2:4] if b[:2] == "66" else b[:2]
        if op in _PORT_OPS:
            port = True
        nins += 1
        a = ins.next_ip
        if b == "c3":
            break
    return nins, ncalls, nints, port


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
    ap.add_argument("--frames", type=int, default=120)
    ap.add_argument("--region", default=None, metavar="LO:HI",
                    help="only rank targets in [LO,HI) (hex)")
    ap.add_argument("--leaf-only", action="store_true",
                    help="only pure leaves (no calls / INT / port I/O)")
    ap.add_argument("--top", type=int, default=30)
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
    hooked = set(cpu.replacement_hooks)

    lo, hi = 0, 1 << 32
    if args.region:
        s_lo, _, s_hi = args.region.partition(":")
        lo, hi = int(s_lo, 16), int(s_hi, 16)

    from dos_re.pm_player import send_key
    data = cpu.mem.data
    calls = Counter()
    base_step = cpu.step

    def counting_step():
        e = cpu.eip
        if data[e] == 0xE8:
            rel = int.from_bytes(bytes(data[e + 1:e + 5]), "little", signed=True)
            calls[(e + 5 + rel) & 0xFFFFFFFF] += 1
        base_step()

    cpu.step = counting_step
    by_frame = demo.by_frame()

    def on_frame(frame):
        for kind, payload in by_frame.get(frame, ()):
            if kind == "key":
                send_key(dos, payload[1], payload[0])
            elif kind == "mouse":
                dos.set_mouse_norm(payload[0], payload[1])
                dos.mouse_buttons = payload[2]

    clock = FrameClock(cpu, demo.frame_tick_addr, on_frame)
    end = min(args.frames, demo.total_frames)
    while clock.frame < end and not cpu.halted:
        clock.stop_at = clock.frame + 1
        try:
            cpu.run(8_000_000)
        except FramePaced:
            pass
    cpu.step = base_step

    print(f"call-target census over {clock.frame} frames "
          f"(region 0x{lo:X}..0x{hi:X}):")
    shown = 0
    for addr, hits in calls.most_common():
        if not (lo <= addr < hi):
            continue
        nins, ncalls, nints, port = _profile(data, addr)
        leaf = ncalls == 0 and nints == 0 and not port
        if args.leaf_only and (not leaf or addr in hooked):
            continue
        tags = []
        if addr in hooked:
            tags.append("HOOKED")
        tags.append("LEAF" if leaf else f"{ncalls}c")
        if nints:
            tags.append(f"{nints}int")
        if port:
            tags.append("port-io")
        print(f"  0x{addr:06X}  hits={hits:6d}  ins={nins:3d}  "
              + ",".join(tags))
        shown += 1
        if shown >= args.top:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
