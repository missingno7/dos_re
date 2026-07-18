"""abi_core_verify.py -- run the seeded mechanical-vs-ABI differential over
every emitted de-stacked core (M3b slice 2 verification).

For each core in cores_manifest.json, the MECHANICAL reference is emitted
FRESH from the recovery IR with the current emitter and compared against
the de-stacked ABI core over the same deterministic pseudo-random states
(dos_re.lift.abi_diff): observed returns, the compat channel (exit flags +
virtual-time cost), and every SEMANTIC memory write must agree exactly --
only the mechanical side's machine-stack writes are excluded, because
virtualising exactly those is the point of the transformation.

Fresh emission (not the shipped modules) keeps both sides of the
differential the SAME emitter generation: the shipped corpus may predate
translator changes (e.g. the cs-constant-local), and comparing across
generations reports emitter drift, not core bugs.  The shipped corpus'
authority is the demo acceptance gate, not this tool.

Run from the game root:
    python dos_re/tools/abi_core_verify.py \
        --ir artifacts/lift/recovery_ir.json \
        --abi-dir lemmings/recovered_abi --abi-base lemmings.recovered_abi \
        --census artifacts/abi/contract_census.json [--states 64]
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path.cwd()))

from dos_re.lift import emit_cpuless  # noqa: E402
from dos_re.lift.abi_diff import (  # noqa: E402
    INCONCLUSIVE, INTERNAL_ERROR, MISMATCH, VERDICT_EXIT,
    VERIFIED, VerdictReport, aggregate, diff_one, verdict_name)
from dos_re.lift.contracts import scan_for  # noqa: E402

_MECH_PKG = "_abidiff_mech"       # temp package the fresh mechanical closure loads under
_KEEP = frozenset(emit_cpuless.W16) | frozenset({"ds", "es"})


def _mech_contract(scan, spec, name):
    """A near-callee CalleeContract from a promoted scan (mirrors
    tools/cpuless_promote.py's construction)."""
    abi = spec.abi
    out_regs = (abi.outputs & _KEEP) - (frozenset()
                if spec.sp_output else frozenset({"sp"}))
    return emit_cpuless.CalleeContract(
        name=name, inputs=tuple(emit_cpuless._contract_inputs(scan, abi)),
        outputs=tuple(sorted(out_regs)), exit_flags=spec.exit_flags,
        needs_plat=spec.needs_plat, ret_kind=spec.ret_kind,
        df_livein=spec.df_livein, sp_delta=spec.sp_delta,
        ret_pop=spec.ret_pop, sp_output=spec.sp_output,
        sp_deltas=spec.sp_deltas, flags_livein=spec.flags_livein,
        parks=spec.parks)


def _promote_toolchain_sig():
    """The emitter generation, as abi_promote computes it -- imported rather
    than reimplemented, so the two can never drift apart."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from abi_promote import _toolchain_sig
        return _toolchain_sig()
    except Exception:                                        # noqa: BLE001
        return None


def _fresh_mechanical(ir: dict, key: str, _cache: dict):
    """Emit + load the mechanical reference for one function -- and, bottom-up,
    every function it reaches by NEAR call -- from the IR with the CURRENT
    emitter, so both sides of the differential are the same emitter
    generation.  The closure loads under a temp package (``_MECH_PKG``) whose
    modules the caller's generated `from _MECH_PKG.func_X import func_X`
    resolve against.  Returns (fn, contract)."""
    if key in _cache:
        return _cache[key]
    scan, why = scan_for(ir["functions"][key])
    if scan is None:
        raise RuntimeError(f"{key}: {why}")
    cs = int(key.split(":")[0], 16)
    callees, far_callees = {}, {}
    for i in scan.insts.values():
        if i.kind == emit_cpuless.CALL and i.target is not None:
            tkey = f"{cs:04X}:{i.target:04X}"
            _, c = _fresh_mechanical(ir, tkey, _cache)
            callees[i.target] = c
        elif i.kind == emit_cpuless.CALL_FAR and i.far_target is not None:
            # the closure must follow FAR edges too, or check_promotable
            # refuses the caller with call-abi-composition and the reference
            # cannot be built for a far-composed core.
            fkey = "%04X:%04X" % i.far_target
            _, c = _fresh_mechanical(ir, fkey, _cache)
            far_callees[i.far_target] = c
    spec = emit_cpuless.check_promotable(scan, callees=callees,
                                         far_callees=far_callees)
    src = emit_cpuless.emit_recovered(
        scan, spec.abi, key, callees=callees, far_callees=far_callees,
        recovered_import_base=_MECH_PKG, needs_plat=spec.needs_plat,
        df_livein=spec.df_livein, sp_output=spec.sp_output,
        flags_livein=spec.flags_livein)
    stem = f"func_{key.replace(':', '_').lower()}"
    if _MECH_PKG not in sys.modules:
        sys.modules[_MECH_PKG] = types.ModuleType(_MECH_PKG)
    mod = types.ModuleType(f"{_MECH_PKG}.{stem}")
    sys.modules[f"{_MECH_PKG}.{stem}"] = mod
    exec(compile(src, stem + ".py", "exec"), mod.__dict__)
    fn = getattr(mod, stem)
    contract = _mech_contract(scan, spec, stem)
    _cache[key] = (fn, contract)
    return fn, contract



#: worker-side state, populated once per process by _pool_init
_W: dict = {}


def _pool_init(ir, census, abi_base, states, iter_cap=0):
    """Load the heavy inputs ONCE per worker, not once per core.

    ``iter_cap`` must reach the workers too: it was applied only on the
    sequential path, so `--jobs N --iter-cap X` silently ran at the emitted
    20M cap -- the flag appeared to work while doing nothing, which is worse
    than not offering it.
    """
    _W["ir"] = ir
    _W["census"] = census
    _W["abi_base"] = abi_base
    _W["states"] = states
    _W["iter_cap"] = iter_cap
    _W["mech"] = {}


def _verify_one(key: str):
    """Differential for a single core, in a worker process.

    The mechanical closure cache is per-process, so a worker that draws
    several cores from one call graph still emits each callee once.
    Returns a plain-data tuple -- no module objects cross the boundary.
    """
    import time as _time
    stem = key.replace(":", "_").lower()
    t0 = _time.time()
    try:
        mech_fn, _ = _fresh_mechanical(_W["ir"], key, _W["mech"])
        core_mod = importlib.import_module(f"{_W['abi_base']}.core_{stem}")
        cap = _W.get("iter_cap") or 0
        if cap:
            for nm, m in list(sys.modules.items()):
                if m is not None and hasattr(m, "_ITER_CAP") and (
                        nm.startswith(_MECH_PKG)
                        or nm.startswith(_W["abi_base"])):
                    m._ITER_CAP = cap
        rep = diff_one(mech_fn, core_mod._abi_core,
                       _W["census"]["functions"][key], states=_W["states"])
    except Exception as e:                                   # noqa: BLE001
        # a worker crash must be a REPORTED failure, never a silently missing
        # core -- that would look identical to a pass.  The TYPED report
        # crosses the process boundary (frozen + top-level, so picklable), so
        # the tool never has to reconstruct a verdict from a status string.
        return key, VerdictReport.internal_error(
            f"verifier raised {type(e).__name__}: {e}"), 0.0
    return key, rep, (_time.time() - t0) * 1000.0

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ir", required=True)
    ap.add_argument("--abi-dir", required=True)
    ap.add_argument("--abi-base", required=True)
    ap.add_argument("--census", required=True)
    ap.add_argument("--states", type=int, default=64)
    ap.add_argument("--cache", default=None,
                    help="incremental-verification cache (JSON).  A core is "
                         "RE-verified only when something that feeds its "
                         "verification changed: its own generated source, its "
                         "IR record, its contract, the state count, or the "
                         "toolchain (emitter/differential/contract analysis). "
                         "A typical slice touches a handful of cores, so this "
                         "turns a 10-minute corpus run into seconds -- without "
                         "ever skipping something that actually changed.")
    ap.add_argument("--iter-cap", type=int, default=0,
                    help="override the generated spin-detector cap on BOTH "
                         "sides (0 = leave as emitted, 20M).  A spin-wait "
                         "burns the full cap per state, so the corpus cost is "
                         "dominated by loops whose ANSWER is just 'both sides "
                         "spin identically' -- the same evidence a 100k cap "
                         "gives 200x cheaper.  Both sides are lowered "
                         "together, so the comparison stays exact.")
    ap.add_argument("--budget-s", type=int, default=0,
                    help="wall-clock budget for the parallel run (0 = none). "
                         "On breach the unfinished cores are NAMED and marked "
                         "unverified rather than the run hanging: an hour of "
                         "silence buys no information, a named list does.")
    ap.add_argument("--jobs", type=int, default=1,
                    help="verify this many cores in parallel (processes).  "
                         "The differential is embarrassingly parallel across "
                         "cores and cost is heavily concentrated, so a cold "
                         "corpus run is dominated by a handful of long poles; "
                         "spreading them is the wall-clock lever once the "
                         "cache has removed the redundant work.  Default 1 "
                         "(sequential) -- identical verdicts either way.")
    ap.add_argument("--include-inconclusive", action="store_true",
                    help="re-run cores a previous ledger recorded as "
                         "INCONCLUSIVE.  By default they are skipped and "
                         "CARRIED FORWARD into the report: a spin-wait core "
                         "establishes nothing per state, so 64 samples of "
                         "nothing cost 64x one sample of nothing, and they "
                         "dominate the corpus runtime.  Skipping never "
                         "improves the count -- they stay INCONCLUSIVE in the "
                         "summary, the ledger and the exit status.")
    ap.add_argument("--ledger", default=None,
                    help="always-written per-core verdict ledger (verified / "
                         "inconclusive / mismatch / internal_error).  Distinct "
                         "from --cache: the cache skips re-verification and "
                         "only a fully green run may publish one, while the "
                         "ledger records which cores are proven -- still true "
                         "when other cores are inconclusive.  Integration "
                         "consumes this.")
    ap.add_argument("--only", default="",
                    help="comma-separated CS:IP subset (bisection aid)")
    ap.add_argument("--slow-ms", type=int, default=5000,
                    help="report per-core wall time above this (default 5s), "
                         "so a pathological function is VISIBLE instead of "
                         "silently dominating the run")
    args = ap.parse_args(argv)

    ir = json.loads(Path(args.ir).read_text(encoding="utf-8"))
    census = json.loads(Path(args.census).read_text(encoding="utf-8"))
    manifest = json.loads((Path(args.abi_dir) / "cores_manifest.json")
                          .read_text(encoding="utf-8"))
    keys = manifest["cores"]

    # GENERATION CHECK.  Verifying a corpus is only meaningful if the corpus
    # came from the emitter now on disk.  A long-running emission started
    # against an older tree can land later and overwrite a newer corpus: the
    # result is a perfectly valid manifest from the wrong generation, and the
    # differential will happily verify it and publish a green cache.  That is
    # exactly how a 143-core corpus became a 90-core one mid-session while
    # every gate stayed green.
    man_sig = manifest.get("toolchain_sig")
    cur_sig = _promote_toolchain_sig()
    if man_sig is not None and cur_sig is not None and man_sig != cur_sig:
        print(f"REFUSING: cores_manifest.json was written by toolchain "
              f"{man_sig}, but the emitter on disk is {cur_sig}.  The corpus "
              f"and the code that must verify it are different generations "
              f"-- re-emit before verifying.")
        return 3
    if man_sig is None:
        print("  (manifest predates generation stamping -- cannot confirm "
              "the corpus matches this emitter)")

    if args.only:
        want = {k.strip().upper() for k in args.only.split(",") if k.strip()}
        keys = [k for k in keys if k.upper() in want]
        missing = sorted(want - {k.upper() for k in keys})
        if missing:
            # A typoed --only during bisection used to select nothing, verify
            # nothing, and exit 0 announcing every core identical.
            print(f"REFUSING: --only selected {len(missing)} key(s) not in "
                  f"the manifest: {', '.join(missing[:8])}")
            return VERDICT_EXIT[INTERNAL_ERROR]
    if not keys:
        print("REFUSING: no cores selected -- an empty corpus proves nothing "
              "and must not report success.")
        return VERDICT_EXIT[INTERNAL_ERROR]
    # The toolchain fingerprint, deliberately NARROW.
    #
    # It used to include emit_abi.py and contracts.py, so every emitter slice
    # invalidated all 143 cached results and a 45-minute re-verification stood
    # between an edit and its answer -- long enough that the tree could move
    # underneath the run.
    #
    # But those two are ALREADY captured downstream: the emitted core source is
    # the output of emit_abi, and the census entry is the output of contracts,
    # and _core_sig hashes both.  Hashing the inputs as well, when the outputs
    # are in hand, buys nothing and costs the entire cache.  A slice that
    # changes 30 cores should re-verify 30 cores.
    #
    # What genuinely stays global: emit_cpuless.py builds the MECHANICAL
    # reference side (not covered by any per-core hash) and abi_diff.py defines
    # what "equal" means (so it can flip any verdict).
    tool_h = hashlib.sha256()
    for mod in ("dos_re/lift/emit_cpuless.py", "dos_re/lift/abi_diff.py"):
        pth = Path(__file__).resolve().parents[1] / mod
        tool_h.update(pth.read_bytes())
    tool_sig = tool_h.hexdigest()[:16]

    def _tree_fingerprint() -> str:
        """Hash of every source the verdict depends on, for staleness."""
        h = hashlib.sha256()
        for mod in ("dos_re/lift/emit_cpuless.py", "dos_re/lift/abi_diff.py",
                    "dos_re/lift/emit_abi.py", "dos_re/lift/contracts.py"):
            h.update((Path(__file__).resolve().parents[1] / mod).read_bytes())
        for p in sorted(Path(args.abi_dir).glob("core_*.py")):
            h.update(p.read_bytes())
        return h.hexdigest()[:16]

    tree_before = _tree_fingerprint()

    cache_path = Path(args.cache) if args.cache else None
    cache = {}
    if cache_path and cache_path.is_file():
        doc = json.loads(cache_path.read_text(encoding="utf-8"))
        if doc.get("tool_sig") == tool_sig and doc.get("states") == args.states:
            cache = doc.get("verified", {})
        else:
            print("  (cache invalidated: toolchain or state count changed)")

    def _core_sig(key: str, stem: str) -> str:
        h = hashlib.sha256()
        h.update((Path(args.abi_dir) / f"core_{stem}.py").read_bytes())
        h.update(json.dumps(census["functions"][key], sort_keys=True).encode())
        h.update(ir["functions"][key]["signature"].encode())
        return h.hexdigest()

    passed, raised_states, cached = 0, 0, 0
    spin_notes: list[str] = []
    slow: list[tuple[float, str]] = []
    failures: dict[str, list[str]] = {}
    inconclusive: dict[str, str] = {}
    internal: dict[str, str] = {}
    mech_cache: dict = {}
    fresh_ok: dict[str, str] = {}

    # SLOWEST FIRST among the cores that must actually run.  Cost is heavily
    # concentrated (1010:1B42 alone was 133s of 375s), so starting the long
    # poles first is what decides wall-clock once the work is spread over
    # workers; it also surfaces the expensive failures early instead of after
    # a hundred cheap passes.
    prev_cost = {}
    if cache_path and cache_path.is_file():
        try:
            prev_cost = json.loads(
                cache_path.read_text(encoding="utf-8")).get("cost_ms", {})
        except Exception:                                    # noqa: BLE001
            prev_cost = {}
    keys = sorted(keys, key=lambda k: -prev_cost.get(k, 0))
    cost_ms: dict[str, int] = {}

    # CARRY FORWARD known-inconclusive cores instead of re-grinding them.
    #
    # These are the spin-waits: every state burns the full iteration cap to
    # establish nothing, and they dominate wall-clock (one core was 70s of a
    # 19-minute run).  Re-proving "still unprovable" every cycle buys no
    # information -- what would change the answer is event modelling, not
    # another 64 samples.
    #
    # They are SKIPPED, never dropped: each is re-reported as inconclusive so
    # the summary, ledger and exit status are exactly what a full run would
    # give.  A faster run must not be a rosier one.
    carried = {}
    if not args.include_inconclusive and args.ledger             and Path(args.ledger).is_file():
        try:
            prev = json.loads(Path(args.ledger).read_text(encoding="utf-8"))
            carried = dict(prev.get("inconclusive", {}))
        except Exception:                                    # noqa: BLE001
            carried = {}
    if carried:
        print(f"  carrying forward {len(carried)} INCONCLUSIVE core(s) "
              f"(pass --include-inconclusive to re-run them)")

    # split cached from must-run FIRST, so the parallel path only ever ships
    # real work to workers
    todo = []
    for key in keys:
        stem = key.replace(":", "_").lower()
        if key in carried:
            inconclusive[key] = (carried[key] + "  [carried forward; not "
                                 "re-verified this run]")
            continue
        sig = _core_sig(key, stem)
        if cache.get(key) == sig:
            cached += 1
            passed += 1
            fresh_ok[key] = sig
            continue
        todo.append((key, sig))

    if args.jobs > 1 and todo:
        # STREAM results as they land, and bound the whole run.
        #
        # ex.map yields in submission order, so one pathological core blocks
        # every later result: a 143-core run produced ZERO output for an hour
        # and had to be killed, buying no information at all -- the same
        # silence failure the emitter's fixpoint had.  as_completed reports
        # each core the moment it finishes, so the culprit is VISIBLE while
        # the run is still going.
        from concurrent.futures import (ProcessPoolExecutor, as_completed,
                                        TimeoutError as FutTimeout)
        sigs = dict(todo)
        deadline = args.budget_s if args.budget_s > 0 else None
        started = time.time()
        ex = ProcessPoolExecutor(max_workers=args.jobs, initializer=_pool_init,
                                 initargs=(ir, census, args.abi_base,
                                           args.states, args.iter_cap))
        futs = {ex.submit(_verify_one, k): k for k, _ in todo}
        done_n = 0
        timed_out = False
        try:
            for fut in as_completed(
                    futs, timeout=(None if deadline is None
                                   else max(1.0, deadline -
                                            (time.time() - started)))):
                key, rep, dt = fut.result()
                done_n += 1
                cost_ms[key] = int(dt)
                if dt >= args.slow_ms:
                    slow.append((dt, key))
                raised_states += rep.raised
                if rep.note:
                    spin_notes.append(f"{key}: {rep.note}")
                st = rep.verdict
                if st is INTERNAL_ERROR:
                    internal[key] = rep.diagnostics[0]
                elif st is VERIFIED:
                    passed += 1
                    fresh_ok[key] = sigs[key]
                elif st is INCONCLUSIVE:
                    inconclusive[key] = rep.note or "inconclusive"
                else:
                    failures[key] = list(rep.diagnostics[:3])
                print(f"  [{done_n}/{len(futs)}] {key} "
                      f"{rep.status} {dt/1000.0:.1f}s", flush=True)
        except FutTimeout:
            timed_out = True
            # A budget breach is a REPORTED outcome, never a silent pass: the
            # unfinished cores are named so the next run can target them.
            stuck = [futs[f] for f in futs if not f.done()]
            print(f"  BUDGET EXCEEDED after {args.budget_s}s -- "
                  f"{len(stuck)} core(s) unfinished: "
                  f"{', '.join(sorted(stuck)[:8])}"
                  + (" ..." if len(stuck) > 8 else ""), flush=True)
            for k in stuck:
                # INCONCLUSIVE, not a mismatch: exceeding a wall-clock budget
                # proves neither equivalence nor divergence.  Reporting it as
                # "diverged" would blame the core for not finishing.
                inconclusive[k] = (f"unverified: exceeded the "
                                   f"{args.budget_s}s run budget (re-run with "
                                   f"--only {k} --states 8 to characterise it)")
        finally:
            # CAPTURE THE PROCESSES FIRST: shutdown() sets _processes to None,
            # so reading it afterwards raised AttributeError -- in a `finally`,
            # which meant it broke every parallel run, not just a budget
            # breach.  (Shipped without ever running --jobs once.)
            procs = list((getattr(ex, "_processes", None) or {}).values())
            if timed_out:
                # cancel_futures drops QUEUED work but cannot stop a worker
                # already inside a pathological core; terminate so a budget
                # breach actually ends the run, then join with a bound.
                ex.shutdown(wait=False, cancel_futures=True)
                for pr in procs:
                    if pr.is_alive():
                        pr.terminate()
                for pr in procs:
                    pr.join(timeout=5)
            else:
                ex.shutdown(wait=True)      # ordinary completion: let them end
        todo = []

    # sequential path -- the reference implementation; --jobs>1 above must
    # produce identical verdicts, only sooner
    def _apply_iter_cap():
        """Lower the cap in every generated module currently loaded -- the
        mechanical closure and the ABI cores alike.  Both sides must get the
        SAME value or a spin-wait would stop at different iteration counts and
        the cost channel would diverge (a false mismatch)."""
        if not args.iter_cap:
            return
        for nm, m in list(sys.modules.items()):
            if m is None:
                continue
            if nm.startswith(_MECH_PKG) or nm.startswith(args.abi_base):
                if hasattr(m, "_ITER_CAP"):
                    m._ITER_CAP = args.iter_cap

    for key, sig in todo:
        stem = key.replace(":", "_").lower()
        t0 = time.time()
        try:
            mech_fn, _ = _fresh_mechanical(ir, key, mech_cache)
            core_mod = importlib.import_module(f"{args.abi_base}.core_{stem}")
            _apply_iter_cap()
            rep = diff_one(mech_fn, core_mod._abi_core,
                           census["functions"][key], states=args.states)
        except Exception as e:                               # noqa: BLE001
            # SAME handling as the worker path.  The sequential path let this
            # propagate and killed the tool, while --jobs reported it as a
            # failure: the same corpus produced a crash or a verdict depending
            # only on how it was scheduled.  Scheduling must never change what
            # is concluded -- found by the test asserting exactly that.
            rep = VerdictReport.internal_error(
                f"verifier raised {type(e).__name__}: {e}")
        dt = (time.time() - t0) * 1000.0
        cost_ms[key] = int(dt)
        if dt >= args.slow_ms:
            slow.append((dt, key))
        raised_states += rep.raised
        if rep.note:
            spin_notes.append(f"{key}: {rep.note}")
        if rep.verdict is INTERNAL_ERROR:
            # NOT a mismatch: the verifier failed, which proves nothing about
            # the core either way.  Collapsing the two would report a tooling
            # bug as a recovery defect.
            internal[key] = rep.diagnostics[0]
        elif rep.verdict is VERIFIED:
            passed += 1
            fresh_ok[key] = sig
        elif rep.verdict is INCONCLUSIVE:
            # NOT cached: an inconclusive core has no positive evidence, and
            # caching it would let "both sides failed identically" masquerade
            # as a verified result on every later run.
            inconclusive[key] = rep.note or "inconclusive"
        else:
            failures[key] = list(rep.diagnostics[:3])

    print(f"ABI-core differential over {len(keys)} de-stacked cores, "
          f"{args.states} seeded states each (mechanical reference emitted "
          f"fresh from the IR):")
    print(f"  VERIFIED identical       {passed:4d}"
          + (f"   ({cached} unchanged, from cache)" if cached else ""))
    print(f"  states raising on both sides (compared equal): "
          f"{raised_states}")
    for n in spin_notes:
        print(f"  note: {n}")
    for dt, key in sorted(slow, reverse=True)[:8]:
        print(f"  slow: {key} took {dt/1000.0:.1f}s")
    if inconclusive:
        # Reported prominently: these are NOT verified, and a reader who sees
        # only a green line would otherwise count them as such.
        print(f"  INCONCLUSIVE             {len(inconclusive):4d}"
              f"   (some state established nothing -- NOT proven equivalent)")
        for key, note in sorted(inconclusive.items())[:8]:
            print(f"    {key}: {note}")
    if failures:
        print(f"  MISMATCHED               {len(failures):4d}")
        for key, ms in sorted(failures.items()):
            print(f"    {key}:")
            for m in ms:
                print(f"      {m}")
    # STALENESS: a long run is a run the tree can move underneath.  These
    # verdicts describe the sources as they were when the run STARTED; if an
    # emitter edit or a re-emission landed since, they describe a tree that no
    # longer exists and publishing them would record a verification of code
    # nobody has.  Report loudly and refuse to publish rather than bless it.
    if _tree_fingerprint() != tree_before:
        print("  STALE: the sources changed while this run was in flight "
              "(emitter edit or re-emission).  The verdicts above describe "
              "the tree as it was at START and are NOT published.  Re-run "
              "against the settled tree.")
        # distinct from the verdict codes (0/1/2/3): staleness is not a
        # statement about the corpus at all
        return 4
    # THE LEDGER is always written; the incremental CACHE is not.
    #
    # They answer different questions and conflating them blocks a legitimate
    # workflow: the cache exists to skip re-verification and must only be
    # published by a fully green run, while the ledger records WHICH cores are
    # proven -- a per-core fact that stays true even when other cores came
    # back inconclusive.  Integration reads the ledger (--verified-only), so
    # without this an all-but-four-green corpus could integrate nothing.
    if args.ledger:
        Path(args.ledger).parent.mkdir(parents=True, exist_ok=True)
        Path(args.ledger).write_text(json.dumps({
            "_notice": "GENERATED by dos_re tools/abi_core_verify.py -- the "
                       "per-core VERDICT LEDGER.  'verified' is the only set "
                       "safe to integrate; inconclusive cores are real "
                       "artifacts that are NOT established as equivalent.",
            "tool_sig": tool_sig, "states": args.states,
            "verified": dict(sorted(fresh_ok.items())),
            "inconclusive": dict(sorted(inconclusive.items())),
            "mismatch": {k: v for k, v in sorted(failures.items())},
            "internal_error": dict(sorted(internal.items())),
        }, indent=1) + "\n", encoding="utf-8")
        print(f"  ledger: {len(fresh_ok)} verified / {len(inconclusive)} "
              f"inconclusive / {len(failures)} mismatch / {len(internal)} "
              f"internal-error -> {args.ledger}")

    # ONE verdict decides the exit status, the summary AND cache publication.
    # Previously each was derived independently: an inconclusive run printed a
    # caveat, returned 0, and still published a cache -- so `subprocess.run(
    # ..., check=True)`, CI and any shell chain read it as success.
    if internal:
        print(f"  VERIFIER ERRORS          {len(internal):4d}"
              f"   (tooling failed; proves nothing about these cores)")
        for k, msg in sorted(internal.items())[:5]:
            print(f"    {k}: {msg}")
    # `passed` cores contribute VERIFIED; the baseline is NOT synthetic --
    # with an empty corpus rejected above, every verdict here is earned.
    run_verdict = aggregate(
        [INTERNAL_ERROR] * bool(internal)
        + [MISMATCH] * bool(failures)
        + [INCONCLUSIVE] * bool(inconclusive)
        + [VERIFIED] * bool(passed))
    if run_verdict == VERIFIED:
        print("ABI-CORE DIFFERENTIAL PASSED: every de-stacked core IS its "
              "mechanical function on every driven state.")
    elif run_verdict == INCONCLUSIVE:
        print(f"ABI-CORE DIFFERENTIAL INCONCLUSIVE: {passed} verified, "
              f"{len(inconclusive)} established nothing.  No core diverged, "
              f"but this run does NOT prove the corpus equivalent.")
    elif run_verdict == MISMATCH:
        print(f"ABI-CORE DIFFERENTIAL FAILED: {len(failures)} core(s) "
              f"diverged.")
    else:
        print(f"ABI-CORE DIFFERENTIAL ERRORED: the verifier failed on "
              f"{len(internal)} core(s); no conclusion about the corpus.")
    # Only a fully VERIFIED run may publish -- the invariant the adjacent
    # comment already claimed while the code published partial results.
    if cache_path is not None and run_verdict == VERIFIED:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(
            {"_notice": "GENERATED by dos_re tools/abi_core_verify.py -- "
                        "incremental verification cache. Delete (or pass "
                        "--full via scripts/abi_build.py) to force a full "
                        "re-verification.",
             "tool_sig": tool_sig, "states": args.states,
             "cost_ms": {**prev_cost, **cost_ms},
             "verified": dict(sorted(fresh_ok.items()))},
            indent=1) + "\n", encoding="utf-8")
        print(f"  cache: {len(fresh_ok)} verified entries -> {cache_path}")
    elif cache_path is not None:
        print(f"  cache NOT published ({verdict_name(run_verdict)} run)")
    return VERDICT_EXIT[run_verdict]


if __name__ == "__main__":
    raise SystemExit(main())
