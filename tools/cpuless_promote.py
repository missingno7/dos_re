"""cpuless_promote.py -- promote functions from the recovery IR into CPUless
recovered Python + generated CPU-ABI adapters (M3 vertical slice).

For every candidate the STRICT first-subset gate runs (no calls, no
interrupts, no boundary/dispatch addresses, no indirect transfers, no segment
writes, no stack traffic, no flag live-ins, emitter-supported ops only) and a
full dry-run emission; anything that does not pass REFUSES with a named
reason.  With --apply, each promoted function produces:

    <recovered-dir>/func_CCCC_IIII.py    the recovered implementation
                                         (pure Python, no imports, no CPU
                                         object; semantic outputs only --
                                         timing/flags ride the hidden compat
                                         channel for the adapter)
    <adapter-dir>/lifted_CCCC_IIII.py    the generated CPU-ABI adapter that
                                         REPLACES the literal lifted module
                                         (one implementation: the recovered
                                         body is authoritative)

This step runs AFTER liftemit/liftlink in the pipeline; regenerating the
lifted corpus and re-running this tool reproduces the same promotion set.

Usage (from a port):
    python dos_re/tools/cpuless_promote.py --ir artifacts/lift/recovery_ir.json \
        --recovered-dir mygame/recovered --adapter-dir mygame/lifted/functions \
        --import-base mygame.recovered \
        --exclude @artifacts/lift/boundary_heads.txt \
        --exclude @artifacts/lift/dispatch_entries.txt \
        [--limit N] [--apply]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dos_re.lift.ir import scan_from_ir_record  # noqa: E402
from dos_re.lift.cpuless import abi_scan  # noqa: E402
from dos_re.lift import emit_cpuless  # noqa: E402


def _read_addr_file(path: Path) -> set[tuple[int, int]]:
    out: set[tuple[int, int]] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        cs, ip = line.split(":")
        out.add((int(cs, 16), int(ip, 16)))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ir", required=True)
    ap.add_argument("--recovered-dir", required=True)
    ap.add_argument("--adapter-dir", required=True)
    ap.add_argument("--import-base", required=True,
                    help="python package the adapters import the recovered "
                         "functions from (e.g. mygame.recovered)")
    ap.add_argument("--exclude", action="append", default=[],
                    help="@FILE of CS:IP addresses (boundary heads, dispatch "
                         "entries) whose functions must not promote")
    ap.add_argument("--entries", default="",
                    help="comma-separated CS:IP candidates (default: all)")
    ap.add_argument("--limit", type=int, default=0,
                    help="promote at most N functions (0 = no limit)")
    ap.add_argument("--apply", action="store_true",
                    help="write the generated files (default: dry-run census)")
    ap.add_argument("--census-out", default=None)
    args = ap.parse_args(argv)

    ir = json.loads(Path(args.ir).read_text(encoding="utf-8"))
    excluded: set[tuple[int, int]] = set()
    for spec in args.exclude:
        excluded |= _read_addr_file(Path(spec.lstrip("@")))

    wanted = ([e.strip().upper() for e in args.entries.split(",") if e.strip()]
              or sorted(ir["functions"]))

    # FIXPOINT over the call DAG (tier 4, call-ABI composition): each round
    # promotes every candidate whose direct near callees are all already
    # promoted; the callee contracts feed the callers' gates and emitters.
    promoted: list[str] = []
    refused: dict[str, list[str]] = {}
    outputs: dict[str, tuple[str, str]] = {}
    contracts: dict[int, emit_cpuless.CalleeContract] = {}
    done: set[str] = set()
    rounds = 0
    while True:
        rounds += 1
        refused = {}
        progress = False
        for key in wanted:
            if key in done:
                continue
            rec = ir["functions"][key]
            cs = int(key.split(":")[0], 16)
            excl_ips = {ip for (xcs, ip) in excluded if xcs == cs}
            try:
                if not rec.get("liftable", True):
                    raise emit_cpuless.Refusal("ir-not-liftable")
                scan = scan_from_ir_record(rec)
                abi, exit_flags = emit_cpuless.check_promotable(
                    scan, excluded_addrs=excl_ips, callees=contracts)
                recovered_src = emit_cpuless.emit_recovered(
                    scan, abi, key, callees=contracts,
                    recovered_import_base=args.import_base)
                adapter_src = emit_cpuless.emit_adapter(
                    scan, abi, key,
                    signature=bytes.fromhex(rec["signature"]),
                    recovered_import_base=args.import_base)
            except emit_cpuless.Refusal as e:
                refused.setdefault(str(e), []).append(key)
                continue
            promoted.append(key)
            done.add(key)
            outputs[key] = (recovered_src, adapter_src)
            contracts[scan.entry] = emit_cpuless.CalleeContract(
                name=f"func_{key.replace(':', '_').lower()}",
                inputs=tuple(emit_cpuless._contract_inputs(scan, abi)),
                outputs=tuple(sorted((abi.outputs - {"sp"})
                                     & (frozenset(emit_cpuless.W16)
                                        | frozenset({"ds", "es"})))),
                exit_flags=exit_flags)
            progress = True
            if args.limit and len(promoted) >= args.limit:
                progress = False
                break
        if not progress:
            break
    print(f"fixpoint reached after {rounds} round(s)")

    print(f"cpuless promotion census ({len(wanted)} candidates):")
    print(f"  promotable                     {len(promoted):4d}")
    for reason, keys in sorted(refused.items(), key=lambda kv: -len(kv[1])):
        print(f"  refused: {reason:<28} {len(keys):4d}")
    if promoted:
        print("promoted set: " + ", ".join(promoted[:16])
              + (" ..." if len(promoted) > 16 else ""))

    if args.census_out:
        out = Path(args.census_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "_notice": "GENERATED by dos_re tools/cpuless_promote.py -- "
                       "regenerate, do not hand-edit.",
            "promotable": promoted,
            "refused": {k: sorted(v) for k, v in sorted(refused.items())},
        }, indent=1), encoding="utf-8")
        print(f"wrote {out}")

    if args.apply and promoted:
        rec_dir = Path(args.recovered_dir)
        ad_dir = Path(args.adapter_dir)
        rec_dir.mkdir(parents=True, exist_ok=True)
        for key in promoted:
            rec_src, ad_src = outputs[key]
            stem = key.replace(":", "_").lower()
            (rec_dir / f"func_{stem}.py").write_text(rec_src, encoding="utf-8",
                                                     newline="\n")
            (ad_dir / f"lifted_{stem}.py").write_text(ad_src, encoding="utf-8",
                                                      newline="\n")
        print(f"APPLIED: {len(promoted)} recovered function(s) -> {rec_dir}; "
              f"adapters occupy their lifted slots in {ad_dir}.")
    elif args.apply:
        print("APPLIED: nothing promotable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
