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
# the GAME root too: --check-parity imports the shipped mechanical modules
# to compare signatures against (runtime introspection, not source parsing).
sys.path.insert(0, str(Path.cwd()))

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


_MECH_KEEP = None


def _mech_spec(ir, key, cache, scan_for, _active=None, *,
               dispatch=frozenset(), heads=frozenset()):
    """The mechanical promotion spec for one function, resolving near/far
    callee contracts RECURSIVELY -- check_promotable refuses `contains-call`
    without them, so an adapter for any non-leaf needs the closure walked
    exactly as tools/abi_core_verify.py does for its reference.

    CYCLES REFUSE.  The verifier never hit one because its closures start
    from cores (acyclic by construction); the full corpus is not.  A
    self- or mutually-recursive chain gets a named refusal rather than a
    guessed conservative contract -- an adapter built on a guess would be
    unverifiable, which is worse than not emitting one."""
    from dos_re.lift import emit_cpuless as _ec
    global _MECH_KEEP
    if _MECH_KEEP is None:
        _MECH_KEEP = frozenset(_ec.W16) | frozenset({"ds", "es"})
    if key in cache:
        return cache[key]
    _active = _active or set()
    if key in _active:
        raise Refusal("mech-call-cycle")
    _active = _active | {key}
    scan, why = scan_for(ir["functions"][key])
    if scan is None:
        raise Refusal(why)
    cs = int(key.split(":")[0], 16)
    callees, far_callees = {}, {}
    for i in scan.insts.values():
        if i.kind == _ec.CALL and i.target is not None:
            t = f"{cs:04X}:{i.target:04X}"
            if t not in ir["functions"]:
                raise Refusal("mech-callee-not-in-ir")
            callees[i.target] = _mech_spec(ir, t, cache, scan_for, _active,
                                           dispatch=dispatch, heads=heads)[1]
        elif i.kind == _ec.CALL_FAR and i.far_target is not None:
            t = "%04X:%04X" % i.far_target
            if t not in ir["functions"]:
                raise Refusal("mech-callee-not-in-ir")
            far_callees[i.far_target] = _mech_spec(
                ir, t, cache, scan_for, _active,
                dispatch=dispatch, heads=heads)[1]
    # SAME PARAMETERS the shipped corpus was generated with.  Dispatch
    # arrivals and boundary heads WIDEN abi.inputs to the full bundle (an
    # alt-entry's liveness is externally governed), which is what puts `cs`
    # in a dispatch target's signature.  Omitting them produced an adapter
    # whose signature did not match the module it replaces -- caught at
    # runtime as "func_1010_4be7() got an unexpected keyword argument 'cs'".
    kcs = int(key.split(":")[0], 16)
    spec = _ec.check_promotable(
        scan, callees=callees, far_callees=far_callees,
        dispatch_addrs={ip for (c, ip) in dispatch if c == kcs},
        boundary_addrs={ip for (c, ip) in heads if c == kcs})
    abi = spec.abi
    outs = (abi.outputs & _MECH_KEEP) - (frozenset()
                                         if spec.sp_output
                                         else frozenset({"sp"}))
    contract = _ec.CalleeContract(
        name=f"func_{key.replace(':', '_').lower()}",
        inputs=tuple(_ec._contract_inputs(scan, abi)),
        outputs=tuple(sorted(outs)), exit_flags=spec.exit_flags,
        needs_plat=spec.needs_plat, ret_kind=spec.ret_kind,
        df_livein=spec.df_livein, sp_delta=spec.sp_delta,
        ret_pop=spec.ret_pop, sp_output=spec.sp_output,
        sp_deltas=spec.sp_deltas, flags_livein=spec.flags_livein,
        parks=spec.parks)
    cache[key] = (spec, contract, scan)
    return cache[key]


class _ProbeSpin(Exception):
    """The probe call did not terminate within its access budget."""


