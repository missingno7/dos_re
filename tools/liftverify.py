"""liftverify — M2: prove lifted hooks against the ASM oracle, in situ.

The pipeline that turns an emitted hook into a trusted one. Point it at a
snapshot (a moment where the target functions run) and a set of entries; it
emits each lifted hook, installs them all with the framework's strict
auto-continuation verifier, runs the VM forward, and — every time a lifted
function executes — interprets the ORIGINAL ASM from the same pre-state to the
hook's own continuation and diffs the full machine state. No hand-written
continuation metadata, no game-specific harness.

Reports, and writes into a lift manifest (dos_re.lift.manifest — the lifter's
own proof ledger, kept separate from recovered-source islands):
    ORACLE_PASSING  the first --samples calls (per hook) were byte-exact
                    (+ block coverage M/K).  NOTE: this is a SAMPLE — verification
                    retires each hook once it hits the --samples cap, so calls
                    beyond it, and blocks the run never reached (M<K, flagged
                    "PARTIAL COVERAGE"), are unproven.  A hook can pass here and
                    still differ on an unsampled deeper path; for whole-run
                    assurance raise --samples/--steps or use the frame-verifier.
    DIVERGED        a call differed from the ASM oracle (details printed)
    NOT_REACHED     never executed in this run — pick a snapshot where it does

Usage:
    python tools/liftverify.py --exe GAME.EXE --snapshot DIR \\
        --entry 1010:4537 [--entry ...] [--entries-file F] \\
        [--steps 5000000] [--emit-dir lifted] [--manifest lifted/manifest.json]

This is the optional accelerator a porting agent reaches for: instead of
hand-translating a routine and hoping, lift it, run this, and either get a
verified replacement island for free or a precise divergence to look at.

Scope: a PER-SLICE tool. Each sampled call re-interprets the original ASM to
the hook's continuation, so verify a handful of related entries at a time
(the "one routine, one verification, per slice" loop) — not the whole census
in one process. `liftgen --report` is the tool for whole-census questions.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dos_re.cpu import HaltExecution, UnsupportedInstruction  # noqa: E402
from dos_re.interrupts import deliver_interrupt  # noqa: E402
from dos_re.lift import scan_function  # noqa: E402
from dos_re.lift.emit import EmitUnsupported, emit_function  # noqa: E402
from dos_re.lift.manifest import LiftManifest, LiftRecord  # noqa: E402
from dos_re.lift.runtime import LiftRuntimeError  # noqa: E402
from dos_re.repro_artifacts import clone_runtime_state  # noqa: E402
from dos_re.snapshot import load_snapshot, parse_addr  # noqa: E402
from dos_re.verification import (HookVerifierConfig, HookVerifyDivergence,  # noqa: E402
                                 install_hook_verifier)


def _probe(rt, cs):
    scratch = clone_runtime_state(rt)
    cpu = scratch.cpu
    cpu.replacement_hooks.clear(); cpu.hook_names.clear()
    cpu.hook_verifier = None; cpu.trace_enabled = False; cpu.pending_irq = None

    def probe(ip):
        ip &= 0xFFFF
        cpu.s.cs, cpu.s.ip = cs & 0xFFFF, ip
        try:
            cpu.step()
        except Exception:  # noqa: BLE001
            return None
        return ((cpu.s.ip - ip) & 0xFFFF) or None
    return probe


def _native_pct(src: str) -> float:
    body = [ln for ln in src.splitlines() if ln.lstrip().startswith("# ") and ":" in ln]
    n = len(body)
    fb = src.count("(interpreter fallback)")
    return 100.0 * (n - fb) / n if n else 0.0


def _load_hook(module_path: Path):
    spec = importlib.util.spec_from_file_location(module_path.stem, module_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn = getattr(mod, module_path.stem)
    return mod, fn


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--exe", required=True)
    p.add_argument("--snapshot", required=True)
    p.add_argument("--game-root", default=None)
    p.add_argument("--entry", action="append", default=[], metavar="CS:IP")
    p.add_argument("--entries-file")
    p.add_argument("--steps", type=int, default=5_000_000,
                   help="how far to run the VM forward from the snapshot (the target "
                        "functions must execute within this budget to be verified)")
    p.add_argument("--samples", type=int, default=20,
                   help="differential-verify each function this many times, then let "
                        "it run freely (each verification clones + re-runs the ASM "
                        "oracle, so a hot function would otherwise crawl). Per-hook, "
                        "so one hot function never starves the sample budget of the "
                        "others -- that sample is what proves the hook.")
    p.add_argument("--drive", metavar="MOD:FUNC", default=None,
                   help="per-frame drive callback FUNC(rt, frame), called before "
                        "each frame's timer IRQs -- lets a game feed menu keys / "
                        "mouse so verification runs along a real session instead "
                        "of idling at one snapshot (requires --timer-irqs)")
    p.add_argument("--verify-timeout", type=float, default=8.0,
                   help="wall-clock seconds a single ASM-oracle re-run may take before "
                        "that verification is abandoned (a function that reaches deep "
                        "into the program can be slow to re-interpret). Keep batches "
                        "small -- this is a per-slice tool, not a whole-census sweep.")
    p.add_argument("--timer-irqs", type=int, default=0, metavar="N",
                   help="deliver N INT 08h timer interrupts per frame while running "
                        "forward (0 = none). Interrupt-gated code -- timer ISRs and "
                        "everything they call -- is never reached by a plain forward "
                        "run; this is how you exercise it (mirror the game's own "
                        "frontend, e.g. skyroads uses 6).")
    p.add_argument("--frame-steps", type=int, default=30_000, metavar="N",
                   help="(with --timer-irqs) VM instructions between IRQ bursts")
    p.add_argument("--emit-dir", default="lifted",
                   help="where the generated hook modules are written / read")
    p.add_argument("--max-iterations", type=int, default=None, metavar="N",
                   help="raise the lifted hook's own runaway guard above the emitter's "
                        "default (still at least instructions*5000 either way). Use "
                        "when a hit MAX_ITERATIONS is a real large data-driven loop, "
                        "not a decode bug -- the static census already cross-checks "
                        "instruction lengths, so a bigger budget is the fix, not a "
                        "hand rewrite.")
    p.add_argument("--manifest", default=None,
                   help="lift proof ledger to update (default: <emit-dir>/manifest.json)")
    p.add_argument("--install-passing", action="store_true",
                   help="after the run, print the import line to install every "
                        "ORACLE_PASSING hook (does not modify game code itself)")
    args = p.parse_args(argv)

    entries = [parse_addr(e) for e in args.entry]
    if args.entries_file:
        for line in Path(args.entries_file).read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                entries.append(parse_addr(line))
    if not entries:
        p.error("no entries (--entry / --entries-file)")

    emit_dir = Path(args.emit_dir)
    manifest_path = Path(args.manifest) if args.manifest else emit_dir / "manifest.json"
    manifest = LiftManifest.load(manifest_path)

    rt = load_snapshot(args.exe, args.snapshot, game_root=args.game_root)
    rt.cpu.trace_enabled = False

    # 1. Emit (or reuse) + load each lifted hook, install as a replacement.
    hooks: dict[tuple[int, int], object] = {}
    modules = {}
    records: dict[str, LiftRecord] = {}
    for cs, ip in entries:
        entry = f"{cs:04X}:{ip:04X}"
        name = f"lifted_{cs:04x}_{ip:04x}"
        module_path = emit_dir / f"{name}.py"
        mem = rt.cpu.mem
        scan = scan_function(lambda off: mem.rb(cs, off & 0xFFFF), ip, probe=_probe(rt, cs))
        if not scan.liftable:
            reasons = ",".join(sorted({r.reason for r in scan.refusals}))
            print(f"skip     {entry}: not liftable ({reasons})")
            continue
        block_end = min((i.next_ip for i in scan.insts.values()
                         if i.kind != "seq" and i.ip >= ip), default=(ip + 8) & 0xFFFF)
        sig = bytes(mem.rb(cs, (ip + k) & 0xFFFF) for k in range(max(4, min(16, (block_end - ip) & 0xFFFF))))
        try:
            src = emit_function(scan, cs, name, signature=sig, coverage=True,
                                min_iterations=args.max_iterations)
        except EmitUnsupported as exc:
            print(f"skip     {entry}: emit-unsupported ({exc})")
            continue
        emit_dir.mkdir(parents=True, exist_ok=True)
        module_path.write_text(src, encoding="utf-8")
        mod, fn = _load_hook(module_path)
        modules[entry] = mod
        hooks[(cs, ip)] = fn
        rt.cpu.replacement_hooks[(cs, ip)] = fn
        rt.cpu.hook_names[(cs, ip)] = name
        records[entry] = LiftRecord(
            entry=entry, module=module_path.name, status="LIFTED",
            instructions=len(scan.insts), blocks=len(scan.block_leaders()),
            native_pct=round(_native_pct(src), 1))

    if not hooks:
        print("nothing liftable to verify")
        return 1

    # 2. Strict, metadata-free differential verification. verify_all=False +
    #    an explicit hook set lets us retire a hook from verification (leaving it
    #    running) once it has been sampled enough — per-hook fairness, so a hot
    #    function never starves the others' sample budget.
    to_verify = set(hooks)
    if len(hooks) > 12:
        print(f"note: verifying {len(hooks)} functions at once is slow (each sampled "
              f"call re-runs the ASM oracle). liftverify is a per-slice tool -- prefer "
              f"batches of a handful of entries.\n")
    verifier = install_hook_verifier(
        rt, HookVerifierConfig.strict(hooks=to_verify, asm_wall_timeout_s=args.verify_timeout),
        stops={})

    # 3. Run the VM forward in chunks; between chunks, retire fully-sampled
    #    hooks from the verify set. The verifier reads config.hooks live.
    diverged: dict[tuple[int, int], str] = {}
    runaway: dict[tuple[int, int], str] = {}
    name_to_key = {v: k for k, v in rt.cpu.hook_names.items() if k in hooks}
    status = "budget reached"
    steps_done = 0
    chunk = args.frame_steps if args.timer_irqs else 200_000
    drive_fn = None
    if args.drive:
        import importlib
        import os as _os
        if _os.getcwd() not in sys.path:
            sys.path.insert(0, _os.getcwd())
        mod_name, _, fn_name = args.drive.partition(":")
        if not fn_name:
            p.error("--drive needs MOD:FUNC")
        drive_fn = getattr(importlib.import_module(mod_name), fn_name)
    frame = 0
    try:
        while steps_done < args.steps:
            try:
                if drive_fn is not None:
                    drive_fn(rt, frame)
                frame += 1
                if args.timer_irqs:
                    for _ in range(args.timer_irqs):
                        deliver_interrupt(rt, 0x08)
                steps_done += rt.cpu.run(min(chunk, args.steps - steps_done))
            except LiftRuntimeError as exc:
                # A lifted hook ran away (unbounded internal wait — a poor lift
                # target). Retire just that hook and keep verifying the rest.
                bad = next((k for nm, k in name_to_key.items() if nm in str(exc)), None)
                if bad is None:
                    raise
                runaway[bad] = str(exc)
                rt.cpu.replacement_hooks.pop(bad, None)
                rt.cpu.hook_names.pop(bad, None)
                to_verify.discard(bad)
                print(f"runaway  {bad[0]:04X}:{bad[1]:04X}: {exc}")
                continue
            for key in list(to_verify):
                if verifier.counts.get(key, 0) >= args.samples:
                    to_verify.discard(key)          # keep running it, stop verifying
    except HookVerifyDivergence as exc:
        status = "divergence"
        print(f"\nDIVERGENCE: {exc}")
        marker = f"{rt.cpu.s.cs:04X}:"          # exception text names the failing hook
        for key in hooks:
            if f"{key[0]:04X}:{key[1]:04X}" in str(exc):
                diverged[key] = str(exc)
        if not diverged:                        # fall back to whatever was mid-verify
            for key in hooks:
                if verifier.counts.get(key):
                    diverged.setdefault(key, str(exc))
    except HaltExecution:
        status = "program halted"
    except UnsupportedInstruction as exc:
        status = f"unsupported instruction: {exc}"
    except Exception as exc:  # noqa: BLE001
        status = f"exception: {type(exc).__name__}: {exc}"

    # 4. Report + update the ledger.
    print(f"\nran {steps_done:,} steps ({status})\n")
    passing = []
    for (cs, ip), fn in hooks.items():
        entry = f"{cs:04X}:{ip:04X}"
        rec = records[entry]
        verified = verifier.counts.get((cs, ip), 0)
        seen, total = modules[entry].coverage()   # coverage counts EVERY run, verified or not
        rec.calls = verified
        rec.verified = verified
        rec.blocks_covered = seen
        if (cs, ip) in runaway:
            rec.status, rec.note = "DIVERGED", "runaway: " + runaway[(cs, ip)][:180]
            rec.divergences = 1
        elif (cs, ip) in diverged:
            rec.status, rec.divergences, rec.note = "DIVERGED", 1, diverged[(cs, ip)][:200]
        elif verified > 0:
            rec.status = "ORACLE_PASSING"
            passing.append((entry, rec))
        elif seen > 0:
            # It executed but the run ended before a sample was taken (rare with
            # per-hook fairness) — honestly not yet proven.
            rec.status, rec.note = "LIFTED", "ran but not sampled; raise --steps/--samples"
        else:
            rec.status = "NOT_REACHED"
        manifest.put(rec)
        cov = f"{seen}/{total} blk" if seen else "-"
        flag = {"ORACLE_PASSING": "PASS    ", "DIVERGED": "DIVERGED",
                "NOT_REACHED": "notreach", "LIFTED": "ran/uvf "}[rec.status]
        partial = "  (PARTIAL COVERAGE)" if rec.status == "ORACLE_PASSING" and not rec.fully_covered else ""
        # Verification retires the hook at the --samples cap; if it was hit, later
        # calls (if any) went unchecked — a pass here is a sample, not a full proof.
        capped = ("  (hit --samples cap; later calls unverified)"
                  if rec.status == "ORACLE_PASSING" and verified >= args.samples else "")
        print(f"{flag} {entry}  verified={verified:<4} {cov:<10} "
              f"native={rec.native_pct:.0f}%{partial}{capped}")

    manifest.save(manifest_path)
    npass, ndiv = len(passing), len(diverged)
    print(f"\n{npass} ORACLE_PASSING, {ndiv} DIVERGED / {len(hooks)} lifted; "
          f"manifest: {manifest_path}")
    if passing and args.install_passing:
        print("\nto install the passing hooks, register them in your adapter's hooks.py:")
        for entry, rec in passing:
            print(f"  from {emit_dir.name}.{rec.module[:-3]} import {rec.module[:-3]}  "
                  f"# @ {entry}")
    return 0 if not diverged else 1


if __name__ == "__main__":
    raise SystemExit(main())
