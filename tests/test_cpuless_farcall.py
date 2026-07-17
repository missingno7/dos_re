"""Platform far-call composition (plat.farcall) -- the CPUless de-carrier's
far-call analogue of plat.intr.

A `call far seg:off` into a declared PLATFORM-BOUNDARY segment (a Win16 import
thunk, a DOS API gateway) is not a game function with a recoverable body -- it
is a platform service.  The CPUless emitter used to REFUSE such a call
("contains-call"), which stalled nearly every non-trivial calls-only function
(they all reach an API through the thunk table).  It now composes the call as a
`plat.farcall` platform effect: the recovered body pushes the pascal args + the
far return frame, hands the register bundle + FLAGS word to the platform, and
merges back AX/DX + the convention bundle + flags, with the pascal callee
cleanup (SP += 4 + argbytes) owned by the recovered body.

Two halves, as with every emitter capability:

  * the ABI/gate side -- a far-call into a boundary segment stops being a
    refusal when a contract (argbytes) is supplied, and REFUSES loudly when the
    boundary is known but the contract is not (never guesses the arg count);
  * the DIFFERENTIAL -- the pure-Python body the emitter writes, wired to the
    live API hook through VMlessPlatformAdapter.farcall, computes byte-for-byte
    what the interpreter computes over identical state (registers, flags, the
    memory the API wrote, and the pascal-cleaned stack pointer).
"""
from __future__ import annotations

import sys
import types

import pytest

from dos_re.cpu import CPU8086, CPUState
from dos_re.lift.cfg import FunctionScan
from dos_re.lift.decode import decode_one
from dos_re.lift.cpuless import abi_scan
from dos_re.lift.emit_cpuless import (
    PlatformFarCall, Refusal, check_promotable, emit_adapter, emit_recovered)
from dos_re.memory import Memory

THUNK_SEG = 0x0060
SLOT = 0x0000
ARGBYTES = 4                       # two pascal word args


# push args (7 then 3), call far 0060:0000, ret.
#   B8 07 00  mov ax,7      (ip 0)
#   50        push ax       (ip 3)
#   B8 03 00  mov ax,3      (ip 4)
#   50        push ax       (ip 7)
#   9A 00 00 60 00  call far 0060:0000  (ip 8)
#   C3        ret           (ip 13)
_CODE = bytes.fromhex("B80700" "50" "B80300" "50" "9A00006000" "C3")


def _scan(code: bytes) -> FunctionScan:
    fetch = lambda o: code[o] if o < len(code) else 0x90  # noqa: E731
    s = FunctionScan(entry=0)
    ip = 0
    while ip < len(code):
        i = decode_one(fetch, ip)
        s.insts[ip] = i
        if i.kind in ("ret", "retf", "iret"):
            s.exits.append(i)
            break
        ip = i.next_ip
    return s


def _make_api_hook():
    """A synthetic pascal API at the thunk slot: reads two word args off the
    stack, returns their sum in AX, writes it to DS:0002 (an observable memory
    effect), clobbers BX (a volatile register), sets CF, and far-returns
    popping the 4-byte frame + ARGBYTES (the pascal cleanup).  A plain hook (NO
    owns_time) so the interpreter charges it one virtual instruction, exactly
    as the real Win16 API dispatch is charged."""
    def api(cpu):
        s = cpu.s
        ss, sp = s.ss & 0xFFFF, s.sp & 0xFFFF
        a0 = cpu.mem.rw(ss, (sp + 4) & 0xFFFF)
        a1 = cpu.mem.rw(ss, (sp + 6) & 0xFFFF)
        total = (a0 + a1) & 0xFFFF
        cpu.mem.ww(s.ds & 0xFFFF, 0x0002, total)      # DS-relative memory write
        s.bx = 0xBEEF                                 # clobber a volatile reg
        ret_off = cpu.mem.rw(ss, sp)
        ret_cs = cpu.mem.rw(ss, (sp + 2) & 0xFFFF)
        s.sp = (sp + 4 + ARGBYTES) & 0xFFFF           # pascal cleanup (retf N)
        s.cs, s.ip = ret_cs & 0xFFFF, ret_off & 0xFFFF
        s.ax = total
        s.flags |= 0x0001                             # set CF
    return api