class _ProbeMem:
    """Throwaway memory for harvesting a shipped function's OUTPUT KEYS.

    BOUNDED ON PURPOSE.  Every read answers 0, so a function that spins
    `while [mem] == 0` -- a wait-for-flag, a retrace poll, a queue drain --
    never terminates.  An unbounded probe hangs the whole emission with no
    output, which is exactly how this first showed up.  The budget converts
    that hang into a named refusal; since a probe failure now costs only the
    ADAPTER and never the core, refusing here is cheap and honest.
    """

    MAX_ACCESS = 2_000_000

    def __init__(self):
        self.n = 0
        self.st = {}

    def _tick(self):
        self.n += 1
        if self.n > self.MAX_ACCESS:
            raise _ProbeSpin()

    @staticmethod
    def _seed(lin):
        # Deterministic pseudo-random, same idea as abi_diff.TraceMem: an
        # all-zero memory makes `while [flag] == 0` unsatisfiable, so 90 of
        # 113 cores could not be probed at all.  Varied bytes let those loops
        # reach an exit; writes overlay so a function still observes its own
        # stores.  Deterministic because a probe that differs between runs
        # would make the emitted corpus irreproducible.
        return (lin * 2654435761 + 0x9E37) >> 7 & 0xFF

    def _b(self, lin):
        v = self.st.get(lin)
        return self._seed(lin) if v is None else v

    def rb(self, s, o):
        self._tick()
        return self._b(((s << 4) + (o & 0xFFFF)) & 0xFFFFF)

    def rw(self, s, o):
        self._tick()
        lin = ((s << 4) + (o & 0xFFFF)) & 0xFFFFF
        return self._b(lin) | (self._b((lin + 1) & 0xFFFFF) << 8)

    def wb(self, s, o, v):
        self._tick()
        self.st[((s << 4) + (o & 0xFFFF)) & 0xFFFFF] = v & 0xFF

    def ww(self, s, o, v):
        self._tick()
        lin = ((s << 4) + (o & 0xFFFF)) & 0xFFFFF
        self.st[lin] = v & 0xFF
        self.st[(lin + 1) & 0xFFFFF] = (v >> 8) & 0xFF


class _ProbePlat:
    """Port/interrupt stub for the probe, bounded for the same reason as
    _ProbeMem: a poll loop on an I/O port touches no memory at all."""

    MAX_CALLS = 2_000_000

    def __init__(self):
        self.n = 0

    def _tick(self):
        self.n += 1
        if self.n > self.MAX_CALLS:
            raise _ProbeSpin()

    def inp(self, *a):
        self._tick()
        # vary, for the same reason _ProbeMem does: a status-port poll
        # (`in al, 0x3DA; test al, 8; jz`) never exits on a constant.
        return (self.n * 2654435761 >> 5) & 0xFF
    def outp(self, *a): self._tick()

    def intr(self, n, ib, cost):
        self._tick()
        out = {r: 0 for r in ("ax", "bx", "cx", "dx", "si", "di", "bp",
                              "ds", "es")}
        out["flags"] = 0
        return out


def _toolchain_sig() -> str:
    """Fingerprint of the code that produces a core corpus.

    Recorded in the manifest so a consumer can tell WHICH generation of the
    emitter wrote the artifacts it is about to trust.
    """
    import hashlib
    h = hashlib.sha256()
    root = Path(__file__).resolve().parents[1]
    for mod in ("dos_re/lift/emit_abi.py", "dos_re/lift/contracts.py",
                "dos_re/lift/emit_cpuless.py", "tools/abi_promote.py"):
        h.update((root / mod).read_bytes())
    return h.hexdigest()[:16]


def shipped_shape(import_base: str, key: str):
    """Derive the adapter's MechShape from the SHIPPED module it replaces.

    Recomputing the shape with check_promotable proved fragile: the shipped
    corpus was generated with a parameter set we do not fully replay
    (dispatch arrivals, boundary heads, alt-entry `_entry_ip`, observed
    dead exits), and every divergence became a runtime TypeError.  Reading
    the shape off the module we are replacing makes the adapter a drop-in
    BY CONSTRUCTION.

    This is runtime introspection of a generated artifact -- the signature
    via ``__kwdefaults__``, the output keys via one probe call on throwaway
    memory -- NOT parsing generated source, so the IR-first rule stands.
    Refuses loudly if the module cannot be imported or probed.

    MEMOISED: the emitter's fixpoint revisits a candidate once per round, and
    the shape of a shipped module cannot change mid-run.  Without the cache
    every round re-imported and re-probed the whole corpus, which is most of
    why the emission ran for tens of minutes with nothing to show.  Refusals
    are cached too -- a refusal is as stable as a success."""
    import importlib
    ck = (import_base, key)
    hit = _SHAPE_CACHE.get(ck)
    if hit is not None:
        if isinstance(hit, Refusal):
            raise hit
        return hit
    try:
        shape = _shipped_shape_uncached(importlib, import_base, key)
    except Refusal as e:
        _SHAPE_CACHE[ck] = e
        raise
    _SHAPE_CACHE[ck] = shape
    return shape


