"""An INDIRECT far call composes from per-site evidence, behind a guard.

A program that computes a far pointer at runtime and calls through it (``call
far [bp-4]``, ``call far es:[disp16]``, ``call far [disp16]``) has no static
target: the pointer is data.  The CPUless emitter refused every such site
(``indirect-control-flow``) -- and with it every function containing one, which
in a real binary is most of the ones that reach a service through a computed
pointer.

THE MECHANISM.  A composed indirect far call is a GUARDED FAN-OUT OVER STATIC
FAR CALLS.  The observed-target evidence channel that already feeds NEAR
indirect dispatch (``{site: [targets]}``) is extended to far transfers -- same
wire format, the target's segment simply comes from memory instead of CS -- and
each observed pointer is composed by exactly the rule that governs a DIRECT
``call far`` to that same target.  Nothing here knows what a target IS; it only
knows how a static call to it would have composed, which is why a platform
boundary target and a recovered far-return body both fall out with no case
analysis, and why any future static far-call rule is inherited for free.

THE GUARD.  Observed evidence is not proof: a capture shows what a pointer HELD
on the paths that ran, never what it CAN hold.  So the site is emitted with a
runtime guard -- a pointer outside the arm set raises
``UnknownFarDispatchTarget`` naming the site and the unresolved pointer.  There
is no fallback arm and no default, so a mis-captured evidence set becomes a
loud stop under replay rather than a wrong answer.

These are DIFFERENTIAL regressions: the composed body is exec'd through its
CPU-ABI adapter and its whole register file, flags, memory and virtual clock
are diffed against stepping the identical bytes through ``CPU8086``.  They FAIL
on the old emitter, where the site refuses ``indirect-control-flow`` and there
is no body to compare (pinned below).
"""
from __future__ import annotations

import sys
import types

import pytest

from dos_re.cpu import CPU8086, CPUState
from dos_re.lift.cfg import FunctionScan
from dos_re.lift.decode import decode_one
from dos_re.lift.dispatch import resolve_far_indirect_target
from dos_re.lift.emit_cpuless import (
    CalleeContract, PlatformFarCall, Refusal, check_promotable, emit_adapter,
    emit_recovered)
from dos_re.memory import Memory

THUNK_SEG = 0x0060
SLOT = 0x0000
ARGBYTES = 4                       # two pascal word args
FAR_KEY = f"{THUNK_SEG:04X}:{SLOT:04X}"

_SS = 0x3000
_SP0 = 0x0100
_DS = 0x1000
_ES = 0x7777
_RET_SENTINEL = 0xDEAD
_PTR_OFF = 0x0100                  # where the [disp16] shapes keep the pointer

# --- the site shapes, taken from the real distribution -------------------
# In a 6-segment commercial Win16 binary the 103 indirect FAR call sites are:
#   68  call far [bp+disp8]     (a far pointer in a frame local)
#   18  call far es:[disp16]
#   13  call far [disp16]       (DGROUP-relative)
#    2  call far es:[bx]
#    1  call far [bp+disp16]
#    1  call far [di]
# Every one is MEMORY-indirect -- FF /3 cannot take a register operand, since
# no 16-bit register holds a 32-bit far pointer -- so one mechanism (read the
# far pointer at the effective address, then dispatch it) covers all of them,
# and the EA shape is just the ordinary addressing the emitter already writes.

# push bp; mov bp,sp; sub sp,4; STORE the far pointer into the local;
# push args; call far [bp-4]; mov sp,bp; pop bp; ret
#   -- the dominant shape: the program itself writes the pointer it will later
#      call through, exactly as a runtime-linked entry point is used.
_FRAME_LOCAL = bytes.fromhex(
    "55"                # push bp
    "8bec"              # mov bp, sp
    "83ec04"            # sub sp, 4
    f"c746fc{SLOT & 0xFF:02x}{SLOT >> 8:02x}"          # mov [bp-4], off
    f"c746fe{THUNK_SEG & 0xFF:02x}{THUNK_SEG >> 8:02x}"  # mov [bp-2], seg
    "b80700" "50"       # mov ax,7 ; push ax
    "b80300" "50"       # mov ax,3 ; push ax
    "ff5efc"            # call far [bp-4]      <- the site
    "8be5"              # mov sp, bp
    "5d"                # pop bp
    "c3")               # ret
