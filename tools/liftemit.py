#!/usr/bin/env python3
"""liftemit — batch-emit every census entry to lifted Python, in one pass.

The bulk-emission step of the DOS_RE 2.0 pipeline (docs/dos_re_2.0.md): given
the census entry list and a snapshot whose memory is the code-bytes authority,
emit a VMless lifted module per entry into ``--emit-dir``.  No verification
and no driving — that is ``liftverify``'s job (per-slice diagnostics / hybrid
tier) and, for the assembled graph, the END-TO-END oracle's.  Under
oracle-guided convergence the whole census is emitted optimistically here,
linked by ``liftlink`` (structural edges), then validated as a graph;
``liftemit`` is the deterministic "labor" that produces the candidate modules.

Emission is byte-identical to ``liftverify``'s emit path (same signature
recipe, ``coverage=True``, same ``--max-iterations`` default) so a module
emitted here and one emitted during verification are the same file.

Usage:
    python tools/liftemit.py --exe GAME.EXE --snapshot DIR \
        --entries-file entries.txt [--emit-dir lifted] [--max-iterations N]
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
    from dos_re.repro_artifacts import clone_runtime_state
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


def emit_entry(mem, rt, cs: int, ip: int, emit_dir: Path, max_iterations):
    """Emit one entry.  Returns ("ok"|"not-liftable"|"emit-unsupported", detail)."""
    name = f"lifted_{cs:04x}_{ip:04x}"
    scan = scan_function(lambda off: mem.rb(cs, off & 0xFFFF), ip, probe=_probe(rt, cs))
    if not scan.liftable:
        return "not-liftable", ",".join(sorted({r.reason for r in scan.refusals}))
    block_end = min((i.next_ip for i in scan.insts.values()
                     if i.kind != "seq" and i.ip >= ip), default=(ip + 8) & 0xFFFF)
    sig = bytes(mem.rb(cs, (ip + k) & 0xFFFF)
                for k in range(max(4, min(16, (block_end - ip) & 0xFFFF))))
    try:
        src = emit_function(scan, cs, name, signature=sig, coverage=True,
                            min_iterations=max_iterations)
    except EmitUnsupported as exc:
        return "emit-unsupported", str(exc)
    (emit_dir / f"{name}.py").write_text(src, encoding="utf-8")
    return "ok", name


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--exe", required=True)
    ap.add_argument("--snapshot", required=True,
                    help="snapshot whose memory is the code-bytes authority")
    ap.add_argument("--game-root", default=None)
    ap.add_argument("--entries-file", required=True,
                    help="census entry list (tools/codemap.py output)")
    ap.add_argument("--emit-dir", default="lifted")
    ap.add_argument("--max-iterations", type=int, default=None, metavar="N",
                    help="runaway-loop guard baked into each module "
                         "(default: emitter default, currently 20000)")
    args = ap.parse_args(argv)

    entries = []
    for line in Path(args.entries_file).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            entries.append(parse_addr(line))

    emit_dir = Path(args.emit_dir)
    emit_dir.mkdir(parents=True, exist_ok=True)
    rt = load_snapshot(args.exe, args.snapshot, game_root=args.game_root)
    rt.cpu.trace_enabled = False
    mem = rt.cpu.mem

    counts = {"ok": 0, "not-liftable": 0, "emit-unsupported": 0}
    skipped: list[tuple[str, str, str]] = []
    for cs, ip in entries:
        status, detail = emit_entry(mem, rt, cs, ip, emit_dir, args.max_iterations)
        counts[status] += 1
        if status != "ok":
            skipped.append((f"{cs:04X}:{ip:04X}", status, detail))
            print(f"skip     {cs:04X}:{ip:04X}: {status} ({detail})")

    print(f"\nemitted {counts['ok']}/{len(entries)} modules to {emit_dir} "
          f"(not-liftable={counts['not-liftable']}, "
          f"emit-unsupported={counts['emit-unsupported']})")
    return 0 if not skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
