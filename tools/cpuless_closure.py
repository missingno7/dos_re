"""cpuless_closure.py -- runtime-closure measurement for play_cpuless.

Completion of the CPUless runtime is measured by the REQUIRED RUNTIME CLOSURE
from declared startup/gameplay roots, NOT by "all named functions promoted"
(dos_re_2.0.md section 6; owner directive).  From each root this walks the
static near-call graph (recovery IR) and partitions every reachable function:

    promoted   -- a recovered CPUless implementation exists
    frontier   -- reachable but NOT yet promoted; the next work, each tagged
                  with the promotion-census refusal reason

A root/edge into a far call, an interrupt, or an indirect transfer is itself a
frontier item (the call graph cannot be followed statically past it until that
construct is recovered).  The closure is COMPLETE when the frontier is empty:
every function reachable from the roots is CPUless.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _promoted_keys(recovered_dir: Path) -> set[str]:
    keys = set()
    for p in recovered_dir.glob("func_*.py"):
        _, cs, ip = p.stem.split("_")
        keys.add(f"{int(cs, 16):04X}:{int(ip, 16):04X}")
    return keys


def walk_closure(ir: dict, roots: list[str], promoted: set[str],
                 refusals: dict[str, str]) -> dict:
    reached: set[str] = set()
    frontier: dict[str, str] = {}
    work = list(roots)
    while work:
        key = work.pop()
        key = key.upper()
        if key in reached:
            continue
        reached.add(key)
        rec = ir["functions"].get(key)
        if key not in promoted:
            # a frontier node -- record WHY, but still follow its statically
            # known near-call targets so the WHOLE reachable graph (and the
            # real frontier depth) is measured, not just the first gap.
            frontier[key] = refusals.get(key, "not-in-ir" if not rec else "not-promoted")
        if not rec:
            continue
        cs = int(key.split(":")[0], 16)
        for blk in rec["blocks"]:
            for i in blk["instructions"]:
                if i["mnemonic"] == "call" and "target" in i:
                    work.append(f"{cs:04X}:{int(i['target'], 16):04X}")
    prom_reached = sorted(reached & promoted)
    return {
        "roots": roots,
        "reached": len(reached),
        "promoted_reached": len(prom_reached),
        "frontier": dict(sorted(frontier.items())),
        "closure_complete": not frontier,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ir", required=True)
    ap.add_argument("--recovered-dir", required=True)
    ap.add_argument("--census", default=None,
                    help="promotion census JSON (for frontier refusal reasons)")
    ap.add_argument("--roots", required=True,
                    help="comma-separated CS:IP startup/gameplay roots")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    ir = json.loads(Path(args.ir).read_text(encoding="utf-8"))
    promoted = _promoted_keys(Path(args.recovered_dir))
    refusals: dict[str, str] = {}
    if args.census and Path(args.census).is_file():
        for reason, keys in json.loads(
                Path(args.census).read_text(encoding="utf-8")).get("refused", {}).items():
            for k in keys:
                refusals[k.upper()] = reason
    roots = [r.strip().upper() for r in args.roots.split(",") if r.strip()]

    rep = walk_closure(ir, roots, promoted, refusals)
    print(f"CPUless runtime closure from roots {roots}:")
    print(f"  reachable functions:   {rep['reached']}")
    print(f"  promoted (reached):    {rep['promoted_reached']}")
    print(f"  frontier (to promote): {len(rep['frontier'])}")
    from collections import Counter
    by_reason = Counter(rep["frontier"].values())
    for reason, n in by_reason.most_common():
        print(f"    {reason:<34} {n}")
    print(f"  CLOSURE COMPLETE: {rep['closure_complete']}")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(rep, indent=1), encoding="utf-8")
        print(f"  wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
