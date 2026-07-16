#!/usr/bin/env python3
"""liftlink — batch linker: ``emulate_call`` edges become direct lifted-to-lifted calls.

The VM-detachment linking pass of the DOS_RE 2.0 pipeline
(docs/dos_re_2.0.md): after ``liftemit`` produces the VMless lifted corpus,
this tool turns interpreter-mediated near CALLs between lifted functions into
direct Python calls, producing the structurally linked VMless graph.
``emit_function`` already knows how to emit a linked direct call (``link_map``
→ ``call_installed_hook_like_near_call``); this tool computes WHICH edges
qualify and re-emits every caller that gains one.

Edge rule (STRUCTURAL — the 2.0 default):

    caller scan is liftable, the near-CALL target is itself a census entry in
    the SAME segment (near calls guarantee that), and EVERY callee exit is a
    near ``ret`` — tail-exit / retf / iret callees stay ``emulate_call``.

Correctness of the linked graph is judged END-TO-END: full-state oracle
comparison at tick boundaries over the assembled graph, with
``tools/hook_bisect.py`` localizing any divergence to the responsible
function.  Per-function ORACLE_PASSING is NOT a link precondition —
``--proven-edges`` restores that 1.x conservative gate for the hybrid tier or
for debugging, but it must not be the default posture (oracle-guided
convergence, docs/dos_re_2.0.md §2).

Everything is rescanned from the snapshot (``scan_function`` with the
interpreter probe, exactly as liftverify does) — stale census artifacts are
never trusted for a code-identity decision.

Cross-module mechanism (why not plain imports): emitted modules live flat in
one directory and are loaded via ``spec_from_file_location`` (see
``dos_re.lift.install._load_module``), which supports no package-relative
imports.  A linked caller therefore carries a module-level
``LINKS = {"CS:IP": None}`` table and each linked CALL site evaluates
``LINKS["CS:IP"]`` late-bound at call time.  ``resolve_links`` (the
installer's second pass) fills the table with the callees' functions; until
then the module still LOADS standalone, and under the hybrid the installed
hook set takes precedence anyway (``call_installed_hook_like_near_call``
prefers ``cpu.replacement_hooks``).

Callers are re-emitted, so their previous proof no longer covers the new
body: re-verify the linked set with liftverify along a drive afterwards.

Usage:
    python tools/liftlink.py --exe GAME.EXE --snapshot DIR \
        --entries-file entries.txt --board manifest.json [--board ...] \
        [--emit-dir lifted] [--out-dir lifted] [--json report.json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dos_re.lift.cfg import FunctionScan, scan_function  # noqa: E402
from dos_re.lift.emit import EmitUnsupported, emit_function  # noqa: E402
from dos_re.repro_artifacts import clone_runtime_state  # noqa: E402
from dos_re.snapshot import load_snapshot, parse_addr  # noqa: E402

PASSING = "ORACLE_PASSING"


def _probe(rt, cs):
    """Interpreter length-probe over a scratch clone (same as liftverify)."""
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


def load_statuses(board_paths) -> dict[str, str]:
    """Merge proof boards / lift manifests → {"CS:IP": best status}.

    ORACLE_PASSING from ANY pass wins (the same merge rule as
    ``install.passing_entries``: the drive corpora are complementary)."""
    statuses: dict[str, str] = {}
    for path in board_paths:
        data = json.loads(Path(path).read_text())
        items = data.items() if isinstance(data, dict) else ((None, r) for r in data)
        for key, rec in items:
            if not isinstance(rec, dict):
                continue
            entry = rec.get("entry") or key
            status = rec.get("status")
            if not entry or not status:
                continue
            if statuses.get(entry) != PASSING:
                statuses[entry] = status
    return statuses


def all_near_ret_exits(scan: FunctionScan) -> bool:
    """True when every exit is a near ``ret`` — the linkable callee shape.

    ``call_installed_hook_like_near_call`` pushes the return IP and runs the
    callee synchronously; only a callee whose every path pops exactly that
    near return is equivalent to the original CALL.  retf/iret would pop CS
    too; a tail exit hands control to the VM mid-flight."""
    return bool(scan.exits) and all(i.kind == "ret" for i in scan.exits)


def all_far_ret_exits(scan: FunctionScan) -> bool:
    """True when every exit is ``retf`` — the far-linkable callee shape
    (``call_installed_hook_like_far_call`` pushes CS:IP; only retf pops both)."""
    return bool(scan.exits) and all(i.kind == "retf" for i in scan.exits)


def compute_far_link_edges(scans: dict[tuple[int, int], FunctionScan],
                           *, exclude: frozenset[str] = frozenset()):
    """The far-linkable edge set: direct far CALLs between census entries whose
    callee exits are all ``retf``.  Structural rule only (the 2.0 posture);
    same exclusion semantics as near edges."""
    edges: list[tuple[str, str]] = []
    blocked: list[tuple[str, str, str]] = []
    for (cs, ip), scan in sorted(scans.items()):
        if not scan.liftable:
            continue
        caller_key = f"{cs:04X}:{ip:04X}"
        for seg, off in sorted(scan.calls_far):
            callee_key = f"{seg:04X}:{off:04X}"
            callee = scans.get((seg, off))
            if callee_key in exclude:
                blocked.append((caller_key, callee_key, "excluded-callee"))
            elif callee is None:
                blocked.append((caller_key, callee_key, "not-an-entry"))
            elif not callee.liftable:
                blocked.append((caller_key, callee_key, "callee-not-liftable"))
            elif not all_far_ret_exits(callee):
                blocked.append((caller_key, callee_key, "exit-shape"))
            else:
                edges.append((caller_key, callee_key))
    return edges, blocked


def compute_link_edges(scans: dict[tuple[int, int], FunctionScan],
                       statuses: dict[str, str], *, structural: bool = True,
                       exclude: frozenset[str] = frozenset()):
    """The linkable edge set from fresh scans (+ proof statuses if gated).

    Returns ``(edges, blocked)``: ``edges`` is ``[("CS:IP", "CS:IP"), ...]``
    (caller, callee) and ``blocked`` is ``[(caller, callee, reason), ...]``
    for the report — every near-call edge between census entries is accounted
    for, linked or not.

    Two edge rules:

    * ``structural=True`` (DEFAULT — oracle-guided convergence): a callee
      qualifies on the STRUCTURAL criterion alone (liftable census entry,
      all-near-``ret`` exits).  Correctness is guaranteed not per-callee but
      by END-TO-END oracle comparison over the assembled graph, which
      localizes the first bad piece (tools/hook_bisect.py).
    * ``structural=False`` (1.x conservative, ``--proven-edges``) — a callee
      must additionally be ORACLE_PASSING on some board.  Safe standalone (a
      linked call reaches only proven code); useful for the hybrid tier and
      for debugging, but it is not the assembly posture.

    ``exclude`` ("CS:IP" strings) blocks edges INTO entries the port keeps
    interpreted (boundary-shadowing functions, env-wait recovery facts).  This
    matters because a linked call binds the callee's lifted body DIRECTLY
    (``LINKS`` default in ``call_installed_hook_like_near_call``), bypassing
    the installation skip entirely — an excluded callee reached through a
    linked edge would run lifted anyway, skipping its boundary sentinels and
    pacing waits (the Lemmings fade-crawl failure).  Blocking the edge makes
    callers ``emulate_call`` the excluded callee, so the interpreter's
    sentinel hooks, wait pacing, and IRQ machinery all apply."""
    edges: list[tuple[str, str]] = []
    blocked: list[tuple[str, str, str]] = []
    for (cs, ip), scan in sorted(scans.items()):
        if not scan.liftable:
            continue
        caller_key = f"{cs:04X}:{ip:04X}"
        for target in sorted(scan.calls_near):
            callee_key = f"{cs:04X}:{target:04X}"   # near call => same segment
            callee = scans.get((cs, target))
            if callee_key in exclude:
                blocked.append((caller_key, callee_key, "excluded-callee"))
            elif callee is None:
                blocked.append((caller_key, callee_key, "not-an-entry"))
            elif not callee.liftable:
                blocked.append((caller_key, callee_key, "callee-not-liftable"))
            elif not structural and statuses.get(callee_key) != PASSING:
                blocked.append((caller_key, callee_key, "not-passing"))
            elif not all_near_ret_exits(callee):
                blocked.append((caller_key, callee_key, "exit-shape"))
            else:
                edges.append((caller_key, callee_key))
    return edges, blocked


def links_table_line(target_keys) -> str:
    """The module-level late-binding table for a linked caller."""
    body = ", ".join(f'"{k}": None' for k in target_keys)
    return ("LINKS = {%s}  "
            "# linked callees; bound by dos_re.lift.install.resolve_links" % body)


def relink_source(scan: FunctionScan, cs: int, target_ips, *,
                  far_targets=(), signature: bytes,
                  min_iterations: int | None = None,
                  drop_dead_flags: bool = False,
                  boundary_heads: frozenset = frozenset(),
                  dispatch_entries: frozenset = frozenset()) -> str:
    """Re-emit a caller with the given near (and far) call targets linked."""
    name = f"lifted_{cs:04x}_{scan.entry:04x}"
    targets = sorted(set(target_ips))
    fars = sorted(set(far_targets))
    link_map = {t: f'LINKS["{cs:04X}:{t:04X}"]' for t in targets}
    far_link_map = {(fs, fo): f'LINKS["{fs:04X}:{fo:04X}"]' for fs, fo in fars}
    dead = frozenset()
    if drop_dead_flags:
        from dos_re.lift.analyze import dead_flag_sites
        dead = frozenset(dead_flag_sites(scan))
    keys = [f"{cs:04X}:{t:04X}" for t in targets] +            [f"{fs:04X}:{fo:04X}" for fs, fo in fars]
    return emit_function(
        scan, cs, name, signature=signature, coverage=False,
        count_instructions=True, dead_flag_ips=dead,
        boundary_heads=frozenset(hip for hcs, hip in boundary_heads
                                 if hcs == cs),
        dispatch_entries=frozenset(dip for dcs, dip in dispatch_entries
                                   if dcs == cs),
        resume_calls=bool(boundary_heads or dispatch_entries),
        min_iterations=min_iterations, link_map=link_map,
        far_link_map=far_link_map,
        link_imports=(links_table_line(keys),))


def count_emulate_calls(src: str) -> int:
    """emulate_call SITES in an emitted module (``emulate_far_call(`` and the
    import line do not match — call sites always carry the open paren)."""
    return src.count("emulate_call(")


_SIG_RE = re.compile(r"SIGNATURE = bytes\.fromhex\('([0-9a-fA-F]+)'\)")
_ITER_RE = re.compile(r"MAX_ITERATIONS = (\d+)")


def _existing_module_facts(src: str) -> tuple[bytes | None, int | None]:
    """(signature, max_iterations) carried over from a previously emitted
    module, so a relink never weakens the SMC guard or the runaway budget."""
    m = _SIG_RE.search(src)
    sig = bytes.fromhex(m.group(1)) if m else None
    m = _ITER_RE.search(src)
    iters = int(m.group(1)) if m else None
    return sig, iters


def _fresh_signature(mem, cs: int, ip: int, scan: FunctionScan) -> bytes:
    """Entry-byte signature, identical to liftverify's recipe."""
    block_end = min((i.next_ip for i in scan.insts.values()
                     if i.kind != "seq" and i.ip >= ip), default=(ip + 8) & 0xFFFF)
    return bytes(mem.rb(cs, (ip + k) & 0xFFFF)
                 for k in range(max(4, min(16, (block_end - ip) & 0xFFFF))))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--exe")
    ap.add_argument("--snapshot",
                    help="snapshot whose memory is the code-bytes authority")
    ap.add_argument("--game-root", default=None)
    ap.add_argument("--entries-file",
                    help="census entry list (tools/codemap.py output)")
    ap.add_argument("--from-ir", default=None, metavar="recovery_ir.json",
                    help="take code identity from the RECOVERY IR instead of "
                         "exe+snapshot+entries (docs/recovery_ir.md): scans "
                         "are re-elaborated from the IR's pinned bytes by the "
                         "one decoder; no snapshot rescans")
    ap.add_argument("--board", action="append", default=[], metavar="JSON",
                    help="proof board / lift manifest JSON (repeatable). "
                         "Optional under the structural default; required "
                         "with --proven-edges, where ORACLE_PASSING on any "
                         "board qualifies a callee.")
    ap.add_argument("--emit-dir", default="lifted",
                    help="where the current emitted modules live (signatures / "
                         "iteration budgets / before-counts are read from here)")
    ap.add_argument("--out-dir", default=None,
                    help="where linked callers are written (default: --emit-dir, "
                         "overwriting the unlinked callers in place)")
    ap.add_argument("--json", default=None, metavar="REPORT",
                    help="write the machine-readable link report here")
    ap.add_argument("--structural-edges", action="store_true",
                    help="(default since DOS_RE 2.0; flag kept for "
                         "compatibility) link on the structural criterion "
                         "alone: liftable entry + all-near-ret exits.")
    ap.add_argument("--proven-edges", action="store_true",
                    help="1.x conservative gate: additionally require the "
                         "callee to be ORACLE_PASSING on some --board. For "
                         "the hybrid tier / debugging only -- graph assembly "
                         "is judged end-to-end (docs/dos_re_2.0.md section 2).")
    ap.add_argument("--boundary-heads", default=None, metavar="@FILE",
                    help="boundary-head addresses (one CS:IP per line); must "
                         "match the liftemit setting for a consistent corpus")
    ap.add_argument("--dispatch-entries", default=None, metavar="@FILE",
                    help="dynamic dispatch-entry addresses; must match the "
                         "liftemit setting for a consistent corpus")
    ap.add_argument("--drop-dead-flags", action="store_true",
                    help="de-carrier pass 1 in re-emitted callers (must match "
                         "the liftemit setting for a consistent corpus)")
    ap.add_argument("--exclude-callees", action="append", default=[],
                    metavar="CS:IP|@FILE",
                    help="block edges INTO these entries (repeatable; @FILE = "
                         "one CS:IP per line, # comments). Use for entries "
                         "the port keeps interpreted -- boundary-shadowing "
                         "functions, env-wait recovery facts -- because a "
                         "linked edge would bypass the install skip and run "
                         "the excluded lifted body anyway.")
    args = ap.parse_args(argv)
    if args.proven_edges and not args.board:
        ap.error("--proven-edges requires at least one --board")
    if not args.from_ir and not (args.exe and args.snapshot and args.entries_file):
        ap.error("either --from-ir IR.json or --exe + --snapshot + --entries-file")

    emit_dir = Path(args.emit_dir)
    out_dir = Path(args.out_dir) if args.out_dir else emit_dir

    # 1. Code identity: EITHER fresh scans from the snapshot, OR the recovery
    #    IR re-elaborated by the one decoder (never a stale census file).
    scans: dict[tuple[int, int], FunctionScan] = {}
    ir_sigs: dict[tuple[int, int], bytes] = {}
    if args.from_ir:
        from dos_re.lift.ir import (load_recovery_ir, record_signature,
                                    scan_from_ir_record)
        doc = load_recovery_ir(args.from_ir)
        for entry, rec in sorted(doc["functions"].items()):
            cs, ip = parse_addr(entry)
            if rec.get("liftable"):
                scans[(cs, ip)] = scan_from_ir_record(rec)
                ir_sigs[(cs, ip)] = record_signature(rec)
    else:
        entries = []
        for line in Path(args.entries_file).read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                entries.append(parse_addr(line))
        rt = load_snapshot(args.exe, args.snapshot, game_root=args.game_root)
        rt.cpu.trace_enabled = False
        mem = rt.cpu.mem
        for cs, ip in entries:
            scans[(cs, ip)] = scan_function(
                lambda off, cs=cs: mem.rb(cs, off & 0xFFFF), ip,
                probe=_probe(rt, cs))

    # 2. The linkable edge set.
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

    exclude: set[str] = set()
    for item in args.exclude_callees:
        if item.startswith("@"):
            for line in Path(item[1:]).read_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if line:
                    exclude.add(line.upper())
        else:
            exclude.add(item.strip().upper())

    statuses = load_statuses(args.board)
    edges, blocked = compute_link_edges(scans, statuses,
                                        structural=not args.proven_edges,
                                        exclude=frozenset(exclude))

    # A linked call needs its callee module ON DISK for the installer's
    # resolution pass — a proven callee that was never emitted cannot be
    # linked (it can still be emulate_call'd, so just drop the edge, loudly).
    kept: list[tuple[str, str]] = []
    for caller_key, callee_key in edges:
        e_cs, e_ip = parse_addr(callee_key)
        if (emit_dir / f"lifted_{e_cs:04x}_{e_ip:04x}.py").is_file():
            kept.append((caller_key, callee_key))
        else:
            blocked.append((caller_key, callee_key, "callee-module-missing"))
            print(f"drop     {caller_key} -> {callee_key}: callee module not in {emit_dir}")
    edges = kept

    # 2b. FAR edges (structural rule; same on-disk requirement).
    far_edges, far_blocked = compute_far_link_edges(scans,
                                                    exclude=frozenset(exclude))
    blocked.extend(far_blocked)
    far_kept: list[tuple[str, str]] = []
    for caller_key, callee_key in far_edges:
        e_cs, e_ip = parse_addr(callee_key)
        if (emit_dir / f"lifted_{e_cs:04x}_{e_ip:04x}.py").is_file():
            far_kept.append((caller_key, callee_key))
        else:
            blocked.append((caller_key, callee_key, "callee-module-missing"))
            print(f"drop far {caller_key} -> {callee_key}: callee module not in {emit_dir}")
    far_edges = far_kept

    # 3. Re-emit every caller that gains at least one linked edge.
    by_caller: dict[str, list[str]] = {}
    for caller_key, callee_key in edges:
        by_caller.setdefault(caller_key, []).append(callee_key)
    far_by_caller: dict[str, list[tuple[int, int]]] = {}
    for caller_key, callee_key in far_edges:
        far_by_caller.setdefault(caller_key, []).append(parse_addr(callee_key))
    all_callers = sorted(set(by_caller) | set(far_by_caller))

    out_dir.mkdir(parents=True, exist_ok=True)
    callers_report: dict[str, dict] = {}
    total_before = total_after = 0
    for caller_key in all_callers:
        cs, ip = parse_addr(caller_key)
        scan = scans[(cs, ip)]
        name = f"lifted_{cs:04x}_{ip:04x}"
        old_path = emit_dir / f"{name}.py"
        old_src = old_path.read_text(encoding="utf-8") if old_path.is_file() else None
        if old_src is not None:
            sig, min_iters = _existing_module_facts(old_src)
        else:
            sig, min_iters = None, None
        if sig is None:
            sig = ir_sigs.get((cs, ip))
        if sig is None:
            sig = _fresh_signature(mem, cs, ip, scan)
        target_ips = [parse_addr(k)[1] for k in by_caller.get(caller_key, ())]
        far_targets = far_by_caller.get(caller_key, ())
        try:
            src = relink_source(scan, cs, target_ips, far_targets=far_targets,
                                signature=sig, min_iterations=min_iters,
                                drop_dead_flags=args.drop_dead_flags,
                                boundary_heads=heads, dispatch_entries=dispatch)
        except EmitUnsupported as exc:
            print(f"skip     {caller_key}: emit-unsupported ({exc})")
            for callee_key in by_caller.get(caller_key, ()):
                blocked.append((caller_key, callee_key, "emit-unsupported"))
                edges.remove((caller_key, callee_key))
            for fs, fo in far_by_caller.get(caller_key, ()):
                ck = f"{fs:04X}:{fo:04X}"
                blocked.append((caller_key, ck, "emit-unsupported"))
                far_edges.remove((caller_key, ck))
            continue
        before = count_emulate_calls(old_src) if old_src is not None \
            else count_emulate_calls(emit_function(scan, cs, name,
                                                   signature=sig, coverage=False,
                                                   count_instructions=True,
                                                   min_iterations=min_iters))
        after = count_emulate_calls(src)
        (out_dir / f"{name}.py").write_text(src, encoding="utf-8")
        total_before += before
        total_after += after
        n_near = len(by_caller.get(caller_key, ()))
        n_far = len(far_by_caller.get(caller_key, ()))
        callers_report[caller_key] = {
            "module": f"{name}.py",
            "linked": sorted(by_caller.get(caller_key, ())),
            "linked_far": sorted(f"{fs:04X}:{fo:04X}"
                                 for fs, fo in far_by_caller.get(caller_key, ())),
            "emulate_call_before": before,
            "emulate_call_after": after,
        }
        print(f"linked   {caller_key}: {n_near} near + {n_far} far edge(s), "
              f"emulate_call {before} -> {after}")

    # 4. Report.
    reasons: dict[str, int] = {}
    for _, _, reason in blocked:
        reasons[reason] = reasons.get(reason, 0) + 1
    print(f"\n{len(edges)} near + {len(far_edges)} far edges linked into "
          f"{len(callers_report)} callers; "
          f"emulate_call sites in re-emitted callers: {total_before} -> {total_after}")
    if reasons:
        print("blocked edges: " + ", ".join(f"{r}={n}" for r, n in sorted(reasons.items())))
    print("NOTE: re-emitted callers carry NEW bodies -- the linked graph is "
          "judged END-TO-END (tick-boundary oracle comparison over the "
          "assembled VMless graph; hook_bisect localizes any divergence). "
          "liftverify along a drive remains available for per-function "
          "diagnostics and the hybrid tier.")

    if args.json:
        report = {
            "snapshot": str(args.snapshot),
            "boards": list(args.board),
            "entries": len(scans),
            "edges": [list(e) for e in edges],
            "far_edges": [list(e) for e in far_edges],
            "blocked": [list(b) for b in blocked],
            "callers": callers_report,
            "totals": {
                "edges_linked": len(edges),
                "callers_reemitted": len(callers_report),
                "emulate_call_before": total_before,
                "emulate_call_after": total_after,
                "blocked_by_reason": reasons,
            },
        }
        Path(args.json).write_text(json.dumps(report, indent=1, sort_keys=True) + "\n",
                                   encoding="utf-8")
        print(f"report: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
