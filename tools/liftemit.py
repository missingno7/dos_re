#!/usr/bin/env python3
"""liftemit — batch-emit selected Recovery IR entries to lifted Python.

Given Recovery IR, or an entry list plus a code-byte snapshot, emit a generated
implementation module per entry into ``--emit-dir``. Emission is a reproducible
implementation recipe; it neither selects these modules for execution nor
claims verification. ``liftverify`` and replay verification produce the
corresponding evidence when needed.

Emission uses ``liftverify``'s exact signature recipe and iteration default,
but coverage instrumentation (per-block BLOCKS_SEEN bookkeeping) is OFF by
default: this tool emits the PRODUCTION corpus the assembled graph installs,
and per-call/per-block instrumentation is verification-tier overhead (it made
the Lemmings fade paths measurably slower).  Pass ``--coverage`` to emit
byte-identically to liftverify's modules when a proof pass will reuse them.

Every emitted module is scanned for ``interp_one`` call sites. The count is
always reported; ``--require-no-interpreter-fallback`` makes any nonzero count
a hard failure when a selected implementation set declares that property.

Usage:
    python tools/liftemit.py --exe GAME.EXE --snapshot DIR \
        --entries-file entries.txt [--emit-dir lifted] [--max-iterations N] \
        [--require-no-interpreter-fallback]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dos_re.lift import scan_function  # noqa: E402
from dos_re.lift.emit import EmitUnsupported, emit_function  # noqa: E402
from dos_re.snapshot import load_snapshot, parse_addr  # noqa: E402


def _probe(rt, cs):
    """Interpreter length-probe over a scratch clone (identical to liftverify /
    liftlink) — the scanner uses it to resolve instruction lengths the pure
    static decode is unsure about."""
    from dos_re.snapshot import clone_runtime_state
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


def emit_entry(mem, rt, cs: int, ip: int, emit_dir: Path, max_iterations,
               coverage: bool = False, drop_dead_flags: bool = False,
               boundary_heads: frozenset = frozenset(),
               dispatch_entries: frozenset = frozenset()):
    """Emit one entry.  Returns ("ok"|"not-liftable"|"emit-unsupported", detail)."""
    name = f"lifted_{cs:04x}_{ip:04x}"
    scan = scan_function(lambda off: mem.rb(cs, off & 0xFFFF), ip, probe=_probe(rt, cs))
    if not scan.liftable:
        return "not-liftable", ",".join(sorted({r.reason for r in scan.refusals}))
    block_end = min((i.next_ip for i in scan.insts.values()
                     if i.kind != "seq" and i.ip >= ip), default=(ip + 8) & 0xFFFF)
    sig = bytes(mem.rb(cs, (ip + k) & 0xFFFF)
                for k in range(max(4, min(16, (block_end - ip) & 0xFFFF))))
    dead = frozenset()
    if drop_dead_flags:
        from dos_re.lift.analyze import dead_flag_sites
        dead = frozenset(dead_flag_sites(scan))
    try:
        src = emit_function(scan, cs, name, signature=sig, coverage=coverage,
                            count_instructions=True, dead_flag_ips=dead,
                            boundary_heads=frozenset(
                                hip for hcs, hip in boundary_heads
                                if hcs == cs),
                            dispatch_entries=frozenset(
                                dip for dcs, dip in dispatch_entries
                                if dcs == cs),
                            resume_calls=bool(boundary_heads or dispatch_entries),
                            min_iterations=max_iterations)
    except EmitUnsupported as exc:
        return "emit-unsupported", str(exc)
    (emit_dir / f"{name}.py").write_text(src, encoding="utf-8")
    return "ok", name


def emit_entry_from_ir(fn_rec: dict, emit_dir: Path, max_iterations,
                       coverage: bool = False, drop_dead_flags: bool = False,
                       boundary_heads: frozenset = frozenset(),
                       dispatch_entries: frozenset = frozenset(),
                       stem: str | None = None, desmc: bool = False):
    """Emit one entry FROM THE RECOVERY IR (docs/recovery_ir.md §3).

    The record is re-elaborated by the shared consumer (``dos_re.lift.ir``):
    the ONE decoder/scanner runs over the IR's pinned bytes — no second
    decode path, and byte-identical output to the scan-path emit when the IR
    captured the same code bytes.

    ``stem`` overrides the default address-derived module/function name (the
    naming-manifest seam, ``dos_re.lift.naming``): the module is written as
    ``{stem}.py`` defining ``def {stem}(...)``, and the caller records the
    entry→stem mapping in the emit dir's ``graph_manifest.json`` so the
    link/install machinery can find it."""
    from dos_re.lift.ir import (apply_desmc, desmc_operand_slots,
                                record_signature, scan_from_ir_record)
    entry = fn_rec["entry"]
    cs, ip = parse_addr(entry)
    name = stem or f"lifted_{cs:04x}_{ip:04x}"
    # De-SMC path (dos_re.lift.smc, shared with cpuless_promote via
    # ir.desmc_operand_slots): a function refused ONLY for runtime code patching,
    # whose every incoming write is a supported operand slot, may be emitted with
    # those operands read from live code memory. The module is banner-marked
    # DESMC; the ordinary differential machinery (liftverify + the end-to-end replay
    # gate) is the promotion decision.  Unlike cpuless_promote, the lifted emitter
    # supports far-target patches too (the JMP_FAR/CALL_FAR path in emit.py).
    slots = desmc_operand_slots(fn_rec)
    if slots is None or (slots and not desmc):
        reasons = sorted({r["reason"] for r in fn_rec.get("refusals", ())})
        return "not-liftable", ",".join(reasons)
    try:
        scan = scan_from_ir_record(fn_rec)
    except ValueError as exc:
        return "emit-unsupported", str(exc)
    apply_desmc(scan, slots)                        # consumed by emit._patched_read
    dead = frozenset()
    if drop_dead_flags:
        from dos_re.lift.analyze import dead_flag_sites
        dead = frozenset(dead_flag_sites(scan))
    try:
        src = emit_function(scan, cs, name, signature=record_signature(fn_rec),
                            coverage=coverage, count_instructions=True,
                            dead_flag_ips=dead,
                            boundary_heads=frozenset(
                                hip for hcs, hip in boundary_heads
                                if hcs == cs),
                            dispatch_entries=frozenset(
                                dip for dcs, dip in dispatch_entries
                                if dcs == cs),
                            resume_calls=bool(boundary_heads or dispatch_entries),
                            min_iterations=max_iterations)
    except EmitUnsupported as exc:
        return "emit-unsupported", str(exc)
    (emit_dir / f"{name}.py").write_text(src, encoding="utf-8")
    return "ok", name


def interpreter_fallback_report(emit_dir: Path):
    """Count ``interp_one`` call sites per generated module.

    Counts real fallback invocations (``interp_one(``) — the import line and
    prose mentions do not match.  Returns {module_name: count} for modules
    with a nonzero count.  Scans every ``.py`` in the emit dir (symbolically
    named modules — the naming manifest — are part of the corpus too)."""
    offenders: dict[str, int] = {}
    for path in sorted(emit_dir.glob("*.py")):
        n = path.read_text(encoding="utf-8").count("interp_one(")
        if n:
            offenders[path.name] = n
    return offenders


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--exe")
    ap.add_argument("--snapshot",
                    help="snapshot whose memory is the code-bytes authority")
    ap.add_argument("--game-root", default=None)
    ap.add_argument("--entries-file",
                    help="census entry list (tools/codemap.py output)")
    ap.add_argument("--from-ir", default=None, metavar="recovery_ir.json",
                    help="emit from the RECOVERY IR instead of exe+snapshot+"
                         "entries (docs/recovery_ir.md): the IR document is "
                         "the single input; byte-identical output when the "
                         "IR captured the same code bytes")
    ap.add_argument("--manifest", default=None, metavar="MAP.json",
                    help="with --from-ir: a {\"CS:IP\": \"module_stem\"} map "
                         "for symbolic module naming (dos_re.lift.naming); "
                         "it is validated, applied per entry, and written to "
                         "the emit dir as graph_manifest.json for the "
                         "link/install machinery")
    ap.add_argument("--emit-dir", default="lifted")
    ap.add_argument("--max-iterations", type=int, default=None, metavar="N",
                    help="runaway-loop guard baked into each module "
                         "(default: emitter default, currently 20000)")
    ap.add_argument("--coverage", action="store_true",
                    help="emit with per-block coverage instrumentation "
                         "(liftverify-identical modules); default OFF for "
                         "uninstrumented generated artifacts")
    ap.add_argument("--boundary-heads", default=None, metavar="@FILE",
                    help="boundary-head addresses (one CS:IP per line): each "
                         "gets an emitted observer event + a RESUME entry, so "
                         "the port's clock parks and resumes in host code "
                         "(host-side stable-boundary instrumentation)")
    ap.add_argument("--dispatch-entries", default=None, metavar="@FILE",
                    help="dynamic dispatch-entry addresses (one CS:IP per "
                         "line): interior addresses reached by indirect "
                         "control flow.  Each is forced as a block leader and "
                         "exported so the backend activator registers a re-entry hook "
                         "into the CONTAINING function -- sharing its "
                         "recovered blocks, not cloning them into a module.")
    ap.add_argument("--desmc", action="store_true",
                    help="emit desmc-candidate functions (the IR's smc "
                         "verdicts, dos_re.lift.smc) with their runtime-"
                         "patched operands read from live code memory "
                         "instead of refusing them; emitted modules are "
                         "CANDIDATES until the differential gate passes")
    ap.add_argument("--drop-dead-flags", action="store_true",
                    help="de-carrier pass 1: elide flag writes proven "
                         "unobservable by analyze.dead_flag_sites "
                         "(seam-conservative liveness). Judged end-to-end "
                         "like every transformation.")
    ap.add_argument("--require-no-interpreter-fallback", action="store_true",
                    help="fail (exit 2) if any emitted module contains an "
                         "interp_one fallback call site")
    args = ap.parse_args(argv)
    if not args.from_ir and not (args.exe and args.snapshot and args.entries_file):
        ap.error("either --from-ir IR.json or --exe + --snapshot + --entries-file")
    if args.manifest and not args.from_ir:
        ap.error("--manifest requires --from-ir")

    emit_dir = Path(args.emit_dir)
    emit_dir.mkdir(parents=True, exist_ok=True)

    naming = None
    if args.manifest:
        import json as _json
        from dos_re.lift.naming import GraphNaming
        naming = GraphNaming(_json.loads(
            Path(args.manifest).read_text(encoding="utf-8")))
        naming.save(emit_dir)

    def _read_pairs(argval):
        if not argval:
            return frozenset()
        out = []
        for line in Path(argval.lstrip("@")).read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                out.append(parse_addr(line))
        return frozenset(out)
    heads = _read_pairs(args.boundary_heads)
    dispatch = _read_pairs(args.dispatch_entries)

    counts = {"ok": 0, "not-liftable": 0, "emit-unsupported": 0}
    skipped: list[tuple[str, str, str]] = []
    if args.from_ir:
        import json
        doc = json.loads(Path(args.from_ir).read_text(encoding="utf-8"))
        recs = doc["functions"]
        n_total = len(recs)
        for entry in sorted(recs):
            status, detail = emit_entry_from_ir(
                recs[entry], emit_dir, args.max_iterations,
                coverage=args.coverage,
                drop_dead_flags=args.drop_dead_flags,
                boundary_heads=heads, dispatch_entries=dispatch,
                stem=naming.stem_of(entry) if naming else None,
                desmc=args.desmc)
            counts[status] += 1
            if status != "ok":
                skipped.append((entry, status, detail))
                print(f"skip     {entry}: {status} ({detail})")
    else:
        entries = []
        for line in Path(args.entries_file).read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                entries.append(parse_addr(line))
        n_total = len(entries)
        rt = load_snapshot(args.exe, args.snapshot, game_root=args.game_root)
        rt.cpu.trace_enabled = False
        mem = rt.cpu.mem
        for cs, ip in entries:
            status, detail = emit_entry(mem, rt, cs, ip, emit_dir,
                                        args.max_iterations,
                                        coverage=args.coverage,
                                        drop_dead_flags=args.drop_dead_flags,
                                        boundary_heads=heads,
                                        dispatch_entries=dispatch)
            counts[status] += 1
            if status != "ok":
                skipped.append((f"{cs:04X}:{ip:04X}", status, detail))
                print(f"skip     {cs:04X}:{ip:04X}: {status} ({detail})")

    print(f"\nemitted {counts['ok']}/{n_total} modules to {emit_dir} "
          f"(not-liftable={counts['not-liftable']}, "
          f"emit-unsupported={counts['emit-unsupported']})")

    offenders = interpreter_fallback_report(emit_dir)
    total_sites = sum(offenders.values())
    if offenders:
        print(f"interpreter fallback audit: {total_sites} interp_one call site(s) "
              f"in {len(offenders)} module(s):")
        for name, n in sorted(offenders.items()):
            print(f"  {name}: {n}")
    else:
        print("interpreter fallback audit: clear (0 interp_one call sites)")
    if args.require_no_interpreter_fallback and offenders:
        print("--require-no-interpreter-fallback: FAIL")
        return 2
    return 0 if not skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