_SHAPE_CACHE: dict = {}


def _shipped_shape_uncached(importlib, import_base: str, key: str):
    from dos_re.lift import emit_abi as _ea
    stem = f"func_{key.replace(':', '_').lower()}"
    mod = importlib.import_module(f"{import_base}.{stem}")
    fn = getattr(mod, stem)
    pos = fn.__code__.co_varnames[:fn.__code__.co_argcount]
    kwd = dict(fn.__kwdefaults__ or {})
    needs_plat = "plat" in pos
    if "_entry_ip" in kwd:
        raise Refusal("alt-entry (_entry_ip) adapter not modelled")
    inputs = tuple(sorted(k for k in kwd
                          if not k.startswith("_")))
    probe_kw = {k: 0 for k in kwd}
    args = (_ProbeMem(), _ProbePlat()) if needs_plat else (_ProbeMem(),)
    try:
        out, _compat = fn(*args, **probe_kw)
    except _ProbeSpin:
        raise Refusal("probe did not terminate (spins on synthetic memory)")
    except Exception as e:                        # noqa: BLE001
        raise Refusal(f"probe failed ({type(e).__name__})")
    return _ea.MechShape(inputs=inputs, outputs=tuple(sorted(out)),
                         needs_plat=needs_plat,
                         df_livein="_df" in kwd,
                         flags_livein="_flags_in" in kwd)


