"""cpuless_census.py -- the M3 promotion census over a recovery IR.

Runs the CPU-ABI inference (dos_re.lift.cpuless) over every censused function
and reports which functions the CPUless emitter can take today and which
capabilities are missing, ranked by how much corpus they unblock -- the M3
work list (docs/history/dos_re_2.0.md section 1, stage 2).

Tiers:
    leaf        no refusals at all: fully analyzed straight-line/branchy code
                with static stack discipline -- the first emitter targets
    calls-only  analyzable except near-call composition (the callee ABI
                composes once callees are promoted or wrapped)
    blocked     needs a named capability (indirect transfers, INT platform
                effects, port I/O, unanalyzed opcodes, ...)

Usage:
    python dos_re/tools/cpuless_census.py --ir artifacts/lift/recovery_ir.json \
        --out artifacts/m3/abi_census.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dos_re.lift.cpuless import classify_corpus  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ir", required=True)
    ap.add_argument("--out", default=None,
                    help="write the full census JSON here")
    args = ap.parse_args(argv)

    ir = json.loads(Path(args.ir).read_text(encoding="utf-8"))
    census = classify_corpus(ir)

    counts = census["tier_counts"]
    total = sum(counts.values())
    print(f"CPUless promotion census over {total} functions:")
    for tier in ("leaf", "calls-only", "blocked"):
        print(f"  {tier:<11} {counts.get(tier, 0):4d}")
    print("missing capabilities (sites, ranked):")
    for cap, n in census["missing_capabilities"].items():
        print(f"  {cap:<32} {n:6d}")
    if census["tiers"]["leaf"]:
        sample = ", ".join(census["tiers"]["leaf"][:8])
        print(f"first leaf targets: {sample}")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(census, indent=1), encoding="utf-8")
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
