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


def _gate_dyn_evidence(scan, cs, dyn_evidence, done, dispatch_owner,
                       contracts_by_cs, iret_keys=frozenset()) -> None:
    """Evidence-gated dynamic dispatch (tier 9): a function containing
    near-indirect transfers promotes only when every OBSERVED runtime target
    of its sites (the canonical-demo probe evidence) is dispatchable --

      * an intra-function block leader (jump-table landing), or
      * an already-promoted NEAR-return function, or
      * a dispatch entry owned by a promoted function (alternate entry).

    A site with no observed targets promotes optimistically: the demo never
    executes it, and a live selector outside the registry raises the
    UnknownDispatchTarget witness -- never a fallback.  Refusals here retry
    every fixpoint round, so promotion order follows the evidence.

    An ISR-CHAIN site (far vector tail, tier 13) gates its observed vectors
    against the promoted IRET-handler set instead."""
    leaders = None
    for i in scan.insts.values():
        if emit_cpuless._is_isr_chain(i):
            site = f"{cs:04X}:{i.ip:04X}"
            for tgt in dyn_evidence.get(site, []):
                if tgt not in iret_keys:
                    raise emit_cpuless.Refusal("isr-chain-handler-unpromoted")
            continue
        if not emit_cpuless._is_dyn(i):
            continue
        site = f"{cs:04X}:{i.ip:04X}"
        for tgt in dyn_evidence.get(site, []):
            tcs, tip = (int(x, 16) for x in tgt.split(":"))
            if i.kind == "jmp_ind" and tcs == cs:
                if leaders is None:
                    leaders = set(scan.block_leaders())
                if tip in leaders:
                    continue                    # intra-function landing
            if tgt in dispatch_owner:
                continue                        # owned alternate entry
            if tgt in done:     # promoted, or tentative this round (cluster)
                c = contracts_by_cs.get(tcs, {}).get(tip)
                if c is not None and c.ret_kind != "near":
                    raise emit_cpuless.Refusal("dyn-target-not-near-return")
                if c is not None and (c.sp_output or c.ret_pop
                                      or c.sp_delta != 0):
                    # the dyn bundle assumes a stack-balanced callee
                    raise emit_cpuless.Refusal("dyn-target-sp-escape")
                if c is not None and c.flags_livein:
                    # the near-dyn bundle carries no full flags word
                    raise emit_cpuless.Refusal("dyn-target-needs-flags")
                continue
            raise emit_cpuless.Refusal("dyn-target-unpromoted")