_FRAME_SITE_IP = 0x18

# push args; call far es:[0x0100]; ret -- the ES-relative shape.
_ES_DIRECT = bytes.fromhex(
    "b80700" "50"
    "b80300" "50"
    f"26ff1e{_PTR_OFF & 0xFF:02x}{_PTR_OFF >> 8:02x}"   # call far es:[0x0100]
    "c3")
_ES_SITE_IP = 0x08

# push args; call far [0x0100]; ret -- the DGROUP-relative shape.
_DS_DIRECT = bytes.fromhex(
    "b80700" "50"
    "b80300" "50"
    f"ff1e{_PTR_OFF & 0xFF:02x}{_PTR_OFF >> 8:02x}"     # call far [0x0100]
    "c3")
_DS_SITE_IP = 0x08


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
        if i.kind == "call_ind":
            s.calls_indirect.append(ip)
        ip = i.next_ip
    return s


def _make_api_hook():
    """A synthetic pascal API at the thunk slot: sums two word args into AX,
    writes the sum to DS:0002, clobbers BX, sets CF, and far-returns with the
    pascal cleanup.  Identical to the direct-far-call oracle -- the point being
    that reaching it through a computed pointer must be indistinguishable."""
    def api(cpu):
        s = cpu.s
        ss, sp = s.ss & 0xFFFF, s.sp & 0xFFFF
        a0 = cpu.mem.rw(ss, (sp + 4) & 0xFFFF)
        a1 = cpu.mem.rw(ss, (sp + 6) & 0xFFFF)
        total = (a0 + a1) & 0xFFFF
        cpu.mem.ww(s.ds & 0xFFFF, 0x0002, total)
        s.bx = 0xBEEF
        ret_off = cpu.mem.rw(ss, sp)
        ret_cs = cpu.mem.rw(ss, (sp + 2) & 0xFFFF)
        s.sp = (sp + 4 + ARGBYTES) & 0xFFFF
        s.cs, s.ip = ret_cs & 0xFFFF, ret_off & 0xFFFF
        s.ax = total
        s.flags |= 0x0001
    return api


_ENTRY_REGS = dict(ax=0x0000, bx=0x1111, cx=0x2222, dx=0x4444,
                   si=0x3333, di=0x5555, bp=0x6666, ds=_DS, es=_ES)
_ENTRY_FLAGS = 0x0002 | 0x0040        # base bits + ZF (must be preserved)
_COMPARE_REGS = ("ax", "bx", "cx", "dx", "si", "di", "bp", "sp", "ds", "es")


def _seed(mem: Memory, code: bytes, ptr=(SLOT, THUNK_SEG)) -> None:
    for k, b in enumerate(code):
        mem.data[(0x2000 << 4) + k] = b
    mem.ww(_SS, _SP0, _RET_SENTINEL)          # the near ret's target
    for seg in (_DS, _ES):                    # the [disp16] shapes' pointer
        mem.ww(seg, _PTR_OFF, ptr[0])
        mem.ww(seg, (_PTR_OFF + 2) & 0xFFFF, ptr[1])


def _fresh(code, hook=None, ptr=(SLOT, THUNK_SEG)):
    mem = Memory()
    _seed(mem, code, ptr)
    st = CPUState(cs=0x2000, ip=0, ss=_SS, **_ENTRY_REGS)
    st.sp = _SP0
    st.flags = _ENTRY_FLAGS
    cpu = CPU8086(mem, st)
    cpu.replacement_hooks[(THUNK_SEG, SLOT)] = _make_api_hook()
    if hook is not None:
        cpu.replacement_hooks[(0x2000, 0x0000)] = hook
    return cpu, mem


