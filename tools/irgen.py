#!/usr/bin/env python3
"""irgen — generate the recovery IR (docs/recovery_ir.md) from canonical inputs.

The serialization step of the DOS_RE 2.0 pipeline: original binary + code-byte
snapshot + entries + explicit recovery facts → one deterministic
``recovery_ir.json``.  Internally reuses the decoder and CFG scanner — the IR
is their serialization plus effect tags and provenance; nothing here invents
a second decoder.  Downstream, every emitter consumes the IR (the VMless
emitter via ``liftemit --from-ir``), analyses annotate it, and the whole
document is disposable/regeneratable.

Usage:
    python tools/irgen.py --exe GAME.EXE --snapshot DIR \
        --entries-file entries.txt [--keep-interpreted @FILE] \
        [--out recovery_ir.json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dos_re.lift import scan_function  # noqa: E402
from dos_re.lift.cfg import FunctionScan  # noqa: E402
from dos_re.snapshot import load_snapshot, parse_addr  # noqa: E402

IR_VERSION = 0

#: static imm8 port → effect tag (dx-dynamic ports classify at runtime/v1)
_PORT_TAGS = {**{p: "port_pit" for p in range(0x40, 0x44)},
              **{p: "port_kbd" for p in (0x60, 0x61, 0x64)},
              **{p: "port_pic" for p in (0x20, 0x21, 0xA0, 0xA1)}}
_INT_TAGS = {0x21: "int21_dos", 0x10: "int10_video", 0x33: "int33_mouse",
             0x16: "int16_kbd", 0x1A: "int1a_time",
             0x60: "int60_61_driver", 0x61: "int60_61_driver"}


def _probe(rt, cs):
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


def _signature(mem, cs: int, ip: int, scan: FunctionScan) -> bytes:
    """Entry-byte SMC-guard signature — the exact liftverify/liftemit recipe."""
    block_end = min((i.next_ip for i in scan.insts.values()
                     if i.kind != "seq" and i.ip >= ip), default=(ip + 8) & 0xFFFF)
    return bytes(mem.rb(cs, (ip + k) & 0xFFFF)
                 for k in range(max(4, min(16, (block_end - ip) & 0xFFFF))))


def _effect_tag(inst) -> str | None:
    if inst.kind == "int" and inst.int_no is not None:
        return _INT_TAGS.get(inst.int_no, f"int{inst.int_no:02x}")
    if inst.op in (0xE4, 0xE5, 0xE6, 0xE7) and inst.imm is not None:
        return _PORT_TAGS.get(inst.imm, f"port_{inst.imm:02x}")
    if inst.op in (0xEC, 0xED, 0xEE, 0xEF):
        return "port_dx_dynamic"
    return None


def _function_record(scan: FunctionScan, cs: int, ip: int, mem,
                     keep_interpreted: set[str],
                     boundary_heads: frozenset = frozenset()) -> dict:
    entry_key = f"{cs:04X}:{ip:04X}"
    blocks = []
    leaders = scan.block_leaders() if scan.liftable else []
    leader_set = set(leaders)
    for leader in leaders:
        insts = []
        p = leader
        while True:
            inst = scan.insts[p]
            rec = {"ip": f"{inst.ip:04X}", "bytes": inst.raw.hex(),
                   "kind": inst.kind, "mnemonic": inst.mnemonic}
            if inst.target is not None:
                rec["target"] = f"{inst.target:04X}"
            if inst.far_target is not None:
                rec["far_target"] = [f"{inst.far_target[0]:04X}",
                                     f"{inst.far_target[1]:04X}"]
            if inst.int_no is not None:
                rec["int_no"] = f"{inst.int_no:02X}"
            if inst.modrm is not None and inst.mod != 3:
                rec["mem_operand"] = True
            tag = _effect_tag(inst)
            if tag:
                rec["platform_effect"] = tag
            if (cs, inst.ip) in boundary_heads:
                # BoundaryEffect (docs/recovery_ir.md): the emitter generates
                # a host-side boundary event here and a ResumePoint at the
                # successor; the runtime parks/resumes in lifted host code.
                rec["boundary_effect"] = True
            insts.append(rec)
            if inst.kind != "seq" and inst.kind not in ("call", "call_far",
                                                        "call_ind", "int"):
                break
            nxt = inst.next_ip
            if nxt in leader_set or nxt not in scan.insts:
                break
            p = nxt
        blocks.append({"leader": f"{leader:04X}", "instructions": insts})

    rec = {
        "entry": entry_key,
        "liftable": scan.liftable,
        "refusals": [{"ip": f"{r.ip:04X}", "reason": r.reason,
                      "detail": r.detail} for r in scan.refusals],
        "exits": sorted({i.kind for i in scan.exits}),
        "blocks": blocks,
        "calls_near": sorted(f"{t:04X}" for t in scan.calls_near),
        "calls_far": sorted([f"{s:04X}", f"{o:04X}"]
                            for s, o in scan.calls_far),
        "ints": sorted({f"{i.int_no:02X}" for i in scan.insts.values()
                        if i.kind == "int" and i.int_no is not None}),
    }
    if scan.liftable:
        rec["signature"] = _signature(mem, cs, ip, scan).hex()
    if entry_key in keep_interpreted:
        rec["platform_effect"] = "env_wait"
    return rec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--exe", required=True)
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--game-root", default=None)
    ap.add_argument("--entries-file", required=True)
    ap.add_argument("--keep-interpreted", default=None, metavar="@FILE",
                    help="the port's keep-interpreted list (one CS:IP per "
                         "line) — tagged env_wait in the IR")
    ap.add_argument("--boundary-heads", default=None, metavar="@FILE",
                    help="boundary/wait head addresses (one CS:IP per line) "
                         "— marked as BoundaryEffect sites in the IR; the "
                         "emitter generates host-side events + ResumePoints "
                         "from them")
    ap.add_argument("--out", default="recovery_ir.json")
    args = ap.parse_args(argv)

    entries = []
    for line in Path(args.entries_file).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            entries.append(parse_addr(line))

    keep: set[str] = set()
    if args.keep_interpreted:
        path = args.keep_interpreted.lstrip("@")
        for line in Path(path).read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                keep.add(line.upper())

    heads: frozenset = frozenset()
    if args.boundary_heads:
        hpath = Path(args.boundary_heads.lstrip("@"))
        pairs = []
        for line in hpath.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                pairs.append(parse_addr(line))
        heads = frozenset(pairs)

    rt = load_snapshot(args.exe, args.snapshot, game_root=args.game_root)
    rt.cpu.trace_enabled = False
    mem = rt.cpu.mem

    functions: dict[str, dict] = {}
    unsupported: list[dict] = []
    for cs, ip in entries:
        scan = scan_function(lambda off, cs=cs: mem.rb(cs, off & 0xFFFF), ip,
                             probe=_probe(rt, cs))
        rec = _function_record(scan, cs, ip, mem, keep, boundary_heads=heads)
        functions[rec["entry"]] = rec
        if not scan.liftable:
            for r in scan.refusals:
                unsupported.append({"entry": rec["entry"], "reason": r.reason,
                                    "detail": r.detail})

    try:
        toolchain = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True).stdout.strip()
    except Exception:  # noqa: BLE001
        toolchain = "unknown"

    doc = {
        "_notice": "AUTOGENERATED by dos_re tools/irgen.py — DO NOT HAND EDIT. "
                   "Regenerate from the original binary, snapshot, entries, "
                   "recovery facts, and the current DOS_RE toolchain "
                   "(docs/recovery_ir.md).",
        "ir_version": IR_VERSION,
        "provenance": {
            "exe": f"{Path(args.exe).name} sha1="
                   f"{hashlib.sha1(Path(args.exe).read_bytes()).hexdigest()}",
            "snapshot": str(args.snapshot),
            "toolchain": toolchain,
            "entries": len(entries),
        },
        "functions": functions,
        "facts_applied": {
            "keep_interpreted": sorted(keep),
            "boundary_heads": sorted("%04X:%04X" % k for k in heads),
        },
        "unsupported": unsupported,
    }
    out = Path(args.out)
    out.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n",
                   encoding="utf-8")
    n_liftable = sum(1 for f in functions.values() if f["liftable"])
    print(f"recovery IR v{IR_VERSION}: {len(functions)} functions "
          f"({n_liftable} liftable, {len(unsupported)} unsupported records) "
          f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
