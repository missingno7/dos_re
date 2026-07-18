"""A COMPOSED needs-plat callee anchors its platform effects at the right time.

When a caller composes a callee that itself makes a platform effect (a
``plat.farcall`` to a Win16 API thunk, a ``plat.intr``, a boundary observer),
the caller hands the callee its ``_base`` -- the ABSOLUTE ``instruction_count``
at the callee's entry, from which the callee computes each effect's virtual
time.  The callee is entered AFTER the caller's ``call`` executes, so ``_base``
must include that call instruction: ``_base + _cost + count`` (``count`` already
counts the call), exactly as the STANDALONE adapter sets ``_base =
cpu.instruction_count`` at entry.  The emitter used ``count - 1`` here, so a
platform effect inside a composed needs-plat callee anchored ONE instruction
early -- an API-boundary checkpoint sampled inside such a callee drifted -1 vs
the interpreter.  The standalone farcall path never exercised a composed
needs-plat callee, so this stayed latent until frame/retf composition reached a
caller that inlines an API-calling helper (SimAnt's ``_mem_Size`` behind
``430E:7860``).

DIFFERENTIAL: the composed caller+callee bodies are exec'd and the ABSOLUTE
virtual time at which the inner ``plat.farcall`` fires is diffed against the
interpreter stepping the identical bytes.  It FAILS on the old ``count - 1``.
"""
from __future__ import annotations

import sys
import types

from dos_re.cpu import CPU8086, CPUState
from dos_re.lift.cfg import scan_function
from dos_re.lift.emit_cpuless import (CalleeContract, PlatformFarCall,
                                      check_promotable, emit_recovered,
                                      _contract_inputs)
from dos_re.memory import Memory

W16 = ("ax", "cx", "dx", "bx", "sp", "bp", "si", "di")
THUNK_SEG, SLOT = 0x0060, 0x0000
ARGBYTES = 2
CALLEE_OFF, CALLER_OFF = 0x0000, 0x0100
CS = 0x2000

# needs-plat callee K (far): push an arg, call the API thunk, far-return.
#   50           push ax
#   9A 00 00 60 00  call far 0060:0000
#   CB           retf
_CALLEE = bytes.fromhex("50" "9a00006000" "cb")

# caller C (far): two nops, compose K, far-return.
#   90 90        nop; nop
#   9A 00 00 00 20  call far 2000:0000
#   CB           retf
_CALLER = bytes.fromhex("9090" "9a00000020" "cb")


def _scan(code, base):
    return scan_function(
        lambda o: code[o - base] if 0 <= o - base < len(code) else 0x90, base)


def _api_hook(record):
    def api(cpu):
        record.append(cpu.instruction_count)     # WHEN the API fires
        s = cpu.s
        ss, sp = s.ss & 0xFFFF, s.sp & 0xFFFF
        ret_off = cpu.mem.rw(ss, sp)
        ret_cs = cpu.mem.rw(ss, (sp + 2) & 0xFFFF)
        s.sp = (sp + 4 + ARGBYTES) & 0xFFFF
        s.cs, s.ip = ret_cs & 0xFFFF, ret_off & 0xFFFF
        s.ax = 0x1234
    return api


def _interp_fire_time():
    """Absolute instruction_count at which the inner API fires, interpreter."""
    mem = Memory()
    for k, b in enumerate(_CALLEE):
        mem.data[(CS << 4) + CALLEE_OFF + k] = b
    for k, b in enumerate(_CALLER):
        mem.data[(CS << 4) + CALLER_OFF + k] = b
    rec: list[int] = []
    st = CPUState(cs=CS, ip=CALLER_OFF, ss=0x3000, ax=0)
    st.sp = 0x0100
    cpu = CPU8086(mem, st)
    cpu.replacement_hooks[(THUNK_SEG, SLOT)] = _api_hook(rec)
    ret_ip = CALLER_OFF + len(_CALLER) - 1
    for _ in range(64):
        if (cpu.s.cs & 0xFFFF) == CS and (cpu.s.ip & 0xFFFF) == ret_ip:
            break
        cpu.step()
    return rec[0]