def _run(cpu: CPU8086) -> None:
    for _ in range(128):
        cpu.step()
        if (cpu.s.cs & 0xFFFF) == 0x2000 and (cpu.s.ip & 0xFFFF) == _RET_SENTINEL:
            return
    raise AssertionError("function did not return within the step budget")


_PLAT = {(THUNK_SEG, SLOT): PlatformFarCall(argbytes=ARGBYTES, name="TESTAPI")}


def _compile(code, site_ip, targets=(FAR_KEY,), pkg_name="t_ifar",
             far_callees=None, dyn_stub=None):
    """Emit the recovered body + adapter for ``code`` with the site's observed
    evidence, and return ``(installable hook, recovered source)``."""
    scan = _scan(code)
    sites = {site_ip: list(targets)}
    spec = check_promotable(scan, plat_far_segs=frozenset({THUNK_SEG}),
                            plat_farcalls=_PLAT, far_callees=far_callees,
                            far_dyn_sites=sites)
    rec_src = emit_recovered(
        scan, spec.abi, "2000:0000", recovered_import_base=pkg_name,
        needs_plat=spec.needs_plat, df_livein=spec.df_livein,
        sp_output=spec.sp_output, flags_livein=spec.flags_livein,
        plat_farcalls=_PLAT, far_callees=far_callees, far_dyn_sites=sites)
    ad_src = emit_adapter(
        scan, spec.abi, "2000:0000", signature=code,
        recovered_import_base=pkg_name, needs_plat=spec.needs_plat,
        ret_kind=spec.ret_kind, df_livein=spec.df_livein,
        sp_output=spec.sp_output, ret_pop=spec.ret_pop,
        flags_livein=spec.flags_livein)
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = []
    sys.modules[pkg_name] = pkg
    if dyn_stub is not None:
        dc = types.ModuleType(pkg_name + "._dyncall")
        dc.dyn_exec = dyn_stub
        dc.far_dispatch_witness = _witness
        sys.modules[pkg_name + "._dyncall"] = dc
    else:
        dc = types.ModuleType(pkg_name + "._dyncall")
        dc.far_dispatch_witness = _witness
        sys.modules[pkg_name + "._dyncall"] = dc
    recmod = types.ModuleType(pkg_name + ".func_2000_0000")
    exec(compile(rec_src, "<recovered>", "exec"), recmod.__dict__)
    sys.modules[pkg_name + ".func_2000_0000"] = recmod
    admod = types.ModuleType(pkg_name + ".adapter")
    exec(compile(ad_src, "<adapter>", "exec"), admod.__dict__)
    return admod.lifted_2000_0000, rec_src, spec


class _Witness(RuntimeError):
    def __init__(self, site, seg, off, regs, base):
        super().__init__(f"{site} -> {seg:04X}:{off:04X}")
        self.site, self.target, self.regs, self.base = site, (seg, off), regs, base


def _witness(site, seg, off, regs, base):
    return _Witness(site, seg, off, regs, base)


