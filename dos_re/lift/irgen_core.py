"""irgen_core — the platform-agnostic core of recovery-IR generation.

The generic half of ``tools/irgen.py`` (docs/recovery_ir.md): per-entry
CFG scan → function-record serialization → fail-loud unsupported ledger →
deterministic JSON document.  Platform knowledge enters ONLY through a
pluggable triple supplied by a front-end:

    fetch_for(cs)   -> fetch(off) -> int      code-byte authority for segment cs
    probe_for(cs)   -> probe(ip)  -> int|None interpreter step-length probe
                                              (or None for a static-only scan);
                                              called PER ENTRY, so a front-end
                                              can hand out a fresh scratch
                                              interpreter each time
    effect_tagger(inst) -> str|None           platform-effect tag (§4 of
                                              docs/recovery_ir.md)

``dos_effect_tag`` is the DOS platform's tagger (INT vectors + I/O ports) —
the default used by the DOS front-end and reusable by other front-ends for
raw-INT C-runtime paths.  Nothing in this module may know any OTHER platform
(no Win16/NE concepts): a front-end (e.g. a win16 layer) owns those and
passes them through the triple.

Consumers import from here (``dos_re.lift.irgen_core``), never from
``tools/irgen.py`` — the tool is the DOS CLI front-end over this core.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .cfg import FunctionScan, scan_function

IR_VERSION = 0

#: static imm8 port → effect tag (dx-dynamic ports classify at runtime/v1)
_PORT_TAGS = {**{p: "port_pit" for p in range(0x40, 0x44)},
              **{p: "port_kbd" for p in (0x60, 0x61, 0x64)},
              **{p: "port_pic" for p in (0x20, 0x21, 0xA0, 0xA1)}}
_INT_TAGS = {0x21: "int21_dos", 0x10: "int10_video", 0x33: "int33_mouse",
             0x16: "int16_kbd", 0x1A: "int1a_time",
             0x60: "int60_61_driver", 0x61: "int60_61_driver"}


def dos_effect_tag(inst) -> str | None:
    """The DOS platform-effect recognizer: INT vector classes + static ports."""
    if inst.kind == "int" and inst.int_no is not None:
        return _INT_TAGS.get(inst.int_no, f"int{inst.int_no:02x}")
    if inst.op in (0xE4, 0xE5, 0xE6, 0xE7) and inst.imm is not None:
        return _PORT_TAGS.get(inst.imm, f"port_{inst.imm:02x}")
    if inst.op in (0xEC, 0xED, 0xEE, 0xEF):
        return "port_dx_dynamic"
    return None


def signature_for(fetch: Callable[[int], int], ip: int,
                  scan: FunctionScan) -> bytes:
    """Entry-byte SMC-guard signature — the exact liftverify/liftemit recipe."""
    block_end = min((i.next_ip for i in scan.insts.values()
                     if i.kind != "seq" and i.ip >= ip), default=(ip + 8) & 0xFFFF)
    return bytes(fetch((ip + k) & 0xFFFF)
                 for k in range(max(4, min(16, (block_end - ip) & 0xFFFF))))


def function_record(scan: FunctionScan, cs: int, ip: int,
                    fetch: Callable[[int], int],
                    keep_interpreted: set[str],
                    boundary_heads: frozenset = frozenset(),
                    dispatch_entries: frozenset = frozenset(),
                    effect_tagger: Callable = dos_effect_tag) -> dict:
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
            tag = effect_tagger(inst)
            if tag:
                rec["platform_effect"] = tag
            if (cs, inst.ip) in boundary_heads:
                # BoundaryEffect (docs/recovery_ir.md): the emitter generates
                # a host-side boundary event here and a ResumePoint at the
                # successor; the runtime parks/resumes in lifted host code.
                rec["boundary_effect"] = True
            if (cs, inst.ip) in dispatch_entries:
                # DynamicDispatchEntry: an interior address reached by indirect
                # control flow — a re-entry point into THIS function's block
                # dispatcher (shares its recovered blocks, not a new function).
                rec["dispatch_entry"] = True
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
        rec["signature"] = signature_for(fetch, ip, scan).hex()
    if entry_key in keep_interpreted:
        rec["platform_effect"] = "env_wait"
    return rec


def build_document(entries, *,
                   fetch_for: Callable[[int], Callable[[int], int]],
                   probe_for: Callable[[int], Callable] | None = None,
                   effect_tagger: Callable = dos_effect_tag,
                   provenance: dict,
                   notice: str,
                   keep_interpreted: set[str] = frozenset(),
                   boundary_heads: frozenset = frozenset(),
                   dispatch_entries: frozenset = frozenset(),
                   identity_for: Callable[[int, int], dict] | None = None
                   ) -> dict:
    """Scan every ``(cs, ip)`` entry and assemble the recovery-IR document.

    ``identity_for(cs, ip)`` (optional) is the pluggable identity provider: it
    returns front-end metadata fields (e.g. ``symbol``/``module`` from a
    shipped symbol table, a loader-level segment alias) embedded faithfully in
    the entry's function record AND in its unsupported-ledger entries, so
    refusals name the symbol, not just the address.  Addresses remain the
    canonical keys — identity is naming evidence, never proof of behavior —
    and this core stays symbol-source-agnostic (a program without symbols
    simply passes nothing).  It may not override core fields.  Refusals of
    unliftable entries land in the fail-loud ``unsupported`` ledger, never
    silently dropped.
    """
    keep = set(keep_interpreted)
    functions: dict[str, dict] = {}
    unsupported: list[dict] = []

    # Phase 1: scan everything first, so runtime CODE PATCHING can be
    # adjudicated with the whole census in view.  A CS-override direct store
    # (cfg.cs_store_target) landing inside a DIFFERENT censused function means
    # that function's bytes are retuned at runtime -- lifting the snapshot's
    # copy would silently freeze one moment's operands (see cfg.py's
    # self-modifying refusal for the single-function case and the observed
    # SkyRoads LZS-decoder instance).  The PATCHED function is refused, loud,
    # naming its patcher.
    scans: list[tuple[int, int, "object"]] = []
    for cs, ip in entries:
        fetch = fetch_for(cs)
        probe = probe_for(cs) if probe_for is not None else None
        scans.append((cs, ip, scan_function(fetch, ip, probe=probe)))

    from .cfg import Refusal, inst_byte_offsets
    byte_sets = {(cs, ip): inst_byte_offsets(scan) for cs, ip, scan in scans}
    for w_cs, w_ip, w_scan in scans:
        for site, target in w_scan.cs_store_targets:
            for (v_cs, v_ip), owned in byte_sets.items():
                if v_cs == w_cs and target in owned and (v_cs, v_ip) != (w_cs, w_ip):
                    v_scan = next(s for c, i, s in scans if (c, i) == (v_cs, v_ip))
                    if not any(r.reason == "code-patched-at-runtime"
                               for r in v_scan.refusals):
                        v_scan.refusals.append(Refusal(
                            v_ip, "code-patched-at-runtime",
                            f"cs:[{target:04X}] is written by {w_cs:04X}:{site:04X}"))

    # Phase 2: build the records from the (possibly refusal-augmented) scans.
    for cs, ip, scan in scans:
        fetch = fetch_for(cs)
        rec = function_record(scan, cs, ip, fetch, keep,
                              boundary_heads=boundary_heads,
                              dispatch_entries=dispatch_entries,
                              effect_tagger=effect_tagger)
        identity = identity_for(cs, ip) if identity_for is not None else {}
        for key in identity:
            if key in rec:
                raise ValueError(f"identity_for may not override {key!r}")
        rec.update(identity)
        functions[rec["entry"]] = rec
        if not scan.liftable:
            for r in scan.refusals:
                urec = {"entry": rec["entry"], "reason": r.reason,
                        "detail": r.detail}
                urec.update({k: v for k, v in identity.items()
                             if k not in urec})
                unsupported.append(urec)

    return {
        "_notice": notice,
        "ir_version": IR_VERSION,
        "provenance": provenance,
        "functions": functions,
        "facts_applied": {
            "keep_interpreted": sorted(keep),
            "boundary_heads": sorted("%04X:%04X" % k for k in boundary_heads),
            "dispatch_entries": sorted("%04X:%04X" % k
                                       for k in dispatch_entries),
        },
        "unsupported": unsupported,
    }


def dump_document(doc: dict) -> str:
    """The deterministic serialization: sorted keys, stable indent, one
    trailing newline — byte-identical across regenerations."""
    return json.dumps(doc, indent=1, sort_keys=True) + "\n"


def write_document(doc: dict, out: str | Path) -> Path:
    out = Path(out)
    out.write_text(dump_document(doc), encoding="utf-8")
    return out
