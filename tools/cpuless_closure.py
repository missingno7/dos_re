"""cpuless_closure.py -- runtime-closure measurement for CPU-independent regions.

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


def _promoted_ranges(ir: dict, promoted: set[str]):
    """[(seg, start, end, key)] for each PROMOTED IR function -- the byte span
    its recovered body covers, so a resume address inside it can be recognised."""
    out = []
    for key, fn in ir["functions"].items():
        if key.upper() not in promoted:
            continue
        seg = int(key.split(":")[0], 16)
        last = None
        start = None
        for blk in fn["blocks"]:
            for i in blk["instructions"]:
                off = int(i["ip"], 16)
                start = off if start is None else min(start, off)
                end = off + len(bytes.fromhex(i["bytes"]))
                last = end if last is None else max(last, end)
        if start is not None:
            out.append((seg, start, last, key.upper()))
    return out


def _containing_promoted(key: str, prom_ranges) -> str | None:
    """The promoted function whose byte span contains ``key`` (an arbitrary
    CS:IP), or None. A resume/head address served by that function's recovered
    body is covered, not a frontier gap."""
    seg = int(key.split(":")[0], 16)
    off = int(key.split(":")[1], 16)
    for s, start, end, fkey in prom_ranges:
        if s == seg and start <= off < end and fkey != key:
            return fkey
    return None


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
    # A resume/head address (a boundary head, a snapshot entry, a dispatch
    # arrival) is NOT its own IR function -- it is an offset INSIDE one. When
    # that containing function is promoted, its recovered body serves the resume
    # (the plat.boundary observer / dispatch registry fires there), so the
    # address is COVERED, not a frontier gap. Without this, feeding resume points
    # as roots reports every one as a spurious "not-in-ir" frontier item.
    prom_ranges = _promoted_ranges(ir, promoted)
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
        container = _containing_promoted(key, prom_ranges) if rec is None else None
        if key not in promoted and container is None:
            # a frontier node -- record WHY, but still follow its statically
            # known near-call targets so the WHOLE reachable graph (and the
            # real frontier depth) is measured, not just the first gap.
            frontier[key] = refusals.get(key, "not-in-ir" if not rec else "not-promoted")
        if not rec:
            if container is not None:
                # covered by a promoted function's recovered body -- walk that
                # function's graph so its own callees are measured too.
                work.append(container)
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


# ---------------------------------------------------------------------------
# The capture<->close FIXPOINT (generic graph-completeness).
#
# walk_closure measures REACHABILITY; it does not answer "how much of the
# reachable set is CPUless-COMPOSABLE", which is a second, coupled fixpoint: a
# reachable caller composes only when every callee it reaches -- near call,
# static far call, AND every observed dynamic-dispatch target -- is itself
# composable. The two loops must run together, because the caller's dispatch
# targets are reached ONLY by following the observed evidence captured at its
# indirect sites (the capture side, port-supplied). A single interpreter capture
# already observes every site that fires and every target it resolves to, so the
# fixpoint iterates purely over the fixed evidence -- no re-capture is needed for
# completeness; "re-capture -> re-close" collapses to a static joint fixpoint.
#
# The subtle, generalisable part is CYCLES. A message-pump dispatch cluster
# (MAINWNDPROC / WINMAIN / the timer driver / the jump-table arms that select
# each other) is a strongly-connected component in the callee graph: no member
# bottoms out first, so a naive "compose when all callees compose" fixpoint
# deadlocks on it forever. But the whole component composes ATOMICALLY when every
# edge LEAVING it lands on a composable target -- runtime dispatch resolution is
# lazy, so the intra-component references are legal by construction. Condensing
# the reachable callee graph into SCCs and promoting a component all-or-nothing
# is exactly that rule, and it generalises far beyond any one game.
# ---------------------------------------------------------------------------

def _reach_edges(ir, roots, prom_ranges, dyn_evidence):
    """Reachability worklist that also records, per reached function, the set of
    direct callee keys it composes through: near calls, static (9A) far calls,
    and every OBSERVED dynamic-dispatch target that is a distinct IR function (an
    intra-function jump-table landing stays inside the caller and is not an
    edge). Returns (reached, edges, resume_cover) -- resume_cover maps a non-IR
    address served by a promoted function's byte span to that container."""
    reached: set[str] = set()
    edges: dict[str, set[str]] = {}
    resume_cover: dict[str, str] = {}
    functions = ir["functions"]
    work = list(roots)
    while work:
        key = work.pop().upper()
        if key in reached:
            continue
        reached.add(key)
        callees = edges.setdefault(key, set())
        rec = functions.get(key)
        if rec is None:
            container = _containing_promoted(key, prom_ranges)
            if container is not None:
                resume_cover[key] = container.upper()
                callees.add(container.upper())
                work.append(container.upper())
            continue
        cs = int(key.split(":")[0], 16)
        for blk in rec["blocks"]:
            for i in blk["instructions"]:
                if i["mnemonic"] == "call" and "target" in i:
                    t = f"{cs:04X}:{int(i['target'], 16):04X}"
                    callees.add(t); work.append(t)
                elif i.get("kind") == "call_far":
                    by = bytes.fromhex(i["bytes"])
                    if by and by[0] == 0x9A:
                        off = by[1] | (by[2] << 8)
                        seg = by[3] | (by[4] << 8)
                        t = f"{seg:04X}:{off:04X}"
                        callees.add(t); work.append(t)
                elif i.get("kind") in ("call_ind", "jmp_ind"):
                    site = f"{cs:04X}:{int(i['ip'], 16):04X}"
                    for tgt in dyn_evidence.get(site, ()):
                        tt = tgt.upper()
                        if tt in functions:      # a distinct callee, not an
                            callees.add(tt)      # intra-function landing
                            work.append(tt)
    return reached, edges, resume_cover


