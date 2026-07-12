"""pmlift — census, lift and in-situ verify functions of a DOS/4GW (LE) title.

The protected-mode liftgen+liftverify in one CLI, over the flat 386 runtime:

* ``--census``: scan entries (static reachability over decode32) and report
  which are mechanically liftable and why the rest refuse.
* ``--verify``: emit each liftable entry as a literal Python hook, install
  them with the strict PM differential verifier, run the program, and report
  per-hook verified-call counts — ORACLE_PASSING / DIVERGED / NOT_REACHED.

Entries come from ``--entry 0xADDR`` (repeatable), ``--entries-file``, or
``--auto-entries N``: a static sweep of the code object collecting direct
near-call targets (Watcom C's regular call graph makes this a good first
census; indirect-only functions need runtime discovery later).

Usage:
    python tools/pmlift.py --exe GAME.EXE --auto-entries 200 --census
    python tools/pmlift.py --exe GAME.EXE --boot-steps 30000000 \\
        --entry 0x119E35 --verify --steps 5000000 [--emit-dir lifted32]

Addresses are RUNTIME (rebased) flat linear addresses — le_info.py maps
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
    ap.add_argument("--census", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--steps", type=int, default=5_000_000,
                    help="verify: instructions to run with hooks installed")
    ap.add_argument("--emit-dir", default="", help="write emitted hook sources here")
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

    if not args.verify:
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
    except Exception as exc:  # noqa: BLE001 — fail-loud frontier is a result too
        print(f"stopped: {type(exc).__name__}: {exc} at eip=0x{rt.cpu.eip:X}")

    for e in sorted(installed):
        n = verifier.calls_per_hook.get(e, 0)
        retired = " (retired at samples cap)" if e in rt.cpu.hook_verifier_passthrough else ""
        print(f"  0x{e:X} {rt.cpu.hook_names[e]}: "
              f"{'ORACLE_PASSING x' + str(n) + retired if n else 'NOT_REACHED'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
