"""pmlift -- census, lift and in-situ verify functions of a DOS/4GW (LE) title.

The protected-mode liftgen+liftverify in one CLI, over the flat 386 runtime:

* ``--census``: scan entries (static reachability over decode32) and report
  which are mechanically liftable and why the rest refuse.
* ``--verify``: emit each liftable entry as a literal Python hook, install
  them with the strict PM differential verifier, run the program, and report
  per-hook verified-call counts -- ORACLE_PASSING / DIVERGED / NOT_REACHED.

Entries come from ``--entry 0xADDR`` (repeatable), ``--entries-file``, or
``--auto-entries N``: a static sweep of the code object collecting direct
near-call targets (Watcom C's regular call graph makes this a good first
census; indirect-only functions need runtime discovery later).

Usage:
    python tools/pmlift.py --exe GAME.EXE --auto-entries 200 --census
    python tools/pmlift.py --exe GAME.EXE --boot-steps 30000000 \\
        --entry 0x119E35 --verify --steps 5000000 [--emit-dir lifted32]

Addresses are RUNTIME (rebased) flat linear addresses -- le_info.py maps
link<->runtime (+0x100000 by default).
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dos_re.runtime import create_pm_runtime            # noqa: E402
from dos_re.pm_snapshot import load_pm_snapshot          # noqa: E402
from dos_re.pm_verification import (PMHookVerifyDivergence,  # noqa: E402
                                    install_pm_hook_verifier)
from dos_re.lift.cfg32 import scan_function32            # noqa: E402
from dos_re.lift.decode32 import decode32                 # noqa: E402
from dos_re.lift.emit32 import EmitUnsupported, emit_function32  # noqa: E402
from dos_re.cpu import HaltExecution                      # noqa: E402


def auto_entries(rt, limit: int) -> list[int]:
    """Static sweep: decode the executable object linearly, collect direct
    near-call targets, most-called first."""
    targets: Counter[int] = Counter()
    read = rt.mem.data.__getitem__
    for obj in rt.image.objects:
        if not obj.executable or not obj.is_32bit:
            continue
        ip = obj.base
        end = obj.base + obj.virtual_size
        while ip < end:
            try:
                inst = decode32(read, ip)
            except ValueError:
                ip += 1
                continue
            if inst.kind == "call" and inst.target is not None \
                    and obj.base <= inst.target < end:
                targets[inst.target] += 1
            ip += inst.length
    return [t for t, _ in targets.most_common(limit)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--exe", required=True)
    ap.add_argument("--snapshot", default="", help="resume from a PM snapshot dir")
    ap.add_argument("--boot-steps", type=int, default=0,
                    help="fresh boot: instructions to run before installing hooks")
    ap.add_argument("--boot-keys", default="20",
                    help="hex ASCII bytes seeded into the DOS key queue at boot")
    ap.add_argument("--entry", action="append", default=[],
                    help="flat runtime address (hex), repeatable")
    ap.add_argument("--entries-file", default="")
    ap.add_argument("--auto-entries", type=int, default=0,
                    help="collect N most-called direct call targets statically")
    ap.add_argument("--exclude", action="append", default=[],
                    help="flat address (hex) to exclude from lifting, repeatable "
                         "-- environment-wait loops (vsync/timer/key polls) whose "
                         "exit condition only IRQ servicing can satisfy must run "
                         "interpreted (or be recovered by hand)")
    ap.add_argument("--census", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--steps", type=int, default=5_000_000,
                    help="verify: instructions to run with hooks installed")
    ap.add_argument("--emit-dir", default="", help="write emitted hook sources here")
    ap.add_argument("--emit-graph", default="",
                    help="emit a LINKED generated graph here (lift_<hex>.py modules "
                         "with LINKS tables; direct calls between all-near-ret "
                         "lifted functions bypass the interpreter). --verify then "
                         "installs it via activate_generated_graph32.")
    ap.add_argument("--auto-exclude-waits", type=int, default=0,
                    help="graph verify: on an environment-wait MAX_ITERATIONS "
                         "stop, exclude that function, re-emit, reboot and rerun "
                         "-- up to N rounds; prints the wait inventory found")
    args = ap.parse_args(argv)

    if args.snapshot:
        rt = load_pm_snapshot(args.exe, args.snapshot)
    else:
        rt = create_pm_runtime(args.exe)
        for tok in filter(None, args.boot_keys.split(",")):
            rt.dos.key_queue.append(int(tok, 16))
        if args.boot_steps:
            rt.cpu.run(args.boot_steps)
            print(f"booted {rt.cpu.instruction_count} instructions")

    entries = [int(e, 16) for e in args.entry]
    if args.entries_file:
        for line in Path(args.entries_file).read_text().split():
            entries.append(int(line, 16))
    if args.auto_entries:
        entries.extend(auto_entries(rt, args.auto_entries))
    entries = list(dict.fromkeys(entries))
    excluded = {int(e, 16) for e in args.exclude}
    if excluded:
        entries = [e for e in entries if e not in excluded]
    if not entries:
        ap.error("no entries: use --entry/--entries-file/--auto-entries")

    fetch = rt.mem.data.__getitem__
    scans = {}
    reasons: Counter[str] = Counter()
    for entry in entries:
        scan = scan_function32(fetch, entry)
        scans[entry] = scan
        for r in scan.refusals:
            reasons[r.reason] += 1
    liftable = [e for e, s in scans.items() if s.liftable]
    print(f"census: {len(entries)} entries, {len(liftable)} liftable "
          f"({100 * len(liftable) // max(1, len(entries))}%)")
    if reasons:
        print("refusals: " + ", ".join(f"{k}={v}" for k, v in reasons.most_common()))

    if args.census and not args.verify:
        for e, s in sorted(scans.items()):
            status = "LIFTABLE" if s.liftable else \
                ",".join(sorted({r.reason for r in s.refusals}))
            lo, hi = s.region
            print(f"  0x{e:X}: {len(s.insts)} insts [{lo:X}..{hi:X}] {status}")
        return 0

    if not (args.verify or args.emit_graph):
        return 0

    def make_runtime():
        """A fresh runtime at the same start state (deterministic boot)."""
        if args.snapshot:
            return load_pm_snapshot(args.exe, args.snapshot)
        fresh = create_pm_runtime(args.exe)
        for tok in filter(None, args.boot_keys.split(",")):
            fresh.dos.key_queue.append(int(tok, 16))
        if args.boot_steps:
            fresh.cpu.run(args.boot_steps)
        return fresh

    def report(installed, verifier, cpu) -> None:
        for e in sorted(installed):
            n = verifier.calls_per_hook.get(e, 0)
            retired = (" (retired at samples cap)"
                       if e in cpu.hook_verifier_passthrough else "")
            print(f"  0x{e:X} {cpu.hook_names[e]}: "
                  f"{'ORACLE_PASSING x' + str(n) + retired if n else 'NOT_REACHED'}")

    if args.emit_graph:
        # LINKED GRAPH: only all-near-RET-exit callees are safe to link (a
        # tail-jump/IRET callee hands control elsewhere, so call_linked32's
        # returned-to-ret_ip contract would not hold); everything else stays
        # emulate_call32 (the hook at the target still dispatches).
        import re as _re
        from dos_re.lift.decode import RET
        from dos_re.lift.install import activate_generated_graph32
        graph_dir = Path(args.emit_graph)
        graph_dir.mkdir(parents=True, exist_ok=True)
        linkable_all = {e for e in liftable
                        if all(x.kind == RET for x in scans[e].exits)}

        def emit_graph() -> None:
            lift_now = [e for e in liftable if e not in excluded]
            linkable = linkable_all - excluded
            emitted = links_total = 0
            emitted_names: set[str] = set()
            for e in lift_now:
                name = f"lift_{e:x}"
                sig = bytes(rt.mem.data[e:e + 8])
                link_targets = sorted(t for t in scans[e].calls_near
                                      if t in linkable and t != e)
                link_map = {t: f'LINKS["0x{t:X}"]' for t in link_targets}
                link_imports: tuple[str, ...] = ()
                if link_map:
                    table = ", ".join(f'"0x{t:X}": None' for t in link_targets)
                    link_imports = ("LINKS = {%s}  # filled by resolve_links32" % table,)
                try:
                    src = emit_function32(scans[e], name, signature=sig,
                                          link_map=link_map, link_imports=link_imports)
                except EmitUnsupported as exc:
                    print(f"  0x{e:X}: emit refused: {exc}")
                    continue
                (graph_dir / f"{name}.py").write_text(src)
                emitted_names.add(f"{name}.py")
                emitted += 1
                links_total += len(link_map)
            # The activator binds every lift_*.py in the directory, so a module
            # from a previous emission (e.g. an entry now excluded) would
            # silently rejoin the graph -- remove what this emission didn't write.
            for stale in sorted(graph_dir.glob("lift_*.py")):
                if stale.name not in emitted_names:
                    stale.unlink()
            print(f"graph: emitted {emitted} modules -> {graph_dir} "
                  f"({len(linkable)} linkable callees, {links_total} linked call sites)")

        emit_graph()
        if not args.verify:
            return 0
        # Verify rounds: an environment-wait loop (vsync/timer/key poll whose
        # exit only IRQ servicing can satisfy) surfaces as MAX_ITERATIONS; each
        # round excludes it, re-emits, reboots, and goes deeper -- building the
        # port's wait-function inventory (the hand-recovery worklist).
        from dos_re.lift.runtime32 import LiftRuntimeError
        found_waits: list[int] = []
        for round_no in range(max(1, args.auto_exclude_waits + 1)):
            if round_no:
                emit_graph()
                rt = make_runtime()          # noqa: F841 -- rebind for this round
            installed = {e: 0 for e in activate_generated_graph32(rt.cpu, graph_dir)}
            print(f"round {round_no}: activated {len(installed)} functions; "
                  f"running {args.steps} steps under the differential verifier")
            verifier = install_pm_hook_verifier(rt)
            try:
                rt.cpu.run(args.steps)
            except PMHookVerifyDivergence as exc:
                print(f"DIVERGED: {exc}")
                return 1
            except HaltExecution:
                pass
            except LiftRuntimeError as exc:
                m = _re.match(r"lift_([0-9a-f]+) ", str(exc))
                if m and round_no < args.auto_exclude_waits:
                    wait_entry = int(m.group(1), 16)
                    excluded.add(wait_entry)
                    found_waits.append(wait_entry)
                    print(f"round {round_no}: environment wait at "
                          f"0x{wait_entry:X} -- excluded, restarting")
                    continue
                print(f"stopped: {exc} at eip=0x{rt.cpu.eip:X}")
            except Exception as exc:  # noqa: BLE001 -- fail-loud frontier is a result too
                print(f"stopped: {type(exc).__name__}: {exc} at eip=0x{rt.cpu.eip:X}")
            report(installed, verifier, rt.cpu)
            break
        if found_waits:
            print("environment-wait inventory (hand-recovery worklist): "
                  + ", ".join(f"0x{e:X}" for e in found_waits))
        return 0

    emit_dir = Path(args.emit_dir) if args.emit_dir else None
    if emit_dir:
        emit_dir.mkdir(parents=True, exist_ok=True)
    installed = {}
    for e in liftable:
        name = f"lift_{e:x}"
        sig = bytes(rt.mem.data[e:e + 8])
        try:
            src = emit_function32(scans[e], name, signature=sig)
        except EmitUnsupported as exc:
            print(f"  0x{e:X}: emit refused: {exc}")
            continue
        if emit_dir:
            (emit_dir / f"{name}.py").write_text(src)
        ns: dict = {}
        exec(compile(src, f"<lift 0x{e:X}>", "exec"), ns)
        rt.cpu.replacement_hooks[e] = ns[name]
        rt.cpu.hook_names[e] = name
        installed[e] = 0
    print(f"installed {len(installed)} lifted hooks; running {args.steps} steps "
          f"under the differential verifier")

    verifier = install_pm_hook_verifier(rt)
    try:
        rt.cpu.run(args.steps)
    except PMHookVerifyDivergence as exc:
        print(f"DIVERGED: {exc}")
        return 1
    except HaltExecution:
        pass
    except Exception as exc:  # noqa: BLE001 -- fail-loud frontier is a result too
        print(f"stopped: {type(exc).__name__}: {exc} at eip=0x{rt.cpu.eip:X}")
    report(installed, verifier, rt.cpu)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
