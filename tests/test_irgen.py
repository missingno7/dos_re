"""Recovery IR v0 round-trip: scan-path emission == IR-path emission.

The IR equivalence gate (docs/recovery_ir.md §3.3) in miniature, synthetic
(game-free tests rule): serialize a scanned function into an IR record the
way irgen does, re-elaborate it through liftemit's emit_entry_from_ir path,
and require the emitted module to be BYTE-IDENTICAL to the scan-path module.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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


class _FakeMem:
    """Just enough of Memory for irgen's signature recipe."""

    def __init__(self, code: bytes, entry: int):
        self.code, self.entry = code, entry

    def rb(self, seg, off):
        i = (off - self.entry) & 0xFFFF
        return self.code[i] if i < len(self.code) else 0x90


def test_ir_record_reemits_byte_identical():
    irgen = _load_tool("irgen")
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

    mem = _FakeMem(code, ENTRY)
    rec = irgen._function_record(scan, CS, ENTRY, mem, keep_interpreted=set())
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