def _assert_matches(code, site_ip, pkg, zf_preserved=True):
    hook, rec_src, spec = _compile(code, site_ip, pkg_name=pkg)
    icpu, imem = _fresh(code)
    _run(icpu)
    ccpu, cmem = _fresh(code, hook)
    _run(ccpu)
    for r in _COMPARE_REGS:
        assert getattr(ccpu.s, r) & 0xFFFF == getattr(icpu.s, r) & 0xFFFF, (
            f"{r}: cpuless={getattr(ccpu.s, r) & 0xFFFF:04X} "
            f"interp={getattr(icpu.s, r) & 0xFFFF:04X}")
    assert (ccpu.s.flags & 0xFFFF) == (icpu.s.flags & 0xFFFF)
    assert ccpu.s.ax == 0x000A and ccpu.s.bx == 0xBEEF   # the API ran
    assert ccpu.s.flags & 0x0001                         # CF set by the API
    # ZF rides _flags_in through the call -- except where the body's own frame
    # arithmetic (`sub sp,4`) computed it, which the interpreter does too.
    assert bool(ccpu.s.flags & 0x0040) == zf_preserved
    # the whole guest stack window and the API's DS write are byte-identical
    sbase = (_SS << 4)
    assert bytes(cmem.data[sbase:sbase + 0x120]) == \
        bytes(imem.data[sbase:sbase + 0x120])
    dbase = (_DS << 4)
    assert bytes(cmem.data[dbase:dbase + 16]) == bytes(imem.data[dbase:dbase + 16])
    assert ccpu.instruction_count == icpu.instruction_count
    return rec_src, spec


# --------------------------------------------------------------------------
# the differentials
# --------------------------------------------------------------------------

def test_frame_local_far_pointer_matches_the_interpreter():
    """`call far [bp-4]` -- the dominant shape.  The body stores the pointer
    into its own frame and calls through it; the composed site reads the very
    bytes the body wrote."""
    rec_src, spec = _assert_matches(_FRAME_LOCAL, _FRAME_SITE_IP, "t_ifar_bp",
                                    zf_preserved=False)
    # the pointer is READ from the frame at runtime, then guarded
    assert "_fea = ((bp + -4) & 0xFFFF)" in rec_src
    assert "_fptr = mem.rw(ss, _fea)" in rec_src
    assert "_fseg = mem.rw(ss, (_fea + 2) & 0xFFFF)" in rec_src
    assert f"if _fseg == 0x{THUNK_SEG:04X} and _fptr == 0x{SLOT:04X}:" in rec_src
    # the arm IS the static far call to that target
    assert f"plat.farcall(0x{THUNK_SEG:04X}, 0x{SLOT:04X}" in rec_src
    # ... and the pascal cleanup makes the site stack-neutral, so the function
    # stays balanced (sp is not a runtime output)
    assert spec.sp_output is False and spec.sp_delta == 0


def test_es_relative_far_pointer_matches_the_interpreter():
    """`call far es:[disp16]` -- a segment-overridden direct pointer slot."""
    rec_src, _ = _assert_matches(_ES_DIRECT, _ES_SITE_IP, "t_ifar_es")
    assert f"_fea = 0x{_PTR_OFF:X}" in rec_src
    assert "_fptr = mem.rw(es, _fea)" in rec_src       # the override is honoured


def test_dgroup_relative_far_pointer_matches_the_interpreter():
    """`call far [disp16]` -- the DS-relative pointer slot."""
    rec_src, _ = _assert_matches(_DS_DIRECT, _DS_SITE_IP, "t_ifar_ds")
    assert "_fptr = mem.rw(ds, _fea)" in rec_src


# --------------------------------------------------------------------------
# what this used to do -- the capability is what changed, nothing else
# --------------------------------------------------------------------------

@pytest.mark.parametrize("code,ip", [(_FRAME_LOCAL, _FRAME_SITE_IP),
                                     (_ES_DIRECT, _ES_SITE_IP),
                                     (_DS_DIRECT, _DS_SITE_IP)])
def test_without_the_evidence_channel_the_site_still_refuses(code, ip):
    """No evidence channel at all -> the site is exactly the refusal it always
    was.  (This is the category the three differentials above fail with on the
    old emitter.)"""
    with pytest.raises(Refusal) as e:
        check_promotable(_scan(code), plat_far_segs=frozenset({THUNK_SEG}),
                         plat_farcalls=_PLAT)
    assert "indirect-control-flow" in str(e.value)


