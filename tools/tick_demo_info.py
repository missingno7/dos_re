"""Inspect a game-tick demo file (dos_re.tick_demo format).

The endgame's proof artifacts are opaque binaries; this prints what one contains — tick count, key-record
length, sideband channels, seed size — plus a short per-channel value summary, so a corpus census or a
stale-recording diagnosis doesn't need ad-hoc parsing.

Usage:
    python tools/tick_demo_info.py <demo.bin> [<demo.bin> ...]

When to use: checking what a recording covers before trusting it, comparing two recordings' shapes,
verifying a re-record actually replaced the file (tick counts differ), or confirming a port's writer
produced a loadable file. (Ports with their own pre-framework format need their own inspector.)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dos_re.tick_demo import TickDemo  # noqa: E402


def describe(path: str) -> int:
    try:
        demo = TickDemo.load(path)
    except Exception as e:  # noqa: BLE001 — a bad file is the finding
        print(f"{path}: NOT LOADABLE — {type(e).__name__}: {e}")
        return 1
    klen = len(demo.keys[0]) if demo.keys else 0
    print(f"{path}:")
    print(f"  ticks       : {demo.n_ticks}")
    print(f"  key record  : {klen} bytes/tick")
    print(f"  seed        : {len(demo.seed):,} bytes (full memory image)")
    if demo.sidebands:
        for name, ch in demo.sidebands.items():
            lo, hi = (min(ch), max(ch)) if ch else (0, 0)
            changes = sum(1 for a, b in zip(ch, ch[1:]) if a != b)
            print(f"  sideband    : {name!r} u16 x{len(ch)}  min={lo} max={hi} changes={changes}")
    else:
        print("  sideband    : (none)")
    if demo.digests:
        print(f"  digests     : {len(demo.digests)} (first {demo.digests[0][:12]}…, last {demo.digests[-1][:12]}…)")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 2
    return max(describe(p) for p in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