def _gate_vector_evidence(scan, cs, vec_evidence, done, contracts_by_cs,
                          iret_keys) -> None:
    """Evidence-gated interrupt dispatch (tier 12): a function containing
    game-vectored INT sites promotes only when every OBSERVED runtime
    vector of its sites resolves to a promoted (or tentative-this-round)
    IRET-contract handler.  A site with no observed vectors promotes
    optimistically -- a live unknown vector raises the witness."""
    for i in scan.insts.values():
        if not emit_cpuless._is_game_int(i):
            continue
        site = f"{cs:04X}:{i.ip:04X}"
        for tgt in vec_evidence.get(site, []):
            if tgt in iret_keys:
                continue                    # promoted IRET handler
            if tgt in done:
                # promoted/tentative but NOT an iret handler -- wrong kind
                raise emit_cpuless.Refusal("int-handler-not-iret")
            raise emit_cpuless.Refusal("int-handler-unpromoted")


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
                    help="@FILE of CS:IP addresses (boundary heads) whose "
                         "functions must not promote")
    ap.add_argument("--dispatch-entries", default=None,
                    help="@FILE of recorded dynamic-arrival addresses "
                         "(recovery facts): each becomes an ALTERNATE ENTRY "
                         "of the recovered function containing it (tier 9)")
    ap.add_argument("--dyn-evidence", default=None,
                    help="indirect_sites.json (per-site dynamic-target "
                         "probe evidence): a function with dynamic transfers "
                         "promotes only when every OBSERVED target of its "
                         "sites is dispatchable (local leader, promoted "
                         "function, or owned dispatch entry)")
    ap.add_argument("--boundary-heads", default=None,
                    help="@FILE of boundary-head CS:IP addresses (tier 13): "
                         "a head inside a function becomes an emitted "
                         "plat.boundary observer; the function (and its "
                         "composed callers) become STANDALONE-ONLY -- the "
                         "recovered module is written, but the VMless demo "
                         "graph keeps the original lifted module")
    ap.add_argument("--vector-evidence", default=None,
                    help="vector_sites.json (game-vectored INT probe "
                         "evidence): a function with INT 60/61 sites "
                         "promotes only when every OBSERVED runtime vector "
                         "resolves to a promoted IRET-contract handler")
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
    dispatch_addrs: set[tuple[int, int]] = set()
    if args.dispatch_entries:
        dispatch_addrs = _read_addr_file(Path(args.dispatch_entries.lstrip("@")))
    boundary_addrs: set[tuple[int, int]] = set()
    if args.boundary_heads:
        boundary_addrs = _read_addr_file(Path(args.boundary_heads.lstrip("@")))
    # per-site dynamic-target evidence: "CS:IP" site -> [observed target keys]
    dyn_evidence: dict[str, list[str]] = {}
    if args.dyn_evidence and Path(args.dyn_evidence).is_file():
        doc = json.loads(Path(args.dyn_evidence).read_text(encoding="utf-8"))
        for site in doc.get("sites", []):
            dyn_evidence[site["site"].upper()] = \
                sorted(k.upper() for k in site.get("targets", {}))
    vec_evidence: dict[str, list[str]] = {}
    if args.vector_evidence and Path(args.vector_evidence).is_file():
        doc = json.loads(Path(args.vector_evidence).read_text(encoding="utf-8"))
        for site in doc.get("sites", []):
            vec_evidence[site["site"].upper()] = \
                sorted(k.upper() for k in site.get("vectors", {}))

    wanted = ([e.strip().upper() for e in args.entries.split(",") if e.strip()]
              or sorted(ir["functions"]))

    # FIXPOINT over the call DAG (tier 4, call-ABI composition): each round
    # promotes every candidate whose direct near callees are all already
    # promoted; the callee contracts feed the callers' gates and emitters.
    promoted: list[str] = []
    refused: dict[str, list[str]] = {}
    outputs: dict[str, tuple[str, str]] = {}
    # near-call contracts are per segment (near targets are IPs within the
    # caller's own cs); far-call contracts are keyed by the static (seg, off).
    contracts_by_cs: dict[int, dict[int, emit_cpuless.CalleeContract]] = {}
    far_contracts: dict[tuple[int, int], emit_cpuless.CalleeContract] = {}
    # dispatch-entry ownership: arrival "CS:IP" -> the promoted function key
    # whose recovered blocks serve it (first promoted container wins,
    # deterministically -- containing scans share the original instructions).
    dispatch_owner: dict[str, str] = {}
    iret_keys: set[str] = set()     # promoted IRET-contract handlers
    done: set[str] = set()
    rounds = 0
    while True:
        rounds += 1
        refused = {}
        progress = False
        # PRE-PASS: keys that would pass the static gate THIS round.  The
        # dyn-evidence gate accepts these as dispatchable-to, so mutually
        # recursive dispatch clusters (threaded-driver command chains whose
        # jump tables select each other) promote ATOMICALLY -- runtime
        # resolution is lazy, so the circular references are legal.
        tentative: set[str] = set(done)
        for key in wanted:
            if key in done:
                continue
            rec = ir["functions"][key]
            cs = int(key.split(":")[0], 16)
            try:
                if not rec.get("liftable", True):
                    raise emit_cpuless.Refusal("ir-not-liftable")
                emit_cpuless.check_promotable(
                    scan_from_ir_record(rec),
                    excluded_addrs={ip for (xcs, ip) in excluded if xcs == cs},
                    callees=contracts_by_cs.setdefault(cs, {}),
                    far_callees=far_contracts,
                    dispatch_addrs={ip for (xcs, ip) in dispatch_addrs
                                    if xcs == cs},
                    boundary_addrs={ip for (xcs, ip) in boundary_addrs
                                    if xcs == cs})
                tentative.add(key)
            except emit_cpuless.Refusal:
                pass
        for key in wanted:
            if key in done:
                continue
            rec = ir["functions"][key]
            cs = int(key.split(":")[0], 16)
            excl_ips = {ip for (xcs, ip) in excluded if xcs == cs}
            disp_ips = {ip for (xcs, ip) in dispatch_addrs if xcs == cs}
            head_ips = {ip for (xcs, ip) in boundary_addrs if xcs == cs}
            contracts = contracts_by_cs.setdefault(cs, {})
            injected_self = None
            try:
                if not rec.get("liftable", True):
                    raise emit_cpuless.Refusal("ir-not-liftable")
                scan = scan_from_ir_record(rec)
                # DIRECT SELF-RECURSION: compose the self-call with a
                # conservative full-bundle contract (the inductive fixed
                # point: assuming the callee balanced/side-effect-full, the
                # checker proves the body consistent).  The emitter calls
                # the module-level name directly -- no self-import.
                if any(i.kind == "call" and i.target == scan.entry
                       for i in scan.insts.values()) \
                        and scan.entry not in contracts:
                    _all = tuple(sorted(frozenset(emit_cpuless.W16)
                                        | frozenset({"ds", "es", "ss"})))
                    contracts[scan.entry] = emit_cpuless.CalleeContract(
                        name=f"func_{key.replace(':', '_').lower()}",
                        inputs=_all,
                        outputs=tuple(sorted(
                            (frozenset(emit_cpuless.W16)
                             | frozenset({"ds", "es"}))
                            - frozenset({"sp"}))),
                        exit_flags=frozenset(), needs_plat=True)
                    injected_self = scan.entry
                spec = emit_cpuless.check_promotable(
                    scan, excluded_addrs=excl_ips, callees=contracts,
                    far_callees=far_contracts, dispatch_addrs=disp_ips,
                    boundary_addrs=head_ips)
                abi = spec.abi
                _gate_dyn_evidence(scan, cs, dyn_evidence, tentative,
                                   dispatch_owner, contracts_by_cs, iret_keys)
                _gate_vector_evidence(scan, cs, vec_evidence, tentative,
                                      contracts_by_cs, iret_keys)
                recovered_src = emit_cpuless.emit_recovered(
                    scan, abi, key, callees=contracts,
                    far_callees=far_contracts,
                    recovered_import_base=args.import_base,
                    needs_plat=spec.needs_plat, dispatch_addrs=disp_ips,
                    df_livein=spec.df_livein, sp_output=spec.sp_output,
                    flags_livein=spec.flags_livein, boundary_addrs=head_ips)
                adapter_src = emit_cpuless.emit_adapter(
                    scan, abi, key,
                    signature=bytes.fromhex(rec["signature"]),
                    recovered_import_base=args.import_base,
                    needs_plat=spec.needs_plat, ret_kind=spec.ret_kind,
                    dispatch_addrs=disp_ips, df_livein=spec.df_livein,
                    sp_output=spec.sp_output, ret_pop=spec.ret_pop,
                    flags_livein=spec.flags_livein)
            except emit_cpuless.Refusal as e:
                if injected_self is not None:
                    contracts.pop(injected_self, None)
                refused.setdefault(str(e), []).append(key)
                continue
            promoted.append(key)
            done.add(key)
            outputs[key] = (recovered_src, adapter_src)
            keep = frozenset(emit_cpuless.W16) | frozenset({"ds", "es"})
            out_regs = (abi.outputs & keep) - (
                frozenset() if spec.sp_output else frozenset({"sp"}))
            contract = emit_cpuless.CalleeContract(
                name=f"func_{key.replace(':', '_').lower()}",
                inputs=tuple(emit_cpuless._contract_inputs(scan, abi)),
                outputs=tuple(sorted(out_regs)),
                exit_flags=spec.exit_flags, needs_plat=spec.needs_plat,
                ret_kind=spec.ret_kind, df_livein=spec.df_livein,
                sp_delta=spec.sp_delta, ret_pop=spec.ret_pop,
                sp_output=spec.sp_output, sp_deltas=spec.sp_deltas,
                flags_livein=spec.flags_livein, parks=spec.parks)
            contracts[scan.entry] = contract
            if spec.ret_kind == "far":
                far_contracts[(cs, scan.entry)] = contract
            if spec.ret_kind == "iret":
                iret_keys.add(key)
            for ip in sorted(disp_ips & set(scan.insts) - {scan.entry}):
                dispatch_owner.setdefault(f"{cs:04X}:{ip:04X}", key)
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
        standalone_only: list[str] = []
        for key in promoted:
            rec_src, ad_src = outputs[key]
            stem = key.replace(":", "_").lower()
            (rec_dir / f"func_{stem}.py").write_text(rec_src, encoding="utf-8",
                                                     newline="\n")
            kcs, kip = (int(x, 16) for x in key.split(":"))
            if contracts_by_cs[kcs][kip].parks:
                # STANDALONE-ONLY: the recovered body parks in-line via
                # plat.boundary; the demo graph keeps the original lifted
                # module (a park unwind would lose composed caller locals).
                standalone_only.append(key)
                continue
            (ad_dir / f"lifted_{stem}.py").write_text(ad_src, encoding="utf-8",
                                                      newline="\n")
        if standalone_only:
            print(f"STANDALONE-ONLY (parking; no adapter installed): "
                  f"{len(standalone_only)}: {', '.join(standalone_only)}")
        # the dynamic-dispatch registry: every promoted NEAR-return function
        # is a selector; owned dispatch entries route to their owner's
        # generated alternate entry.  Regenerated every apply (tier 9).
        registry: dict[str, tuple] = {}
        handlers: dict[str, tuple] = {}
        # Why a PROMOTED function is still not dynamically dispatchable.  This
        # is the CPUless-vs-VMless reachability gap made measurable: a VM can
        # enter any lifted function through its hook (an indirect jump simply
        # walks there), but the standalone program has no CPU and must resolve
        # every indirect transfer through this registry.  A recovered function
        # missing from it is reachable ONLY by static call composition -- so an
        # indirect jump to it raises UnknownDispatchTarget in live play while
        # VMless sails through.  The exclusions below are CONTRACT limits of
        # the near-dyn bundle, not missing evidence; each reason names the
        # bundle capability that would close it.
        dispatch_excluded: dict[str, str] = {}
        for key in promoted:
            kcs, kip = (int(x, 16) for x in key.split(":"))
            c = contracts_by_cs[kcs][kip]
            if c.ret_kind == "iret":
                # every IRET-contract function is vector-dispatchable: the
                # invoking site pops the frame at the MERGED runtime sp, so
                # even an sp-varying ISR (mid-ISR alt entries make the
                # static delta an artifact) is exact.
                handlers[key] = (f"{args.import_base}.{c.name}", c.name,
                                 None, tuple(c.inputs), c.needs_plat,
                                 c.df_livein, c.flags_livein)
                continue
            reason = None
            if c.ret_kind != "near":
                reason = f"ret-kind-{c.ret_kind}"   # needs a far-dyn bundle
            elif c.sp_output:
                reason = "sp-output"                # needs sp threading
            elif c.ret_pop:
                reason = "ret-pop"                  # callee-pops stack args
            elif c.sp_delta != 0:
                reason = "sp-delta"                 # unbalanced exit
            elif c.flags_livein:
                reason = "flags-livein"             # bundle carries no FLAGS
            if reason is not None:
                dispatch_excluded[key] = reason
                continue    # only balanced near-return functions dispatch
            registry[key] = (f"{args.import_base}.{c.name}", c.name, None,
                             tuple(c.inputs), c.needs_plat, c.df_livein,
                             c.flags_livein)
        for dkey, owner in dispatch_owner.items():
            ocs, oip = (int(x, 16) for x in owner.split(":"))
            c = contracts_by_cs[ocs][oip]
            registry[dkey] = (f"{args.import_base}.{c.name}", c.name,
                              int(dkey.split(":")[1], 16),
                              tuple(c.inputs), c.needs_plat, c.df_livein,
                              c.flags_livein)
        (rec_dir / "dispatch.py").write_text(
            emit_cpuless.emit_dispatch_table(registry, handlers),
            encoding="utf-8", newline="\n")
        (rec_dir / "_dyncall.py").write_text(
            emit_cpuless.DYNCALL_SUPPORT_SRC, encoding="utf-8", newline="\n")
        print(f"APPLIED: {len(promoted)} recovered function(s) -> {rec_dir}; "
              f"adapters occupy their lifted slots in {ad_dir}; dispatch "
              f"registry: {len(registry)} selectors "
              f"({len(dispatch_owner)} alternate entries).")
        # The reachability gap, reported every apply -- never discovered as a
        # live-play UnknownDispatchTarget crash.
        if dispatch_excluded:
            by_reason: dict[str, list[str]] = {}
            for k, r in sorted(dispatch_excluded.items()):
                by_reason.setdefault(r, []).append(k)
            print(f"NOT DYNAMICALLY DISPATCHABLE: {len(dispatch_excluded)} of "
                  f"{len(promoted)} promoted function(s) -- static-call "
                  f"reachable only; an indirect jump to one raises "
                  f"UnknownDispatchTarget:")
            for r, ks in sorted(by_reason.items(),
                                key=lambda kv: (-len(kv[1]), kv[0])):
                print(f"    {r:16s} {len(ks):3d}  {', '.join(ks[:6])}"
                      f"{' ...' if len(ks) > 6 else ''}")
        if args.census_out:
            cpath = Path(args.census_out)
            if cpath.is_file():
                doc = json.loads(cpath.read_text(encoding="utf-8"))
                doc["dispatch"] = {
                    "selectors": sorted(registry),
                    "handlers": sorted(handlers),
                    "alternate_entries": sorted(dispatch_owner),
                    "excluded": dispatch_excluded,
                }
                cpath.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    elif args.apply:
        print("APPLIED: nothing promotable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