# --------------------------------------------------------------------------
# the negatives -- evidence is data, and it is checked
# --------------------------------------------------------------------------

def test_empty_evidence_for_a_site_refuses_loud():
    """The site was offered to the channel and came back with NOTHING.  There
    is nothing to dispatch to, so composing would emit a body that can only
    raise -- refuse instead, with its own category."""
    with pytest.raises(Refusal) as e:
        check_promotable(_scan(_FRAME_LOCAL),
                         plat_far_segs=frozenset({THUNK_SEG}),
                         plat_farcalls=_PLAT,
                         far_dyn_sites={_FRAME_SITE_IP: []})
    assert "far-dispatch-no-evidence" in str(e.value)


def test_an_observed_target_with_no_contract_or_body_refuses_loud():
    """Evidence naming a target that is neither a contracted platform boundary
    nor a recovered far-return body cannot be composed -- the arm would have to
    be guessed.  The two ways that happens get their own names, mirroring the
    direct-call vocabulary: a KNOWN boundary whose contract is missing (never
    guess the arg cleanup), and game code with no recovered body yet (retried
    each fixpoint round)."""
    with pytest.raises(Refusal) as e:
        check_promotable(_scan(_FRAME_LOCAL),
                         plat_far_segs=frozenset({THUNK_SEG}),
                         plat_farcalls=_PLAT,
                         far_dyn_sites={_FRAME_SITE_IP: ["1234:5678"]})
    assert "far-dispatch-target-unpromoted" in str(e.value)

    # the same site pointed at an uncontracted slot of the DECLARED boundary
    with pytest.raises(Refusal) as e2:
        check_promotable(_scan(_FRAME_LOCAL),
                         plat_far_segs=frozenset({THUNK_SEG}),
                         plat_farcalls=_PLAT,
                         far_dyn_sites={_FRAME_SITE_IP:
                                        [f"{THUNK_SEG:04X}:0040"]})
    assert "far-dispatch-platform-contract-unknown" in str(e2.value)


def test_arms_that_disagree_on_the_stack_effect_refuse_loud():
    """Two observed targets with different pascal cleanups make the depth after
    the site arm-dependent.  Static depth is the premise every other analysis
    rests on, so this refuses rather than becoming dynamic."""
    plat = dict(_PLAT)
    plat[(THUNK_SEG, 0x0010)] = PlatformFarCall(argbytes=6, name="OTHER")
    with pytest.raises(Refusal) as e:
        check_promotable(_scan(_FRAME_LOCAL),
                         plat_far_segs=frozenset({THUNK_SEG}),
                         plat_farcalls=plat,
                         far_dyn_sites={_FRAME_SITE_IP:
                                        [FAR_KEY, f"{THUNK_SEG:04X}:0010"]})
    assert "far-dispatch-nonuniform-stack" in str(e.value)


def test_an_unwitnessed_pointer_at_runtime_raises_and_never_dispatches():
    """THE SOUNDNESS GUARD.  The evidence says the pointer was the thunk; the
    program hands it something else.  The body must raise a witness naming the
    site and the pointer -- not dispatch to it, not fall through, not pick the
    nearest arm.  (The API hook stays installed, so a fallback would be
    VISIBLE: the run would simply succeed.)"""
    hook, _src, _spec = _compile(_ES_DIRECT, _ES_SITE_IP, pkg_name="t_ifar_bad")
    ccpu, _mem = _fresh(_ES_DIRECT, hook, ptr=(0x0042, 0x4321))
    with pytest.raises(_Witness) as e:
        _run(ccpu)
    assert e.value.target == (0x4321, 0x0042)
    assert e.value.site == "2000:%04X" % _ES_SITE_IP
    assert "ax" in e.value.regs                # the witness carries the state


