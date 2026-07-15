#!/usr/bin/env python3
"""liftlink — batch linker: proven ``emulate_call`` edges become direct native calls.

The de-VM pass of the automatic recovery pipeline (M-C).  ``emit_function``
already knows how to emit a linked direct call (``link_map`` →
``call_installed_hook_like_near_call``); this tool computes WHICH edges are
safe and re-emits every caller that gains one.

Edge rule (the link precondition — enforced here, not by the emitter):

    caller scan is liftable, the near-CALL target is itself a census entry in
    the SAME segment (near calls guarantee that), the callee's proof status is
    ORACLE_PASSING on at least one board, and EVERY callee exit is a near
    ``ret`` — tail-exit / retf / iret callees stay ``emulate_call``.

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


def compute_link_edges(scans: dict[tuple[int, int], FunctionScan],
                       statuses: dict[str, str]):
    """The linkable edge set from fresh scans + proof statuses.

    Returns ``(edges, blocked)``: ``edges`` is ``[("CS:IP", "CS:IP"), ...]``
    (caller, callee) and ``blocked`` is ``[(caller, callee, reason), ...]``
    for the report — every near-call edge between census entries is accounted
    for, linked or not."""
    edges: list[tuple[str, str]] = []
    blocked: list[tuple[str, str, str]] = []
    for (cs, ip), scan in sorted(scans.items()):
        if not scan.liftable:
            continue
        caller_key = f"{cs:04X}:{ip:04X}"
        for target in sorted(scan.calls_near):
            callee_key = f"{cs:04X}:{target:04X}"   # near call ⇒ same segment
            callee = scans.get((cs, target))
            if callee is None:
                blocked.append((caller_key, callee_key, "not-an-entry"))
            elif not callee.liftable:
                blocked.append((caller_key, callee_key, "callee-not-liftable"))
            elif statuses.get(callee_key) != PASSING:
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
                  signature: bytes, min_iterations: int | None = None) -> str:
    """Re-emit a caller with the given near-call targets linked."""
    name = f"lifted_{cs:04x}_{scan.entry:04x}"
    targets = sorted(set(target_ips))
    link_map = {t: f'LINKS["{cs:04X}:{t:04X}"]' for t in targets}
    return emit_function(
        scan, cs, name, signature=signature, coverage=True,
        min_iterations=min_iterations, link_map=link_map,
        link_imports=(links_table_line(f"{cs:04X}:{t:04X}" for t in targets),))


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
    ap.add_argument("--exe", required=True)
    ap.add_argument("--snapshot", required=True,
                    help="snapshot whose memory is the code-bytes authority")
    ap.add_argument("--game-root", default=None)
    ap.add_argument("--entries-file", required=True,
                    help="census entry list (tools/codemap.py output)")
    ap.add_argument("--board", action="append", required=True, metavar="JSON",
                    help="proof board / lift manifest JSON (repeatable; "
                         "ORACLE_PASSING on any board qualifies a callee)")
    ap.add_argument("--emit-dir", default="lifted",
                    help="where the current emitted modules live (signatures / "
                         "iteration budgets / before-counts are read from here)")
    ap.add_argument("--out-dir", default=None,
                    help="where linked callers are written (default: --emit-dir, "
                         "overwriting the unlinked callers in place)")
    ap.add_argument("--json", default=None, metavar="REPORT",
                    help="write the machine-readable link report here")
    args = ap.parse_args(argv)

    entries = []
    for line in Path(args.entries_file).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            entries.append(parse_addr(line))
    emit_dir = Path(args.emit_dir)
    out_dir = Path(args.out_dir) if args.out_dir else emit_dir

    rt = load_snapshot(args.exe, args.snapshot, game_root=args.game_root)
    rt.cpu.trace_enabled = False
    mem = rt.cpu.mem

    # 1. Fresh scans from the snapshot — code identity is never taken from a
    #    stale census file.
    scans: dict[tuple[int, int], FunctionScan] = {}
    for cs, ip in entries:
        scans[(cs, ip)] = scan_function(
            lambda off, cs=cs: mem.rb(cs, off & 0xFFFF), ip, probe=_probe(rt, cs))

    # 2. The linkable edge set.
    statuses = load_statuses(args.board)
    edges, blocked = compute_link_edges(scans, statuses)

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

    # 3. Re-emit every caller that gains at least one linked edge.
    by_caller: dict[str, list[str]] = {}
    for caller_key, callee_key in edges:
        by_caller.setdefault(caller_key, []).append(callee_key)

    out_dir.mkdir(parents=True, exist_ok=True)
    callers_report: dict[str, dict] = {}
    total_before = total_after = 0
    for caller_key in sorted(by_caller):
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
            sig = _fresh_signature(mem, cs, ip, scan)
        target_ips = [parse_addr(k)[1] for k in by_caller[caller_key]]
        try:
            src = relink_source(scan, cs, target_ips,
                                signature=sig, min_iterations=min_iters)
        except EmitUnsupported as exc:
            print(f"skip     {caller_key}: emit-unsupported ({exc})")
            for callee_key in by_caller[caller_key]:
                blocked.append((caller_key, callee_key, "emit-unsupported"))
                edges.remove((caller_key, callee_key))
            continue
        before = count_emulate_calls(old_src) if old_src is not None \
            else count_emulate_calls(emit_function(scan, cs, name,
                                                   signature=sig, coverage=True,
                                                   min_iterations=min_iters))
        after = count_emulate_calls(src)
        (out_dir / f"{name}.py").write_text(src, encoding="utf-8")
        total_before += before
        total_after += after
        callers_report[caller_key] = {
            "module": f"{name}.py",
            "linked": sorted(by_caller[caller_key]),
            "emulate_call_before": before,
            "emulate_call_after": after,
        }
        print(f"linked   {caller_key}: {len(by_caller[caller_key])} edge(s), "
              f"emulate_call {before} -> {after}")

    # 4. Report.
    reasons: dict[str, int] = {}
    for _, _, reason in blocked:
        reasons[reason] = reasons.get(reason, 0) + 1
    print(f"\n{len(edges)} edges linked into {len(callers_report)} callers; "
          f"emulate_call sites in re-emitted callers: {total_before} -> {total_after}")
    if reasons:
        print("blocked edges: " + ", ".join(f"{r}={n}" for r, n in sorted(reasons.items())))
    print("NOTE: re-emitted callers carry NEW bodies — re-verify them with "
          "liftverify along a drive before trusting the linked set.")

    if args.json:
        report = {
            "snapshot": str(args.snapshot),
            "boards": list(args.board),
            "entries": len(entries),
            "edges": [list(e) for e in edges],
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
