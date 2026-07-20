"""Recovery IR v0 round-trip: scan-path emission == IR-path emission.

The IR equivalence gate (docs/recovery_ir.md §3.3) in miniature, synthetic
(game-free tests rule): serialize a scanned function into an IR record the
way irgen does, re-elaborate it through liftemit's emit_entry_from_ir path,
and require the emitted module to be BYTE-IDENTICAL to the scan-path module.

Plus the generic-core seam (dos_re.lift.irgen_core): tools/irgen.py is a DOS
front-end over a platform-parameterized core — the document built through the
core with the DOS effect tagger is pinned here, and a CUSTOM effect tagger's
tags must land in the document (the contract a non-DOS front-end, e.g. a
Win16 layer, builds on).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from dos_re.lift import irgen_core
from dos_re.lift.cfg import scan_function
from dos_re.lift.emit import emit_function

_TOOLS = Path(__file__).resolve().parents[1] / "tools"


def _load_tool(name):
    spec = importlib.util.spec_from_file_location(name, _TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


CS = 0x1000
ENTRY = 0x0100


def test_ir_record_reemits_byte_identical():
    liftemit = _load_tool("liftemit")

    # A function with a call, an int (effect tag), memory ops and a shared
    # ret — enough shape to exercise blocks/targets/effects in the record.
    code = bytes.fromhex(
        "B80300"          # 0100 mov ax,3
        "01C8"            # 0103 add ax,cx
        "E80500"          # 0105 call 010D   (the shared ret — trivial callee)
        "CD21"            # 0108 int 21h     (int21_dos effect tag)
        "A32000"          # 010A mov [0020],ax
        "C3")             # 010D ret
    fetch = lambda off: code[(off - ENTRY) & 0xFFFF] if 0 <= (off - ENTRY) < len(code) else 0x90
    scan = scan_function(fetch, ENTRY)
    assert scan.liftable

    rec = irgen_core.function_record(
        scan, CS, ENTRY, fetch, environment_wait_entries=set())
    assert rec["liftable"]
    assert any(i.get("platform_effect") == "int21_dos"
               for b in rec["blocks"] for i in b["instructions"])

    # Scan-path emission (what liftemit's exe+snapshot path produces).
    name = f"lifted_{CS:04x}_{ENTRY:04x}"
    sig = bytes.fromhex(rec["signature"])
    scan_src = emit_function(scan, CS, name, signature=sig,
                             count_instructions=True)

    # IR-path emission through the real consumer.
    out = Path(__file__).parent / "_irgate_tmp"
    out.mkdir(exist_ok=True)
    try:
        status, detail = liftemit.emit_entry_from_ir(rec, out, None)
        assert status == "ok", detail
        ir_src = (out / f"{name}.py").read_text(encoding="utf-8")
    finally:
        for p in out.glob("*.py"):
            p.unlink()
        out.rmdir()

    assert ir_src == scan_src


# --- the generic-core seam ---------------------------------------------------

# A function with a far call (platform-taggable by a front-end), an int 21h
# (DOS-tagged by the default tagger) and a ret.
_SEAM_CODE = bytes.fromhex(
    "9A34126000"      # 0100 call far 0060:1234  (an import-thunk-style target)
    "CD21"            # 0105 int 21h             (int21_dos under the DOS tagger)
    "C3")             # 0107 ret


def _seam_fetch(off):
    i = (off - ENTRY) & 0xFFFF
    return _SEAM_CODE[i] if i < len(_SEAM_CODE) else 0x90


def _build(effect_tagger):
    return irgen_core.build_document(
        [(CS, ENTRY)],
        fetch_for=lambda cs: _seam_fetch,
        effect_tagger=effect_tagger,
        provenance={"exe": "synthetic", "snapshot": "none",
                    "toolchain": "test", "entries": 1},
        notice="TEST DOCUMENT",
    )


def test_core_document_with_dos_tagger_is_pinned_and_deterministic():
    doc = _build(irgen_core.dos_effect_tag)
    assert doc["ir_version"] == irgen_core.IR_VERSION
    rec = doc["functions"]["1000:0100"]
    assert rec["liftable"]
    assert rec["exits"] == ["ret"]
    assert rec["calls_far"] == [["0060", "1234"]]
    assert rec["ints"] == ["21"]
    assert doc["unsupported"] == []
    insts = [i for b in rec["blocks"] for i in b["instructions"]]
    # Pin the instruction stream the core serializes (the emitters' input).
    assert [(i["ip"], i["kind"], i.get("platform_effect")) for i in insts] == [
        ("0100", "call_far", None),      # the DOS tagger does NOT tag far calls
        ("0105", "int", "int21_dos"),
        ("0107", "ret", None),
    ]
    assert insts[0]["far_target"] == ["0060", "1234"]
    # Determinism: an identical rebuild serializes byte-identically.
    assert (irgen_core.dump_document(doc)
            == irgen_core.dump_document(_build(irgen_core.dos_effect_tag)))


def test_custom_effect_tagger_tags_land_in_the_document():
    """The seam contract: a front-end's platform tagger (e.g. Win16 far-call-
    to-thunk recognition) flows into the serialized IR untouched."""
    def tagger(inst):
        if inst.kind == "call_far" and inst.far_target == (0x0060, 0x1234):
            return "api:TESTMOD.Frob"
        return irgen_core.dos_effect_tag(inst)

    doc = _build(tagger)
    insts = [i for b in doc["functions"]["1000:0100"]["blocks"]
             for i in b["instructions"]]
    assert [i.get("platform_effect") for i in insts] == [
        "api:TESTMOD.Frob", "int21_dos", None]


def test_unsupported_ledger_is_fail_loud():
    """An unliftable entry lands in the ledger, never silently dropped."""
    bad = bytes.fromhex("63c0")   # arpl — the scanner refuses
    doc = irgen_core.build_document(
        [(CS, ENTRY)],
        fetch_for=lambda cs: (lambda off: bad[(off - ENTRY) & 0xFFFF]
                              if (off - ENTRY) & 0xFFFF < len(bad) else 0x90),
        provenance={"exe": "synthetic", "snapshot": "none",
                    "toolchain": "test", "entries": 1},
        notice="TEST DOCUMENT",
    )
    rec = doc["functions"]["1000:0100"]
    assert not rec["liftable"]
    assert doc["unsupported"]
    assert all(u["entry"] == "1000:0100" for u in doc["unsupported"])


def test_identity_provider_embeds_in_records_and_ledger():
    """The pluggable identity provider: front-end metadata (symbol/module
    names, segment aliases) lands in the function record AND in unsupported-
    ledger entries — refusals name the symbol, not just the address."""
    identity = {(CS, ENTRY): {"symbol": "_Frob", "module": "TEST_MODULE",
                              "ne_seg": 5}}
    doc = _build_with(identity_for=lambda cs, ip: identity.get((cs, ip), {}))
    rec = doc["functions"]["1000:0100"]
    assert (rec["symbol"], rec["module"], rec["ne_seg"]) == (
        "_Frob", "TEST_MODULE", 5)

    bad = bytes.fromhex("63c0")   # arpl — refused, so the ledger fills
    doc = irgen_core.build_document(
        [(CS, ENTRY)],
        fetch_for=lambda cs: (lambda off: bad[(off - ENTRY) & 0xFFFF]
                              if (off - ENTRY) & 0xFFFF < len(bad) else 0x90),
        provenance={"exe": "synthetic", "snapshot": "none",
                    "toolchain": "test", "entries": 1},
        notice="TEST DOCUMENT",
        identity_for=lambda cs, ip: identity.get((cs, ip), {}),
    )
    assert doc["unsupported"]
    for u in doc["unsupported"]:
        assert u["symbol"] == "_Frob" and u["module"] == "TEST_MODULE"


def _build_with(**kw):
    return irgen_core.build_document(
        [(CS, ENTRY)],
        fetch_for=lambda cs: _seam_fetch,
        provenance={"exe": "synthetic", "snapshot": "none",
                    "toolchain": "test", "entries": 1},
        notice="TEST DOCUMENT",
        **kw)