def test_a_target_that_escapes_the_stack_refuses_loud():
    """A recovered far body that does not return stack-balanced makes the
    continuation depth arm-dependent -- the same rule the near-dyn evidence
    gate applies to its targets."""
    far = {(0x3000, 0x0000): CalleeContract(
        name="func_3000_0000", inputs=(), outputs=(), exit_flags=frozenset(),
        ret_kind="far", ret_pop=4)}
    with pytest.raises(Refusal) as e:
        check_promotable(_scan(_FRAME_LOCAL),
                         plat_far_segs=frozenset({THUNK_SEG}),
                         plat_farcalls=_PLAT, far_callees=far,
                         far_dyn_sites={_FRAME_SITE_IP: ["3000:0000"]})
    assert "far-dispatch-target-sp-escape" in str(e.value)


# --------------------------------------------------------------------------
# the other arm kind: a RECOVERED far body, dispatched through the registry
# --------------------------------------------------------------------------

def test_a_recovered_far_body_arm_dispatches_through_the_registry():
    """The second composition rule falls out with no case analysis: an observed
    pointer that names a recovered FAR-return body becomes a registry dispatch
    (`_dyn`), with the same 4-byte far frame written literally and popped after
    -- exactly what a direct `call far` to that body emits."""
    seen = {}

    def dyn_exec(key, mem, plat, base, regs):
        seen["key"] = key
        seen["sp"] = regs["sp"]
        out = dict(regs)
        out["ax"] = 0x1234
        return out, {"fmask": 0, "flags": 0, "cost": 1}

    far = {(0x3000, 0x0000): CalleeContract(
        name="func_3000_0000", inputs=(), outputs=("ax",),
        exit_flags=frozenset(), ret_kind="far")}
    # the body writes the pointer it will call through, so point it at the
    # recovered far body instead of the thunk.
    code = bytearray(_FRAME_LOCAL)
    code[9:11] = (0x00, 0x00)                   # mov [bp-4], 0x0000
    code[14:16] = (0x00, 0x30)                  # mov [bp-2], 0x3000
    code = bytes(code)
    hook, rec_src, _spec = _compile(
        code, _FRAME_SITE_IP, targets=("3000:0000",),
        pkg_name="t_ifar_body", far_callees=far, dyn_stub=dyn_exec)
    assert '_dyn("3000:0000"' in rec_src
    assert "'cs': 0x3000" in rec_src            # the CALLEE's segment, not ours
    assert "sp = (sp + 4) & 0xFFFF" in rec_src  # the far frame is popped

    ccpu, _m = _fresh(code, hook)
    _run(ccpu)
    assert seen["key"] == "3000:0000"
    assert ccpu.s.ax == 0x1234
    # the far return frame was on the stack when the arm ran (sp below the args)
    assert seen["sp"] == (_SP0 - 2 - 4 - 4 - 4) & 0xFFFF
    assert ccpu.s.sp & 0xFFFF == (_SP0 + 2) & 0xFFFF   # balanced on return


# --------------------------------------------------------------------------
# the capture side: the same evidence channel, extended to far transfers
# --------------------------------------------------------------------------

def test_the_far_resolver_reads_the_pointer_the_site_will_take():
    """The producer-side resolver is the far counterpart of the near one and
    keeps the wire format ("SEG:OFF"); only the SOURCE of the segment differs
    (memory, not CS).  A register operand is not a far transfer at all."""
    mem = Memory()
    mem.ww(_ES, _PTR_OFF, SLOT)
    mem.ww(_ES, (_PTR_OFF + 2) & 0xFFFF, THUNK_SEG)
    st = CPUState(cs=0x2000, ip=0, ss=_SS, **_ENTRY_REGS)
    inst = decode_one(lambda o: _ES_DIRECT[o], _ES_SITE_IP)
    assert resolve_far_indirect_target(st, mem, inst) == FAR_KEY

    reg_form = decode_one(lambda o: bytes.fromhex("ffd3")[o], 0)   # call bx
    assert resolve_far_indirect_target(st, mem, reg_form) is None