_ENTRY_REGS = dict(ax=0x0000, bx=0x1111, cx=0x2222, dx=0x4444,
                   si=0x3333, di=0x5555, bp=0x6666, ds=0x1000, es=0x7777)
_ENTRY_FLAGS = 0x0002 | 0x0040        # base bits + ZF set (must be preserved)
_SS = 0x3000
_SP0 = 0x0100
_RET_SENTINEL = 0xDEAD


def _seed(mem: Memory) -> None:
    for k, b in enumerate(_CODE):
        mem.data[(0x2000 << 4) + k] = b
    mem.ww(_SS, _SP0, _RET_SENTINEL)     # the near ret's target


def _run(cpu: CPU8086) -> None:
    for _ in range(64):
        cpu.step()
        if (cpu.s.cs & 0xFFFF) == 0x2000 and (cpu.s.ip & 0xFFFF) == _RET_SENTINEL:
            return
    raise AssertionError("function did not return within the step budget")


def _interp_run():
    mem = Memory()
    _seed(mem)
    st = CPUState(cs=0x2000, ip=0, ss=_SS, **_ENTRY_REGS)
    st.sp = _SP0
    st.flags = _ENTRY_FLAGS
    cpu = CPU8086(mem, st)
    cpu.replacement_hooks[(THUNK_SEG, SLOT)] = _make_api_hook()
    _run(cpu)
    return cpu, mem


def _compile_installed_hook():
    """Emit the recovered body + CPU-ABI adapter, wire the adapter's import to
    the recovered module, and return the installable ``lifted_2000_0000(cpu)``
    hook -- the same artifact the promoter routes into the graph."""
    scan = _scan(_CODE)
    plat = {(THUNK_SEG, SLOT): PlatformFarCall(argbytes=ARGBYTES, name="TESTAPI")}
    spec = check_promotable(scan, plat_far_segs=frozenset({THUNK_SEG}),
                            plat_farcalls=plat)
    assert spec.needs_plat and spec.flags_livein and spec.ret_kind == "near"
    base = "t_pf_pkg"
    rec_src = emit_recovered(
        scan, spec.abi, "2000:0000", recovered_import_base=base,
        needs_plat=spec.needs_plat, df_livein=spec.df_livein,
        sp_output=spec.sp_output, flags_livein=spec.flags_livein,
        plat_farcalls=plat)
    ad_src = emit_adapter(
        scan, spec.abi, "2000:0000", signature=_CODE,
        recovered_import_base=base, needs_plat=spec.needs_plat,
        ret_kind=spec.ret_kind, df_livein=spec.df_livein,
        sp_output=spec.sp_output, ret_pop=spec.ret_pop,
        flags_livein=spec.flags_livein)
    pkg = types.ModuleType(base)
    pkg.__path__ = []                     # mark as a package for submodule import
    sys.modules[base] = pkg
    recmod = types.ModuleType(base + ".func_2000_0000")
    exec(compile(rec_src, "<recovered>", "exec"), recmod.__dict__)
    sys.modules[base + ".func_2000_0000"] = recmod
    admod = types.ModuleType(base + ".adapter")
    exec(compile(ad_src, "<adapter>", "exec"), admod.__dict__)
    return admod.lifted_2000_0000, rec_src


def _cpuless_run(hook):
    mem = Memory()
    _seed(mem)
    st = CPUState(cs=0x2000, ip=0, ss=_SS, **_ENTRY_REGS)
    st.sp = _SP0
    st.flags = _ENTRY_FLAGS
    cpu = CPU8086(mem, st)
    cpu.replacement_hooks[(THUNK_SEG, SLOT)] = _make_api_hook()
    cpu.replacement_hooks[(0x2000, 0x0000)] = hook     # the CPUless adapter
    _run(cpu)
    return cpu, mem


_COMPARE_REGS = ("ax", "bx", "cx", "dx", "si", "di", "bp", "sp", "ds", "es")


