"""liftgen — M0 census for the automatic lifter (docs/lifting_design.md §10).

Given a snapshot and a list of function entry points, statically scan each
function (dos_re.lift) with the interpreter cross-checking every decoded
instruction length, and report: liftable or not, size, blocks, exits, call
dependencies, INT usage, and the refusal taxonomy. No code generation — this
tool exists to measure what fraction of REAL functions the v1 lifter subset
would cover, before any emitter is written.

Usage:
    python tools/liftgen.py --exe GAME.EXE --snapshot DIR \
        --entry 1010:4537 [--entry CS:IP ...] [--entries-file F] [--json OUT]

--entries-file: one CS:IP per line, '#' comments allowed.
Probe details: per entry, the runtime is cloned once; each NON-TRANSFER
instruction is length-measured as the IP delta of one step() at a forced
CS:IP (decode/operand fetches advance s.ip byte-by-byte, including the
interpreter's inlined fast paths; transfers overwrite IP and are fixed-size
encodings covered by unit tests instead). A probe step that itself faults
marks the address "unchecked" (reported); a successful probe disagreeing
with the static decode refuses the function (decoder-mismatch).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dos_re.lift import scan_function  # noqa: E402
from dos_re.repro_artifacts import clone_runtime_state  # noqa: E402
from dos_re.snapshot import load_snapshot, parse_addr  # noqa: E402


def _make_probe(rt, cs: int):
    """Interpreter IP-delta probe at cs:ip (see module docstring)."""
    scratch = clone_runtime_state(rt)
    cpu = scratch.cpu
    cpu.replacement_hooks.clear()
    cpu.hook_names.clear()
    cpu.hook_verifier = None
    cpu.trace_enabled = False
    cpu.pending_irq = None

    def probe(ip: int) -> int | None:
        ip &= 0xFFFF
        cpu.s.cs = cs
        cpu.s.ip = ip
        try:
            cpu.step()
        except Exception:  # noqa: BLE001 — probe is advisory; scan records it
            return None
        return ((cpu.s.ip - ip) & 0xFFFF) or None

    return probe


def scan_entry(rt, cs: int, ip: int):
    mem = rt.cpu.mem

    def fetch(off: int) -> int:
        return mem.rb(cs, off & 0xFFFF)

    return scan_function(fetch, ip & 0xFFFF, probe=_make_probe(rt, cs))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--exe", required=True)
    p.add_argument("--snapshot", required=True)
    p.add_argument("--entry", action="append", default=[], metavar="CS:IP")
    p.add_argument("--entries-file")
    p.add_argument("--json", help="write full per-function results to this file")
    p.add_argument("--game-root", default=None)
    args = p.parse_args(argv)

    entries = [parse_addr(e) for e in args.entry]
    if args.entries_file:
        for line in Path(args.entries_file).read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                entries.append(parse_addr(line))
    if not entries:
        p.error("no entries given (--entry / --entries-file)")

    rt = load_snapshot(args.exe, args.snapshot, game_root=args.game_root)
    rt.cpu.trace_enabled = False

    results = []
    refusal_hist: Counter[str] = Counter()
    liftable = 0
    for cs, ip in entries:
        scan = scan_entry(rt, cs, ip)
        lo, hi = scan.region
        rec = {
            "entry": f"{cs:04X}:{ip:04X}",
            "liftable": scan.liftable,
            "insts": len(scan.insts),
            "bytes": (hi - lo) & 0xFFFF,
            "blocks": len(scan.block_leaders()),
            "exits": sorted({i.kind for i in scan.exits}),
            "calls_near": sorted(f"{t:04X}" for t in scan.calls_near),
            "calls_far": sorted(f"{s:04X}:{o:04X}" for s, o in scan.calls_far),
            "calls_indirect": len(scan.calls_indirect),
            "ints": sorted(scan.ints),
            "probe_unchecked": len(scan.probe_unchecked),
            "refusals": [{"ip": f"{r.ip:04X}", "reason": r.reason, "detail": r.detail}
                         for r in scan.refusals],
        }
        results.append(rec)
        if scan.liftable:
            liftable += 1
        for r in scan.refusals:
            refusal_hist[r.reason] += 1
        flag = "LIFTABLE " if scan.liftable else "refused  "
        reasons = ",".join(sorted({r.reason for r in scan.refusals})) or "-"
        print(f"{flag} {rec['entry']}  insts={rec['insts']:<4} blocks={rec['blocks']:<3} "
              f"calls={len(scan.calls_near)}+{rec['calls_indirect']}i "
              f"ints={','.join(map(str, rec['ints'])) or '-':<6} {reasons}")

    print(f"\n{liftable}/{len(entries)} liftable "
          f"({100.0 * liftable / len(entries):.0f}%)")
    if refusal_hist:
        print("refusal histogram:")
        for reason, n in refusal_hist.most_common():
            print(f"  {n:4d}  {reason}")
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1), encoding="utf-8")
        print(f"json: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
