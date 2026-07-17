"""A `cs:[...]` access in a recovered function resolves to the fixed code segment.

CS is the function's own code segment -- a compile-time constant, not a runtime
input the ABI carries (it carries ds/es/ss).  A CS-relative memory read -- notably
the dynamic-dispatch SELECTOR read `mov reg, cs:[bx+disp]`, which bypasses the
normal ABI input pass -- must therefore resolve against a function-local `cs`
constant, not an undefined name (which used to `NameError` at runtime and was
caught by the demo-driven differential on OVERKILL's object-walk dispatchers).
"""
from __future__ import annotations

from dos_re.lift.cfg import FunctionScan
from dos_re.lift.decode import decode_one
from dos_re.lift.emit_cpuless import emit_recovered
from dos_re.lift.cpuless import abi_scan


def _scan(code: bytes, base: int = 0) -> FunctionScan:
    fetch = lambda o: code[o - base] if 0 <= o - base < len(code) else 0x90  # noqa: E731
    s = FunctionScan(entry=base)
    ip = base
    while ip - base < len(code):
        i = decode_one(fetch, ip)
        s.insts[ip] = i
        if i.kind in ("ret", "retf", "iret"):
            s.exits.append(i)
            break
        ip = i.next_ip
    return s


def test_cs_relative_read_defines_the_cs_constant():
    # 2E 8B 07  mov ax, cs:[bx]   ; C3 ret
    scan = _scan(bytes.fromhex("2e8b07" "c3"), base=0x0100)
    src = emit_recovered(scan, abi_scan(scan), "1010:0100")
    assert "cs = 0x1010" in src            # the fixed code segment, defined as a local
    assert "cs=" not in src.split("def func", 1)[1].split(":", 1)[0]  # cs is NOT a param
    # and the body actually executes without a NameError for cs (cs is a local)
    ns = {"_PARITY": [0] * 256}
    exec(compile(src, "<rec>", "exec"), ns)
    out, _compat = ns["func_1010_0100"](_FakeMem(), bx=0x10)
    assert "ax" in out                     # ran to completion, cs resolved


class _FakeMem:
    def __init__(self):
        self.data = bytearray(0x120000)

    def rw(self, seg, off):
        return self.data[(seg << 4) + off] | (self.data[(seg << 4) + off + 1] << 8)

    def rb(self, seg, off):
        return self.data[(seg << 4) + off]

    def ww(self, seg, off, v):
        self.data[(seg << 4) + off] = v & 0xFF
        self.data[(seg << 4) + off + 1] = (v >> 8) & 0xFF

    def wb(self, seg, off, v):
        self.data[(seg << 4) + off] = v & 0xFF
