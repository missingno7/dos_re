"""contract_census.py -- the M3b ABI-contract census over a recovery IR.

Runs the ABI-contract inference (dos_re.lift.contracts) over every censused
function: proposed real parameters (from register/stack live-ins),
caller-observed return values (interprocedural exit-liveness narrowing),
stack-argument evidence, pointer-pair evidence, and structured refusals --
the ABI-recovered CPUless work list (docs/history/dos_re_2.0.md, Stage 2b / M3b).

Externally-reachable functions (roots, dynamic-dispatch targets, vectored
handlers) keep conservative return sets; supply them so the narrowing never
drops an output something outside the static graph observes:

Usage:
    python dos_re/tools/contract_census.py --ir artifacts/lift/recovery_ir.json \
        --roots 1010:0000 --dyn-evidence artifacts/lift/indirect_sites.json \
        --vector-evidence artifacts/lift/vector_sites.json \
        --names lemmings/recovery_facts/recovery_facts.json \
        --out artifacts/abi/contract_census.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dos_re.lift.contracts import infer_contracts  # noqa: E402


def _evidence_targets(path: str | None, field: str) -> set[str]:
    """Observed dynamic targets/vectors from a probe-evidence JSON."""
    out: set[str] = set()
    if path and Path(path).is_file():
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        for site in doc.get("sites", []):
            out |= {k.upper() for k in site.get(field, {})}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ir", required=True)
    ap.add_argument("--roots", default="",
                    help="comma-separated CS:IP roots (externally called)")
    ap.add_argument("--dyn-evidence", default=None,
                    help="indirect_sites.json: observed dynamic-dispatch "
                         "targets are externally reachable")
    ap.add_argument("--vector-evidence", default=None,
                    help="vector_sites.json: observed vectored handlers are "
                         "externally reachable")
    ap.add_argument("--boundary-heads", default=None,
                    help="@FILE of boundary-head CS:IP addresses: a park "
                         "digests the full register bundle, so its function "
                         "widens and its site observes everything")
    ap.add_argument("--dispatch-entries", default=None,
                    help="@FILE of dynamic-arrival CS:IP addresses: an "
                         "alt-entry function widens and its returns stay "
                         "conservative")
    ap.add_argument("--names", default=None,
                    help="recovery facts JSON with an optional "
                         "'function_names' {CS:IP: name} table (provenance "
                         "metadata; addresses remain the identity)")
    ap.add_argument("--ss-globals-floor", default=None,
                    help="offset in the STACK SEGMENT below which ss-relative "
                         "accesses are the program's own globals rather than "
                         "stack (hex or decimal).  A per-program layout fact "
                         "-- boot sp, memory model, how far the stack grows -- "
                         "so dos_re has no default: omit it and the ss-globals "
                         "tier simply does not apply.  Supply it with evidence "
                         "for the observed global range and the minimum "
                         "reachable sp.")
    ap.add_argument("--out", default=None,
                    help="write the full census JSON here")
    args = ap.parse_args(argv)

    ir = json.loads(Path(args.ir).read_text(encoding="utf-8"))
    external = {r.strip().upper() for r in args.roots.split(",") if r.strip()}
    external |= _evidence_targets(args.dyn_evidence, "targets")
    external |= _evidence_targets(args.vector_evidence, "vectors")
    names: dict[str, str] = {}
    if args.names and Path(args.names).is_file():
        names = {k.upper(): v for k, v in json.loads(
            Path(args.names).read_text(encoding="utf-8")).get(
                "function_names", {}).items()}

    def addr_file(spec: str | None) -> frozenset:
        if not spec:
            return frozenset()
        out = set()
        for line in Path(spec.lstrip("@")).read_text(
                encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                cs, ip = line.split(":")
                out.add((int(cs, 16), int(ip, 16)))
        return frozenset(out)

    floor = (int(str(args.ss_globals_floor), 0)
             if args.ss_globals_floor is not None else None)
    census = infer_contracts(
        ir, external=frozenset(external), names=names,
        boundary_addrs=addr_file(args.boundary_heads),
        dispatch_addrs=addr_file(args.dispatch_entries),
        ss_globals_floor=floor)
    # record the premise IN the census, so a consumer can see which layout
    # fact the ss-globals proposals rest on rather than inferring it
    census["ss_globals_floor"] = floor

    s = census["summary"]
    print(f"M3b ABI-contract census over {s['total']} functions:")
    print(f"  contract-promotable       {s['contract_promotable']:4d}")
    print(f"  returns narrowed          {s['returns_narrowed']:4d}")
    print(f"  dropped register outputs  {s['dropped_output_total']:4d}")
    if s["refusal_counts"]:
        print("refusals (functions, ranked):")
        for reason, n in s["refusal_counts"].items():
            print(f"  {reason:<40} {n:4d}")
    framed = [k for k, f in census["functions"].items()
              if f.get("stack", {}).get("framed")]
    pairs = sum(1 for f in census["functions"].values() if f["pointer_pairs"])
    print(f"framed (stack-arg candidates): {len(framed)}; "
          f"functions with pointer-pair evidence: {pairs}")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(census, indent=1), encoding="utf-8")
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
