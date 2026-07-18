"""Which refusal is actually WORTH fixing -- root causes, not raw counts.

The wall's exception list ranks tiers by how many functions carry them, and
that ranking is misleading.  `leaf-only:call` is the largest tier, but it is
not a shape this emitter cannot handle: it fires when a near call's target is
not yet a core, i.e. the CALLEE was refused.  Those functions are blocked by
somebody else's problem and need no work of their own.

This separates the two:

  ROOT     the function's own shape is unsupported (stack-addressed memory,
           a return-address touch, an observer frame ...).  Real work.
  CASCADE  refused only because something it calls is refused.  Free the
           moment its blockers clear.

and then, for each root cause, computes the UNLOCK: how many cascade-refused
functions would become emittable if that one tier were supported.  That is the
number worth sorting by -- it says which tier buys the most corpus.

Usage:
    python tools/abi_blockers.py --ir <recovery_ir.json>
                                 --manifest <cores_manifest.json>
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

#: refusals that mean "blocked by a callee", not "I cannot do this shape"
CASCADE = {"leaf-only:call", "leaf-only:call_far", "leaf-only:call_ind",
           "call-composition-cycle", "callee-needs-ss-segment"}


def _key(cs: int, ip: int) -> str:
    return "%04X:%04X" % (cs, ip)


def analyse(ir: dict, manifest: dict) -> dict:
    cores = set(manifest.get("cores", ()))
    refused = dict(manifest.get("refused", {}))

    # near+far callee edges, keyed the same way the manifest is
    callees: dict[str, set] = defaultdict(set)
    for key, fn in ir["functions"].items():
        cs = int(key.split(":")[0], 16)
        # the IR stores targets as hex STRINGS ("031E"), far ones as "CS:IP"
        for t in fn.get("calls_near", ()):
            callees[key].add(_key(cs, int(t, 16)))
        for t in fn.get("calls_far", ()):
            if isinstance(t, str) and ":" in t:
                callees[key].add(t.upper())
            elif isinstance(t, (list, tuple)) and len(t) == 2:
                callees[key].add(_key(int(t[0], 16) if isinstance(t[0], str)
                                      else t[0],
                                      int(t[1], 16) if isinstance(t[1], str)
                                      else t[1]))

    roots = {k: r for k, r in refused.items() if r not in CASCADE}
    cascades = {k: r for k, r in refused.items() if r in CASCADE}

    # A cascade function is unblocked when every callee is a core.  Solving a
    # tier makes its ROOT members emittable; that can unblock cascades, which
    # can unblock further cascades -- so iterate to a fixpoint per tier.
    unlock: dict[str, int] = {}
    tiers = sorted(set(roots.values()))
    for tier in tiers:
        emitted = set(cores) | {k for k, r in roots.items() if r == tier}
        changed = True
        while changed:
            changed = False
            for k in list(cascades):
                if k in emitted:
                    continue
                if all(c in emitted or c not in refused for c in callees[k]):
                    emitted.add(k)
                    changed = True
        direct = sum(1 for r in roots.values() if r == tier)
        unlock[tier] = {"root_functions": direct,
                        "total_emittable_gain": len(emitted) - len(cores)}

    return {"cores": len(cores), "roots": len(roots), "cascades": len(cascades),
            "tiers": unlock,
            "cascade_tiers": {t: sum(1 for r in cascades.values() if r == t)
                              for t in sorted(set(cascades.values()))}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ir", required=True)
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args(argv)

    ir = json.loads(Path(args.ir).read_text(encoding="utf-8"))
    man = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    rep = analyse(ir, man)

    print(f"cores {rep['cores']}   root-refused {rep['roots']}   "
          f"cascade-refused {rep['cascades']}")
    print("\nCASCADE tiers (no work of their own -- freed by their callees):")
    for t, n in sorted(rep["cascade_tiers"].items(), key=lambda kv: -kv[1]):
        print(f"  {t:<34} {n:4d}")
    print("\nROOT tiers, by how much corpus each one BUYS:")
    print(f"  {'tier':<34} {'own':>5} {'+cascades unlocked':>20}")
    for t, d in sorted(rep["tiers"].items(),
                       key=lambda kv: -kv[1]["total_emittable_gain"]):
        print(f"  {t:<34} {d['root_functions']:5d} "
              f"{d['total_emittable_gain']:20d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
