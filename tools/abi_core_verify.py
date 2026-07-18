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
import importlib
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path.cwd()))

from dos_re.lift import emit_cpuless  # noqa: E402
from dos_re.lift.abi_diff import diff_one  # noqa: E402
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ir", required=True)
    ap.add_argument("--abi-dir", required=True)
    ap.add_argument("--abi-base", required=True)
    ap.add_argument("--census", required=True)
    ap.add_argument("--states", type=int, default=64)
    args = ap.parse_args(argv)

    ir = json.loads(Path(args.ir).read_text(encoding="utf-8"))
    census = json.loads(Path(args.census).read_text(encoding="utf-8"))
    manifest = json.loads((Path(args.abi_dir) / "cores_manifest.json")
                          .read_text(encoding="utf-8"))
    keys = manifest["cores"]
    passed, raised_states = 0, 0
    spin_notes: list[str] = []
    failures: dict[str, list[str]] = {}
    mech_cache: dict = {}
    for key in keys:
        stem = key.replace(":", "_").lower()
        mech_fn, _ = _fresh_mechanical(ir, key, mech_cache)
        core_mod = importlib.import_module(f"{args.abi_base}.core_{stem}")
        rep = diff_one(mech_fn, core_mod._abi_core,
                       census["functions"][key], states=args.states)
        raised_states += rep["raised"]
        if rep.get("note"):
            spin_notes.append(f"{key}: {rep['note']}")
        if rep["ok"]:
            passed += 1
        else:
            failures[key] = rep["mismatches"][:3]

    print(f"ABI-core differential over {len(keys)} de-stacked cores, "
          f"{args.states} seeded states each (mechanical reference emitted "
          f"fresh from the IR):")
    print(f"  IDENTICAL to mechanical  {passed:4d}")
    print(f"  states raising on both sides (compared equal): "
          f"{raised_states}")
    for n in spin_notes:
        print(f"  note: {n}")
    if failures:
        print(f"  MISMATCHED               {len(failures):4d}")
        for key, ms in sorted(failures.items()):
            print(f"    {key}:")
            for m in ms:
                print(f"      {m}")
        return 1
    print("ABI-CORE DIFFERENTIAL PASSED: every de-stacked core IS its "
          "mechanical function on every driven state.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