def test_platform_farcall_body_matches_the_interpreter_byte_for_byte():
    hook, rec_src = _compile_installed_hook()
    icpu, imem = _interp_run()
    ccpu, cmem = _cpuless_run(hook)

    # the register file agrees (AX = the API result, BX = the clobber, the
    # preserved registers unchanged, SP pascal-cleaned back to entry)
    for r in _COMPARE_REGS:
        assert getattr(ccpu.s, r) & 0xFFFF == getattr(icpu.s, r) & 0xFFFF, (
            f"{r}: cpuless={getattr(ccpu.s, r) & 0xFFFF:04X} "
            f"interp={getattr(icpu.s, r) & 0xFFFF:04X}")
    assert ccpu.s.ax == 0x000A and ccpu.s.bx == 0xBEEF   # the API's effects
    assert ccpu.s.cx == 0x2222 and ccpu.s.si == 0x3333   # preserved
    assert ccpu.s.sp & 0xFFFF == (_SP0 + 2) & 0xFFFF     # args cleaned + ret

    # the flags agree (CF set by the API, ZF preserved through _flags_in)
    assert (ccpu.s.flags & 0xFFFF) == (icpu.s.flags & 0xFFFF)
    assert ccpu.s.flags & 0x0001 and ccpu.s.flags & 0x0040

    # the memory the API wrote (DS:0002) is byte-identical
    base = (0x1000 << 4)
    assert bytes(cmem.data[base:base + 8]) == bytes(imem.data[base:base + 8])
    assert cmem.rw(0x1000, 0x0002) == 0x000A

    # and the virtual clock advanced identically -- the call far (1) + the API
    # hook dispatch (1) cost exactly two instructions on both sides
    assert ccpu.instruction_count == icpu.instruction_count

    # the emitted body routes through the platform seam, not a game callee
    assert "plat.farcall(0x0060, 0x0000" in rec_src
    assert "argbytes" not in rec_src            # the number is inlined, not named
    # the dispatch cost is the DYNAMIC value the platform reports, not a fixed
    # +1 -- virtual time follows the real service (instruction_count matches the
    # interpreter above), so the body adds back plat.farcall's returned cost.
    assert "_cost += _fo['cost']" in rec_src


def test_boundary_farcall_without_a_contract_refuses_loud():
    """A far-call into a KNOWN platform-boundary segment with NO contract must
    refuse (the arg cleanup is unknown -- never guessed), and WITHOUT declaring
    the segment as a boundary at all it stays the ordinary uncomposed
    contains-call refusal.  Supplying the contract is what promotes it."""
    scan = _scan(_CODE)
    # boundary declared, but no per-target contract -> fail loud, distinctly
    with pytest.raises(Refusal) as e:
        check_promotable(scan, plat_far_segs=frozenset({THUNK_SEG}),
                         plat_farcalls={})
    assert "platform-farcall-contract-unknown" in str(e.value)

    # not declared a boundary at all -> the plain uncomposed-call refusal
    with pytest.raises(Refusal) as e2:
        check_promotable(scan)
    assert "contains-call" in str(e2.value)

    # contract supplied -> promotes
    spec = check_promotable(scan, plat_far_segs=frozenset({THUNK_SEG}),
                            plat_farcalls={(THUNK_SEG, SLOT):
                                           PlatformFarCall(argbytes=ARGBYTES)})
    assert spec is not None and spec.needs_plat


def test_farcall_stack_depth_is_balanced_across_the_pascal_cleanup():
    """The `push args; call far; retf argbytes` sequence is stack-balanced, so
    the function's own exit stays balanced (sp is NOT a runtime output) -- the
    depth tracker must credit the +argbytes pop at the call site."""
    scan = _scan(_CODE)
    spec = check_promotable(scan, plat_far_segs=frozenset({THUNK_SEG}),
                            plat_farcalls={(THUNK_SEG, SLOT):
                                           PlatformFarCall(argbytes=ARGBYTES)})
    assert spec.sp_output is False and spec.sp_delta == 0


def test_abi_scan_reads_the_full_bundle_and_pops_argbytes():
    scan = _scan(_CODE)
    abi = abi_scan(scan, plat_farcalls={(THUNK_SEG, SLOT): ARGBYTES})
    assert not abi.refusals                     # no contains-call refusal
    # the API bundle is live-in (the body echoes it back) -- every register the
    # farcall reads that this body does not write before the call (ax is set by
    # `mov ax` first, so it is not a live-in here)
    assert {"bx", "cx", "dx", "si", "di", "bp", "ds", "es"} <= set(abi.inputs)
    # and written back (AX/DX + the convention clobbers are outputs)
    assert {"ax", "bx", "cx", "dx", "es"} <= set(abi.outputs)