def _composed_fire_time(base):
    plat_far = {(THUNK_SEG, SLOT): PlatformFarCall(argbytes=ARGBYTES)}
    k_scan = _scan(_CALLEE, CALLEE_OFF)
    k_spec = check_promotable(k_scan, plat_far_segs=frozenset({THUNK_SEG}),
                              plat_farcalls=plat_far)
    assert k_spec.needs_plat and k_spec.ret_kind == "far"
    base_pkg = "t_cpb"
    k_src = emit_recovered(k_scan, k_spec.abi, "2000:0000",
                           recovered_import_base=base_pkg,
                           needs_plat=True, df_livein=k_spec.df_livein,
                           sp_output=k_spec.sp_output,
                           flags_livein=k_spec.flags_livein,
                           plat_farcalls=plat_far)
    pkg = types.ModuleType(base_pkg)
    pkg.__path__ = []
    sys.modules[base_pkg] = pkg
    kmod = types.ModuleType(base_pkg + ".func_2000_0000")
    exec(compile(k_src, "<k>", "exec"), kmod.__dict__)
    sys.modules[base_pkg + ".func_2000_0000"] = kmod

    k_contract = CalleeContract(
        name="func_2000_0000",
        inputs=tuple(_contract_inputs(k_scan, k_spec.abi)),
        outputs=tuple(sorted((k_spec.abi.outputs
                              & frozenset(W16[:4] + ("si", "di", "bp", "es")))
                             - frozenset({"sp"}))),
        exit_flags=k_spec.exit_flags, needs_plat=True, ret_kind="far",
        df_livein=k_spec.df_livein, sp_delta=k_spec.sp_delta,
        ret_pop=k_spec.ret_pop, sp_output=k_spec.sp_output,
        sp_deltas=k_spec.sp_deltas, flags_livein=k_spec.flags_livein)

    c_scan = _scan(_CALLER, CALLER_OFF)
    far_callees = {(CS, CALLEE_OFF): k_contract}
    c_spec = check_promotable(c_scan, far_callees=far_callees)
    assert c_spec.needs_plat            # composing a needs-plat callee propagates
    c_src = emit_recovered(c_scan, c_spec.abi, "2000:0100",
                           recovered_import_base=base_pkg,
                           far_callees=far_callees, needs_plat=True,
                           df_livein=c_spec.df_livein, sp_output=c_spec.sp_output,
                           flags_livein=c_spec.flags_livein)
    ns = {"_PARITY": [0] * 256}
    exec(compile(c_src, "<c>", "exec"), ns)
    c_fn = ns["func_2000_0100"]

    rec: list[int] = []

    class _Plat:
        def farcall(self, seg, off, regs, argbytes, cost):
            rec.append(cost)                     # the ABSOLUTE anchor handed in
            out = {r: regs.get(r, 0) & 0xFFFF for r in
                   ("ax", "bx", "cx", "dx", "si", "di", "bp", "ds", "es")}
            out["flags"] = regs.get("_flags", 2)
            out["halted"] = False
            out["cost"] = 1
            return out

    mem = Memory()
    kw = {r: 0 for r in _contract_inputs(c_scan, c_spec.abi)
          if r not in ("sp", "ss")}
    c_fn(mem, _Plat(), _base=base, ss=0x3000, sp=0x0100, **kw)
    return rec[0]


def test_composed_needs_plat_callee_anchors_the_api_at_the_right_time():
    interp = _interp_fire_time()          # e.g. 5 instructions from C entry
    composed = _composed_fire_time(base=0)
    assert composed == interp, (
        f"composed API fire time {composed} != interpreter {interp} "
        f"(a -1 here is the count-1 base-anchor bug)")