def _sccs(nodes, edges):
    """Tarjan strongly-connected components over the reachable callee graph.
    Returns a list of components (each a set of keys) in reverse-topological
    order (a component appears before any component that depends on it)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    out: list[set[str]] = []
    counter = [0]
    # iterative Tarjan (the graph can be deep -- avoid Python recursion limits).
    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(sorted(edges.get(root, ()))))]
        index[root] = low[root] = counter[0]; counter[0] += 1
        stack.append(root); on_stack.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for succ in it:
                if succ not in nodes:
                    continue
                if succ not in index:
                    index[succ] = low[succ] = counter[0]; counter[0] += 1
                    stack.append(succ); on_stack.add(succ)
                    work.append((succ, iter(sorted(edges.get(succ, ())))))
                    advanced = True
                    break
                if succ in on_stack:
                    low[node] = min(low[node], index[succ])
            if advanced:
                continue
            if low[node] == index[node]:
                comp: set[str] = set()
                while True:
                    w = stack.pop(); on_stack.discard(w); comp.add(w)
                    if w == node:
                        break
                out.append(comp)
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
    return out


def composable_closure(ir, roots, *, promoted, body_clean, resolved=frozenset(),
                       dyn_evidence=None, observed=None, refusals=None) -> dict:
    """How much of the runtime-reachable closure is CPUless-COMPOSABLE -- the
    capture<->close joint fixpoint, with atomic strongly-connected-component
    promotion.

    ``promoted``    -- recovered/promoted function keys (a composable base, and
                       the source of resume-address coverage).
    ``body_clean``  -- IR function keys whose OWN body passes the promotion gate
                       (leaf, or calls-only, or an evidence-resolved dispatch):
                       they compose iff every callee composes.
    ``resolved``    -- extra callee keys that compose WITHOUT an IR body (a
                       platform-boundary far-call/interrupt the emitter models as
                       a plat effect, a manual override). Consumer-supplied --
                       dos_re hardcodes no boundary segment.
    ``dyn_evidence``-- per-site observed dynamic-dispatch targets (the capture),
                       so the walk and the composition follow indirect edges.
    ``observed``    -- executed function entries: splits the residual frontier
                       into runtime-reachable vs static-only (reported, not
                       dropped).

    A component composes when every one of its members is a composable base or
    body_clean AND every edge leaving the component lands on a composable
    component. Cyclic dispatch clusters therefore promote all-or-nothing. The
    residual frontier tags each blocked function with its own hard refusal, or
    ``blocked-by-callee`` when it is body-clean but a callee never composes."""
    dyn_evidence = dyn_evidence or {}
    refusals = refusals or {}
    promoted = {k.upper() for k in promoted}
    body_clean = {k.upper() for k in body_clean}
    resolved = {k.upper() for k in resolved}
    prom_ranges = _promoted_ranges(ir, promoted)
    roots = [r.upper() for r in roots]
    reached, edges, resume_cover = _reach_edges(ir, roots, prom_ranges,
                                                dyn_evidence)
    functions = ir["functions"]

    def is_base(k: str) -> bool:
        # composable with no dependence on callees: already promoted, an
        # explicitly-resolved platform/manual target, or a resume address served
        # by a promoted container.
        return k in promoted or k in resolved or k in resume_cover

    # A node can EVER compose only if it is a base or a clean IR body; anything
    # else (a hard-blocked body, an unresolved non-IR external target) is a wall.
    def can_compose_alone_or_via_callees(k: str) -> bool:
        return is_base(k) or (k in functions and k in body_clean)

    comps = _sccs(reached, edges)
    comp_of: dict[str, int] = {}
    for ci, comp in enumerate(comps):
        for k in comp:
            comp_of[k] = ci
    comp_composable: list[bool] = [False] * len(comps)
    # reverse-topological order: _sccs already yields components before their
    # dependents, so a single pass suffices (no iteration to a fixpoint needed --
    # the condensation is a DAG). This IS the fixpoint, converged by construction.
    for ci, comp in enumerate(comps):
        if all(is_base(k) for k in comp):
            comp_composable[ci] = True
            continue
        if not all(can_compose_alone_or_via_callees(k) for k in comp):
            comp_composable[ci] = False
            continue
        ok = True
        for k in comp:
            if is_base(k):
                continue
            for c in edges.get(k, ()):
                cj = comp_of.get(c)
                if cj is None:            # edge outside the reached graph
                    ok = False; break
                if cj != ci and not comp_composable[cj]:
                    ok = False; break     # a leaving edge lands on a wall
            if not ok:
                break
        comp_composable[ci] = ok

    composable = {k for ci, comp in enumerate(comps) if comp_composable[ci]
                  for k in comp}
    frontier: dict[str, str] = {}
    for k in reached:
        if k in composable:
            continue
        if not can_compose_alone_or_via_callees(k):
            frontier[k] = refusals.get(k, "not-in-ir" if k not in functions
                                       else "body-blocked")
        else:
            # body-clean but a callee (possibly transitively) never composes.
            blocker = next((c for c in sorted(edges.get(k, ()))
                            if c not in composable), None)
            frontier[k] = f"blocked-by-callee:{blocker}" if blocker \
                else "blocked-by-callee"

    observed = {a.upper() for a in (observed or ())}
    runtime_frontier = {k: v for k, v in frontier.items()
                        if not observed or k in observed}
    static_only = {k: v for k, v in frontier.items()
                   if observed and k not in observed}
    max_scc = max((len(c) for c in comps), default=0)
    cyclic = [sorted(c) for c in comps if len(c) > 1]
    return {
        "roots": roots,
        "reached": len(reached),
        "promoted_reached": len(reached & promoted),
        "composable": len(composable),
        "composable_keys": sorted(composable),
        "frontier": dict(sorted(frontier.items())),
        "runtime_frontier": dict(sorted(runtime_frontier.items())),
        "static_only_frontier": dict(sorted(static_only.items())),
        "closure_complete": not runtime_frontier,
        "static_closure_complete": not frontier,
        "scc_count": len(comps),
        "max_scc_size": max_scc,
        "cyclic_components": cyclic,
        "converged": True,
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
    ap.add_argument("--fixpoint", action="store_true",
                    help="run the capture<->close COMPOSABILITY fixpoint "
                         "(composable_closure) instead of the reachability walk: "
                         "reports how much of the reachable set is CPUless-"
                         "composable, promoting cyclic dispatch clusters "
                         "atomically. Needs --body-clean-census.")
    ap.add_argument("--body-clean-census", default=None,
                    help="a classify_corpus census JSON: body_clean = the leaf + "
                         "calls-only tiers (functions whose OWN body passes the "
                         "gate; they compose iff their callees do)")
    ap.add_argument("--resolved", default=None,
                    help="@FILE of CS:IP keys that compose WITHOUT an IR body "
                         "(platform-boundary far-call/interrupt targets, manual "
                         "overrides). Consumer-supplied -- dos_re hardcodes none.")
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

    from collections import Counter
    if args.fixpoint:
        body_clean: set[str] = set()
        if args.body_clean_census and Path(args.body_clean_census).is_file():
            tiers = json.loads(Path(args.body_clean_census).read_text(
                encoding="utf-8")).get("tiers", {})
            for t in ("leaf", "calls-only"):
                body_clean |= {k.upper() for k in tiers.get(t, ())}
        resolved: set[str] = set()
        if args.resolved and Path(args.resolved.lstrip("@")).is_file():
            for line in Path(args.resolved.lstrip("@")).read_text(
                    encoding="utf-8").splitlines():
                line = line.split("#", 1)[0].strip()
                if line:
                    resolved.add(line.upper())
        rep = composable_closure(
            ir, roots, promoted=promoted, body_clean=body_clean,
            resolved=resolved, dyn_evidence=dyn_evidence, observed=observed,
            refusals=refusals)
        print(f"CPUless COMPOSABILITY fixpoint from roots {roots}:")
        print(f"  reachable functions:   {rep['reached']}")
        print(f"  composable (CPUless):  {rep['composable']}")
        print(f"  SCCs: {rep['scc_count']}  max SCC size: {rep['max_scc_size']}")
        rt = rep["runtime_frontier"]
        print(f"  frontier (runtime-reachable, not composable): {len(rt)}")
        for reason, n in Counter(rt.values()).most_common(12):
            print(f"    {reason:<34} {n}")
        if observed:
            print(f"  static-only frontier: {len(rep['static_only_frontier'])}")
        print(f"  CLOSURE COMPLETE (runtime): {rep['closure_complete']}"
              f"  converged: {rep['converged']}")
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(json.dumps(rep, indent=1), encoding="utf-8")
            print(f"  wrote {args.out}")
        return 0

    rep = walk_closure(ir, roots, promoted, refusals, dyn_evidence, observed)
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
