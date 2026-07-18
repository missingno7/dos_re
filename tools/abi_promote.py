"""abi_promote.py -- generate ABI-recovered modules from the M3b contract
census (dos_re.lift.emit_abi, slice 1).

For every contract-promotable census entry with proven-unobserved outputs
(the poison-proof set) -- or every promotable entry with --all -- emit the
dual-entrypoint module (public ABI-recovered entry + contract-proof shadow)
plus the generated shadow loader.  Substituting the shadows into the
recovered graph and replaying the canonical demo through the acceptance
gate proves the narrowed contracts end to end against the oracle.

Usage:
    python dos_re/tools/abi_promote.py \
        --census artifacts/abi/contract_census.json \
        --import-base lemmings.recovered --abi-base lemmings.recovered_abi \
        --out-dir lemmings/recovered_abi [--all] [--apply]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dos_re.lift import emit_abi  # noqa: E402
from dos_re.lift.emit_cpuless import Refusal  # noqa: E402


def _addr_file(spec: str | None) -> frozenset:
    if not spec:
        return frozenset()
    out = set()
    for line in Path(spec.lstrip("@")).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            cs, ip = line.split(":")
            out.add((int(cs, 16), int(ip, 16)))
    return frozenset(out)


def _emit_cores(args, census, wanted) -> int:
    """Slice 2: emit the de-stacked ABI core for every destackable leaf."""
    from dos_re.lift.contracts import scan_for

    if not args.ir:
        raise SystemExit("--cores requires --ir")
    ir = json.loads(Path(args.ir).read_text(encoding="utf-8"))
    heads = _addr_file(args.boundary_heads)
    disp = _addr_file(args.dispatch_entries)
    wanted_set = set(wanted)
    cores: dict[str, str] = {}
    contracts: dict[str, emit_abi.CoreContract] = {}   # key -> its contract
    refused: dict[str, str] = {}

    def _name(prop):
        for n in prop.get("notes", ()):
            if n.startswith("name: "):
                return n.split()[1]
        return None

    # BOTTOM-UP FIXPOINT (mirrors cpuless_promote): a function emits once
    # every near-call target it needs is already an ABI core.  A target that
    # never emits (leaf-refused, or outside the wanted set) leaves its
    # callers refused too -- reported, never silently degraded.
    rounds = 0
    while True:
        rounds += 1
        progress = False
        for key in wanted:
            if key in cores or key in refused:
                continue
            prop = census["functions"][key]
            if prop["refusals"]:
                refused[key] = "contract-not-promotable"
                progress = True
                continue
            scan, why = scan_for(ir["functions"][key])
            if scan is None:
                refused[key] = why
                progress = True
                continue
            cs = int(key.split(":")[0], 16)
            # near-call targets that are already ABI cores compose; a target
            # not yet emitted (and not yet refused) defers this function
            near = [i.target for i in scan.insts.values()
                    if i.kind == emit_abi.CALL and i.target is not None]
            far = [i.far_target for i in scan.insts.values()
                   if i.kind == emit_abi.CALL_FAR and i.far_target is not None]
            callee_map, far_map = {}, {}
            deferred = False
            for t in near:
                tkey = f"{cs:04X}:{t:04X}"
                if tkey in contracts:
                    callee_map[t] = contracts[tkey]
                elif tkey in refused or tkey not in wanted_set \
                        or tkey not in census["functions"]:
                    callee_map = None            # a callee will never be a core
                    break
                else:
                    deferred = True
            if callee_map is not None:
                # a direct FAR call composes exactly like a near one in the
                # ABI form (no return-address frame at all); the contract is
                # keyed by the static (segment, offset) target.
                for ft in far:
                    fkey = "%04X:%04X" % ft
                    if fkey in contracts:
                        far_map[ft] = contracts[fkey]
                    elif fkey in refused or fkey not in wanted_set \
                            or fkey not in census["functions"]:
                        callee_map = None
                        break
                    else:
                        deferred = True
            if callee_map is None:
                # emit anyway so check_composable names the exact refusal
                callee_map = {t: contracts[f"{cs:04X}:{t:04X}"]
                              for t in near
                              if f"{cs:04X}:{t:04X}" in contracts}
                far_map = {ft: contracts["%04X:%04X" % ft] for ft in far
                           if "%04X:%04X" % ft in contracts}
            elif deferred:
                continue                         # wait for callees this round
            try:
                src, contract = emit_abi.emit_abi_core(
                    scan, prop, key, name=_name(prop),
                    callees=callee_map, far_callees=far_map,
                    abi_base=args.abi_base,
                    boundary_addrs={ip for (hc, ip) in heads if hc == cs},
                    dispatch_addrs={ip for (hc, ip) in disp if hc == cs})
                cores[key] = src
                contracts[key] = contract
            except Refusal as e:
                refused[key] = str(e)
            progress = True
        if not progress:
            break
    # any still-undecided function was blocked on a deferred callee cycle
    for key in wanted:
        if key not in cores and key not in refused:
            refused[key] = "call-composition-cycle"

    print(f"de-stacked ABI core emission over {len(wanted)} candidates "
          f"(fixpoint: {rounds} rounds):")
    print(f"  cores emitted  {len(cores):4d}")
    from collections import Counter
    print("  kept mechanical (next-tier work list):")
    for reason, n in Counter(refused.values()).most_common():
        print(f"    {reason:<44} {n:4d}")
    if args.apply and cores:
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        # PRUNE first: a core file left over from an earlier run whose
        # function is now REFUSED would linger on disk and could still be
        # imported by a sibling core -- silently shadowing the refusal.  The
        # emitted set is the whole truth, so the directory must match it.
        keep = {f"core_{emit_abi._stem(k)}.py" for k in cores}
        stale = [p for p in out.glob("core_*.py") if p.name not in keep]
        for p in stale:
            p.unlink()
        if stale:
            print(f"  pruned {len(stale)} stale core module(s): "
                  + ", ".join(sorted(p.name for p in stale)[:6])
                  + (" ..." if len(stale) > 6 else ""))
        for key, src in sorted(cores.items()):
            (out / f"core_{emit_abi._stem(key)}.py").write_text(
                src, encoding="utf-8")
        manifest = {"_notice": "GENERATED by dos_re tools/abi_promote.py "
                               "--cores. Regenerate, do not hand-edit.",
                    "cores": sorted(cores),
                    # the CLASSIFIED EXCEPTION list: every function kept
                    # mechanical, with the exact capability that blocked it.
                    # tools/abi_gate.py reports these as the classes that
                    # still owe a generated representation.
                    "refused": {k: v for k, v in sorted(refused.items())}}
        (out / "cores_manifest.json").write_text(
            json.dumps(manifest, indent=1), encoding="utf-8")
        print(f"wrote {len(cores)} core modules + cores_manifest.json "
              f"to {out}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--census", required=True,
                    help="contract_census.json (dos_re.lift.contracts)")
    ap.add_argument("--import-base", required=True,
                    help="package of the mechanical recovered modules")
    ap.add_argument("--abi-base", required=True,
                    help="package the ABI modules are imported as")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--all", action="store_true",
                    help="emit every promotable contract (default: only "
                         "those with dropped outputs -- the poison-proof "
                         "set)")
    ap.add_argument("--cores", action="store_true",
                    help="slice 2: emit DE-STACKED ABI cores "
                         "(core_CCCC_IIII.py) for every destackable leaf; "
                         "requires --ir; prints the destack refusal census "
                         "(the slice-3 work list)")
    ap.add_argument("--ir", default=None,
                    help="recovery_ir.json (required for --cores)")
    ap.add_argument("--boundary-heads", default=None,
                    help="@FILE of boundary-head CS:IP addresses (a park-"
                         "carrying function stays mechanical)")
    ap.add_argument("--dispatch-entries", default=None,
                    help="@FILE of dynamic-arrival CS:IP addresses (an "
                         "alt-entry function stays mechanical)")
    ap.add_argument("--entries", default="",
                    help="comma-separated CS:IP subset (bisection aid)")
    ap.add_argument("--apply", action="store_true",
                    help="write the generated files (default: dry run)")
    args = ap.parse_args(argv)

    census = json.loads(Path(args.census).read_text(encoding="utf-8"))
    wanted = ([e.strip().upper() for e in args.entries.split(",")
               if e.strip()] or sorted(census["functions"]))

    if args.cores:
        return _emit_cores(args, census, wanted)

    emitted: dict[str, str] = {}
    skipped: dict[str, str] = {}
    for key in wanted:
        prop = census["functions"][key]
        if prop["refusals"]:
            skipped[key] = "refused: " + ",".join(
                r["reason"] for r in prop["refusals"])
            continue
        if not args.all and not prop["dropped_outputs"]:
            skipped[key] = "no-dropped-outputs"
            continue
        name = None
        for n in prop.get("notes", ()):
            if n.startswith("name: "):
                name = n.split()[1]
        try:
            emitted[key] = emit_abi.emit_abi_module(
                key, prop, import_base=args.import_base, name=name)
        except Refusal as e:
            skipped[key] = str(e)

    print(f"ABI-recovered emission over {len(wanted)} candidates:")
    print(f"  emitted  {len(emitted):4d}")
    from collections import Counter
    for reason, n in Counter(skipped.values()).most_common():
        print(f"  skipped: {reason:<40} {n:4d}")

    if args.apply and emitted:
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        for key, src in sorted(emitted.items()):
            (out / f"abi_{emit_abi._stem(key)}.py").write_text(
                src, encoding="utf-8")
        (out / "shadow_loader.py").write_text(
            emit_abi.emit_shadow_loader(sorted(emitted),
                                        abi_base=args.abi_base,
                                        import_base=args.import_base),
            encoding="utf-8")
        (out / "__init__.py").write_text(
            '"""AUTOGENERATED by dos_re tools/abi_promote.py -- '
            'ABI-recovered contract modules (M3b slice 1).  Regenerate, '
            'do not hand-edit."""\n', encoding="utf-8")
        print(f"wrote {len(emitted)} modules + shadow_loader to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
