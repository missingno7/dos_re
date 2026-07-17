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
                 refusals: dict[str, str],
                 dyn_evidence: dict[str, list[str]] | None = None,
                 observed: set[str] | None = None) -> dict:
    """``dyn_evidence`` (tier 9): "CS:IP" site -> observed dynamic target
    keys (lemmings-style indirect_sites.json), so the walk follows dynamic
    dispatch and static far calls, not just near calls.

    ``observed`` (function entries the game ACTUALLY executed, from the census
    probe): the static near-call walk over-approximates -- a `call` in a branch
    no run ever takes reaches a target that never runs (SkyRoads: 15 such,
    zero of them executed across 7,651 observed addresses). Supplying it splits
    the frontier into RUNTIME-REACHABLE (real work: the game runs it, so a true
    CPUless build must recover it) and STATIC-ONLY (reached only through an
    untaken call -- no runtime evidence it is live). The runtime frontier is the
    honest completion target; static-only items are REPORTED, never dropped
    (replay coverage is not proof of deadness -- each still owes an explanation
    or a lift before the wall can call itself closed)."""
    dyn_evidence = dyn_evidence or {}
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
                elif i.get("kind") == "call_far":
                    by = bytes.fromhex(i["bytes"])
                    if by and by[0] == 0x9A:
                        off = by[1] | (by[2] << 8)
                        seg = by[3] | (by[4] << 8)
                        work.append(f"{seg:04X}:{off:04X}")
                elif i.get("kind") in ("call_ind", "jmp_ind"):
                    site = f"{cs:04X}:{int(i['ip'], 16):04X}"
                    for tgt in dyn_evidence.get(site, ()):
                        # an observed target that is an IR function walks;
                        # a dispatch-entry arrival shares blocks already
                        # reached through its containing function.
                        if tgt.upper() in ir["functions"]:
                            work.append(tgt.upper())
    prom_reached = sorted(reached & promoted)
    observed = observed or set()
    # split the frontier by runtime evidence when an observed set is supplied.
    # Without one, every frontier item is treated as runtime-reachable (the
    # conservative default -- no evidence to demote anything to static-only).
    runtime_frontier = {k: v for k, v in frontier.items()
                        if not observed or k in observed}
    static_only = {k: v for k, v in frontier.items()
                   if observed and k not in observed}
    return {
        "roots": roots,
        "reached": len(reached),
        "promoted_reached": len(prom_reached),
        "frontier": dict(sorted(frontier.items())),
        "runtime_frontier": dict(sorted(runtime_frontier.items())),
        "static_only_frontier": dict(sorted(static_only.items())),
        # the honest target: every function the game RUNS is CPUless. Static-only
        # items are reported (they still owe an explanation) but do not block it.
        "closure_complete": not runtime_frontier,
        "static_closure_complete": not frontier,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ir", required=True)
    ap.add_argument("--recovered-dir", required=True)
    ap.add_argument("--census", default=None,
                    help="promotion census JSON (for frontier refusal reasons)")
    ap.add_argument("--roots", required=True,
                    help="comma-separated CS:IP startup/gameplay roots")
    ap.add_argument("--dyn-evidence", default=None,
                    help="indirect_sites.json (per-site dynamic-target "
                         "evidence) so the walk follows dynamic dispatch")
    ap.add_argument("--observed", default=None,
                    help="observed.json (probe execution trace): splits the "
                         "frontier into runtime-reachable vs static-only, so a "
                         "call never taken at runtime does not block closure")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    ir = json.loads(Path(args.ir).read_text(encoding="utf-8"))
    promoted = _promoted_keys(Path(args.recovered_dir))
    observed: set[str] = set()
    if args.observed and Path(args.observed).is_file():
        doc = json.loads(Path(args.observed).read_text(encoding="utf-8"))
        # the executed trace lists every address stepped; a function is
        # "observed" when its ENTRY was executed.
        observed = {a.upper() for a in doc.get("executed", ())
                    if isinstance(a, str)}
    refusals: dict[str, str] = {}
    if args.census and Path(args.census).is_file():
        for reason, keys in json.loads(
                Path(args.census).read_text(encoding="utf-8")).get("refused", {}).items():
            for k in keys:
                refusals[k.upper()] = reason
    roots = [r.strip().upper() for r in args.roots.split(",") if r.strip()]
    dyn_evidence: dict[str, list[str]] = {}
    if args.dyn_evidence and Path(args.dyn_evidence).is_file():
        for site in json.loads(Path(args.dyn_evidence).read_text(
                encoding="utf-8")).get("sites", []):
            dyn_evidence[site["site"].upper()] = \
                sorted(k.upper() for k in site.get("targets", {}))

    rep = walk_closure(ir, roots, promoted, refusals, dyn_evidence, observed)
    from collections import Counter
    print(f"CPUless runtime closure from roots {roots}:")
    print(f"  reachable functions:   {rep['reached']}")
    print(f"  promoted (reached):    {rep['promoted_reached']}")
    rt = rep["runtime_frontier"]
    so = rep["static_only_frontier"]
    label = "runtime-reachable" if observed else "to promote"
    print(f"  frontier ({label}): {len(rt)}")
    for reason, n in Counter(rt.values()).most_common():
        print(f"    {reason:<34} {n}")
    if observed:
        print(f"  static-only (reached via an untaken call; no runtime "
              f"evidence): {len(so)}")
        for reason, n in Counter(so.values()).most_common():
            print(f"    {reason:<34} {n}")
    print(f"  CLOSURE COMPLETE (runtime): {rep['closure_complete']}"
          + ("" if not observed else
             f"   (static-strict: {rep['static_closure_complete']})"))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(rep, indent=1), encoding="utf-8")
        print(f"  wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
