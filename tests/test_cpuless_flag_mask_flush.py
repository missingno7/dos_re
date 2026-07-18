"""A flag written earlier in the block is the body's OWN flag at a call site.

``_fmask`` is the recovered body's claim about the FLAGS word: a bit inside the
mask holds a value this body computed, a bit outside it rides the caller's
``_flags_in``.  Every site that hands an OUTGOING flags word to something else
-- a platform far-call or INT, a dynamic dispatch, a flags-livein callee, a
boundary observer -- rebuilds the word as ``(_flags_in & ~_fmask) | (live &
_fmask)``.

The mask was only written at the END of a block (one ``|=`` per block rather
than one per instruction).  So a flag written EARLIER IN THE SAME BLOCK was not
yet in the mask when such a site read it, and the site handed out the CALLER's
stale bit instead of the value the body had just computed::

    or cx, cx          ; ZF := (cx == 0)   <- recorded only at the block end
    push arg ; push arg
    call far <API>     ; composes the flags word HERE -- ZF taken from
                       ;   _flags_in, i.e. the caller's entry ZF

The interpreter, of course, hands the API the ZF the ``or`` computed.  Anything
downstream of the API's returned flags word then diverges: here the API
preserves what it is given, so the divergence survives all the way to the
function's exit flags -- a silent wrong answer, not a loud one.

The fix is to flush the pending bits into ``_fmask`` at the site, which is
simply recording them when they become true rather than when the block ends.
This is a DIFFERENTIAL regression: the composed body runs through its CPU-ABI
adapter and its exit flags are diffed against stepping the identical bytes
through ``CPU8086``.  It FAILS on the old emitter (ZF=1 from the entry word
where the interpreter has ZF=0).
"""
from __future__ import annotations

import sys
import types

from dos_re.cpu import CPU8086, CPUState
from dos_re.lift.cfg import FunctionScan
from dos_re.lift.decode import decode_one
from dos_re.lift.emit_cpuless import (
    PlatformFarCall, check_promotable, emit_adapter, emit_recovered)
from dos_re.memory import Memory

THUNK_SEG, SLOT, ARGBYTES = 0x0060, 0x0000, 4
_SS, _SP0, _DS = 0x3000, 0x0100, 0x1000
_RET = 0xDEAD

#   0B C9           or cx, cx     <- writes ZF/PF/SF/CF/OF (CX != 0 -> ZF=0)
#   B8 07 00 / 50   mov ax,7 ; push ax
#   B8 03 00 / 50   mov ax,3 ; push ax
#   9A 00 00 60 00  call far 0060:0000   <- composes the outgoing FLAGS word
#   C3              ret
_CODE = bytes.fromhex("0bc9" "b80700" "50" "b80300" "50" "9a00006000" "c3")

_ENTRY_REGS = dict(ax=0, bx=0x1111, cx=0x2222, dx=0x4444, si=0x3333,
                   di=0x5555, bp=0x6666, ds=_DS, es=0x7777)
_ENTRY_FLAGS = 0x0002 | 0x0040          # base bits + ZF SET at entry
_PLAT = {(THUNK_SEG, SLOT): PlatformFarCall(argbytes=ARGBYTES, name="TESTAPI")}


def _scan(code):
    s = FunctionScan(entry=0)
    ip = 0
    while ip < len(code):
        i = decode_one(lambda o: code[o] if o < len(code) else 0x90, ip)
        s.insts[ip] = i
        if i.kind in ("ret", "retf", "iret"):
            s.exits.append(i)
            break
        ip = i.next_ip
    return s


def _api(cpu):
    """A pascal API that PRESERVES the flags it is given (bar setting CF), so
    the caller's ZF must survive the round trip exactly as the body computed
    it."""
    s = cpu.s
    ss, sp = s.ss & 0xFFFF, s.sp & 0xFFFF
    total = (cpu.mem.rw(ss, (sp + 4) & 0xFFFF)
             + cpu.mem.rw(ss, (sp + 6) & 0xFFFF)) & 0xFFFF
    ret_off, ret_cs = cpu.mem.rw(ss, sp), cpu.mem.rw(ss, (sp + 2) & 0xFFFF)
    s.sp = (sp + 4 + ARGBYTES) & 0xFFFF
    s.cs, s.ip, s.ax = ret_cs, ret_off, total
    s.flags |= 0x0001


def _run(hook=None):
    mem = Memory()
    for k, b in enumerate(_CODE):
        mem.data[(0x2000 << 4) + k] = b
    mem.ww(_SS, _SP0, _RET)
    st = CPUState(cs=0x2000, ip=0, ss=_SS, **_ENTRY_REGS)
    st.sp = _SP0
    st.flags = _ENTRY_FLAGS
    cpu = CPU8086(mem, st)
    cpu.replacement_hooks[(THUNK_SEG, SLOT)] = _api
    if hook is not None:
        cpu.replacement_hooks[(0x2000, 0x0000)] = hook
    for _ in range(64):
        cpu.step()
        if (cpu.s.cs & 0xFFFF) == 0x2000 and (cpu.s.ip & 0xFFFF) == _RET:
            return cpu
    raise AssertionError("function did not return within the step budget")


def _hook():
    scan = _scan(_CODE)
    spec = check_promotable(scan, plat_far_segs=frozenset({THUNK_SEG}),
                            plat_farcalls=_PLAT)
    base = "t_fmask_pkg"
    rec = emit_recovered(scan, spec.abi, "2000:0000",
                         recovered_import_base=base, needs_plat=spec.needs_plat,
                         df_livein=spec.df_livein, sp_output=spec.sp_output,
                         flags_livein=spec.flags_livein, plat_farcalls=_PLAT)
    ad = emit_adapter(scan, spec.abi, "2000:0000", signature=_CODE,
                      recovered_import_base=base, needs_plat=spec.needs_plat,
                      ret_kind=spec.ret_kind, df_livein=spec.df_livein,
                      sp_output=spec.sp_output, ret_pop=spec.ret_pop,
                      flags_livein=spec.flags_livein)
    pkg = types.ModuleType(base)
    pkg.__path__ = []
    sys.modules[base] = pkg
    m = types.ModuleType(base + ".func_2000_0000")
    exec(compile(rec, "<recovered>", "exec"), m.__dict__)
    sys.modules[base + ".func_2000_0000"] = m
    a = types.ModuleType(base + ".adapter")
    exec(compile(ad, "<adapter>", "exec"), a.__dict__)
    return a.lifted_2000_0000, rec


def test_a_flag_written_before_a_platform_farcall_reaches_the_platform():
    hook, rec = _hook()
    interp, cpuless = _run(), _run(hook)
    assert (cpuless.s.flags & 0xFFFF) == (interp.s.flags & 0xFFFF), (
        f"cpuless={cpuless.s.flags & 0xFFFF:04X} "
        f"interp={interp.s.flags & 0xFFFF:04X}")
    # the `or cx,cx` cleared ZF; the entry word had it SET, so a body that
    # handed the API _flags_in's ZF would end with ZF set here.
    assert not (cpuless.s.flags & 0x0040)
    assert cpuless.s.flags & 0x0001          # CF from the API
    # the mask is recorded AT the site, before the word is composed
    site = rec.index("_ff = (_flags_in & ~_fmask)")
    assert "_fmask |= " in rec[:site]