def _parity_mismatch(import_base: str, key: str, shape) -> str | None:
    """Compare an adapter's intended signature with the SHIPPED mechanical
    module it will replace.  Reads the live function's own metadata
    (``__kwdefaults__`` / code object) -- runtime introspection, NOT parsing
    generated source, so the IR-first rule stands.  Returns a reason string
    on mismatch, or None when the adapter is a genuine drop-in."""
    import importlib
    stem = f"func_{key.replace(':', '_').lower()}"
    try:
        mod = importlib.import_module(f"{import_base}.{stem}")
        fn = getattr(mod, stem)
    except Exception as e:                       # noqa: BLE001
        return f"shipped module unavailable ({type(e).__name__})"
    pos = fn.__code__.co_varnames[:fn.__code__.co_argcount]
    kwd = set((fn.__kwdefaults__ or {}))
    want_kw = set(shape.inputs)
    if shape.needs_plat:
        want_kw.add("_base")
    if shape.df_livein:
        want_kw.add("_df")
    if shape.flags_livein:
        want_kw.add("_flags_in")
    if ("plat" in pos) != shape.needs_plat:
        return f"plat: shipped={'plat' in pos} adapter={shape.needs_plat}"
    if kwd != want_kw:
        missing = sorted(kwd - want_kw)
        extra = sorted(want_kw - kwd)
        return (f"kwargs differ (shipped-only={missing}, adapter-only={extra})")
    return None


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
    # cores that ARE emitted and verified but get no integration adapter, so
    # the game keeps calling the mechanical function for them.  Distinct from
    # `refused`: the core exists, only the drop-in scaffold is missing.
    not_integrated: dict[str, str] = {}
    mech_cache: dict = {}          # mechanical specs, closure-resolved

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
        # Emit progress per round.  All the summary output lands after the
        # fixpoint, so a slow run was previously indistinguishable from a
        # hang -- silence that costs tens of minutes to interpret.
        print(f"  round {rounds}: {len(cores)} cores, {len(refused)} refused, "
              f"{len(wanted) - len(cores) - len(refused)} undecided",
              flush=True)
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
            shape = None
            if args.integrate:
                # Shape taken FROM the shipped module we replace, so the
                # adapter is a drop-in by construction rather than by a
                # recomputation we would have to keep in lockstep.
                #
                # An adapter refusal must NOT cost us the CORE.  The core is
                # the recovery artifact -- differentially proven, and useful
                # to every later stage -- while the adapter is only the
                # scaffold that lets the still-mechanical graph call it.  An
                # earlier version dropped the core too, silently shrinking a
                # verified 113-core corpus to 87 for a reason that had
                # nothing to do with the core's correctness.
                try:
                    shape = shipped_shape(args.import_base, key)
                except Refusal as e:
                    not_integrated[key] = str(e)
            try:
                src, contract = emit_abi.emit_abi_core(
                    scan, prop, key, name=_name(prop),
                    callees=callee_map, far_callees=far_map,
                    abi_base=args.abi_base, mech_shape=shape,
                    boundary_addrs={ip for (hc, ip) in heads if hc == cs},
                    dispatch_addrs={ip for (hc, ip) in disp if hc == cs},
                    # the layout fact travels WITH the census that used it,
                    # so the emitter cannot silently apply a different floor
                    ss_globals_floor=census.get("ss_globals_floor"))
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
        stale = [p for p in out.glob("core_*.py")
                 if p.name not in keep and p.name != "core_loader.py"]
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
                    # WHICH TOOLCHAIN WROTE THIS.  A long-running emission
                    # started against an older tree can land much later and
                    # overwrite a newer corpus -- silently, because the output
                    # is a valid manifest, just not the current one.  That
                    # happened: a background run from an earlier generation
                    # replaced a 143-core corpus with its own 90-core one, and
                    # the differential then verified the wrong artifact and
                    # published a green cache.  Stamping the generation lets a
                    # consumer NOTICE.
                    "toolchain_sig": _toolchain_sig(),
                    "cores": sorted(cores),
                    # the CLASSIFIED EXCEPTION list: every function kept
                    # mechanical, with the exact capability that blocked it.
                    # tools/abi_gate.py reports these as the classes that
                    # still owe a generated representation.
                    "refused": {k: v for k, v in sorted(refused.items())},
                    # emitted + verified, but no drop-in adapter: the game
                    # still calls the mechanical function for these.  They are
                    # NOT refusals -- the core is real -- but they do not
                    # count as integrated, and the honest integration figure
                    # is len(cores) - len(not_integrated).
                    # scoped to EMITTED cores: shipped_shape runs for every
                    # candidate, but a candidate that is also `refused` has no
                    # core to integrate and would inflate this count (it read
                    # 113 when only 24 cores were affected).
                    "not_integrated": {k: v for k, v
                                       in sorted(not_integrated.items())
                                       if k in cores}}
        (out / "cores_manifest.json").write_text(
            json.dumps(manifest, indent=1), encoding="utf-8")
        if args.integrate:
            # Only adapter-backed cores may be installed: a stem in STEMS
            # without a mechanical-shaped entry would fail to substitute.
            installable = sorted(k for k in cores if k not in not_integrated)
            (out / "core_loader.py").write_text(
                emit_abi.emit_core_loader(installable,
                                          abi_base=args.abi_base,
                                          import_base=args.import_base),
                encoding="utf-8")
            print(f"  wrote core_loader.py -- {len(installable)} of "
                  f"{len(cores)} cores are drop-in installable")
            blocked = {k: v for k, v in not_integrated.items() if k in cores}
            if blocked:
                print("  emitted but NOT integrated (core is real, adapter "
                      "is not modelled):")
                for reason, n in Counter(blocked.values()).most_common():
                    print(f"    {reason:<44} {n:4d}")
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
    ap.add_argument("--integrate", action="store_true",
                    help="also emit a MECHANICAL-shaped adapter per core (so "
                         "the still-mechanical runtime can call it) plus the "
                         "core loader.  Without this the cores are proven "
                         "only in isolation -- nothing executes them.")
    ap.add_argument("--check-parity", action="store_true", default=True,
                    help="verify each integration adapter's signature matches "
                         "the shipped mechanical module it replaces (default "
                         "on); refuse rather than fail mid-demo")
    ap.add_argument("--no-check-parity", dest="check_parity",
                    action="store_false")
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
