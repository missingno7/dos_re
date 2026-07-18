"""The ABI-recovered emitter (M3b, slice 1) -- public contract entry +
contract-proof shadow over the ONE mechanical core.

Consumes an ABI-contract proposal (dos_re.lift.contracts census output --
itself pure recovery-IR analysis) and generates, per function, a module
with TWO entrypoints around the single generated algorithmic core (the
mechanical CPUless recovered function -- no duplicate implementation):

* the PUBLIC ABI-recovered entry (``abi_CCCC_IIII``, or the recovered name
  when the naming table supplies one): semantic register parameters only;
  ``cs`` is bound statically (the function's own segment); the machine
  stack, where the core still uses it, is ONE explicitly-typed ``stack``
  parameter -- an (ss, sp) pair, classified HISTORICAL MEMORY VIEW, that
  disappears when the core re-emission (slice 2) turns local push/pop into
  Python locals.  Returns the caller-observed registers as a normal Python
  value (scalar / tuple / None) -- never the full written-register dict.

* the CONTRACT-PROOF SHADOW (``func_CCCC_IIII``, the mechanical name):
  preserves the exact mechanical ABI but XOR-PERTURBS every register output
  the census proved unobserved.  Substituting the shadow module for the
  mechanical one (sys.modules pre-registration) and replaying the canonical
  demo through the acceptance gate proves the narrowed contract END TO END:
  if anything -- a caller, a boundary register digest -- actually observes
  a "dropped" output, the perturbation surfaces and the oracle comparison
  fails loudly at that boundary.  XOR (not a constant) guarantees the value
  differs from the mechanical one whenever it is looked at.

Refusal-first: a proposal with refusals, or a shape this slice does not
support, raises -- never a silently degraded module.
"""
from __future__ import annotations

from dataclasses import dataclass

from .decode import (CALL, CALL_FAR, CALL_IND, HLT, INT, IRET, JCC, JMP,
                     JMP_FAR, JMP_IND, RET, RETF, SEQ)
from .cpuless import SEGS
from .contracts import _STRING_PAIRS
from .emit_cpuless import (CalleeContract, Refusal, _check_flag_liveins,
                           _DISPATCH_ITER_CAP, _FLAG_BITS, _INT_REGS,
                           _JCC_EXPR, _patched_read, _r8_write, _reg16,
                           _rm_read, _rm_write_lines, _translate)

#: XOR perturbation for proven-unobserved outputs: guaranteed to differ
#: from the mechanical value if anything observes it.
POISON_XOR = 0xA5A5

#: DOS/BIOS SERVICE interrupts modelled as an explicit platform effect
#: (plat.intr) -- mirrors dos_re.lift.cpuless.register_effects' PLATFORM_INT.
#: A game-vectored INT (3/60/61) is a call into recovered handler code
#: (needs the _ivec dispatch) and stays mechanical this tier; INT 20h
#: terminates; anything else is an unmodelled installed vector.
PLATFORM_INT = frozenset({0x21, 0x10, 0x11, 0x12, 0x13, 0x15, 0x16, 0x1A,
                          0x2F, 0x33, 0x67})
#: the flag bits an INT service reproduces (CF PF AF ZF SF OF IF DF).
_INT_FMASK = 0xED5


@dataclass(frozen=True)
class CoreContract:
    """One already-emitted ABI core, as a caller composing it needs it.

    ``inputs`` are the callee's semantic parameters (sorted; no sp/ss/cs);
    ``returns`` its observed register returns; ``df_livein`` whether it
    takes the caller's DF; ``exit_flags`` the flags it definitely defines on
    every exit (feeds the caller's flag-liveness).  A ``needs_plat`` core
    also takes the platform interface and the absolute virtual-time base
    (``plat``, ``_base``) so its port I/O reaches the device with correct
    timing; a composed call threads those through.  The composed call always
    takes back ``(_o, _c)`` -- the SAME two-tuple the mechanical composition
    consumes, so cost and flags merge identically.  (flags_livein cores stay
    mechanical this tier.)"""
    key: str
    stem: str
    inputs: tuple
    returns: tuple
    df_livein: bool
    exit_flags: frozenset
    needs_plat: bool = False
    flags_livein: bool = False


def _stem(key: str) -> str:
    cs, ip = key.split(":")
    return f"{int(cs, 16):04x}_{int(ip, 16):04x}"


def emit_abi_module(key: str, proposal: dict, *, import_base: str,
                    name: str | None = None) -> str:
    """Generate the ABI-recovered module source for one census proposal.

    ``import_base``: the package holding the mechanical recovered modules
    (e.g. ``lemmings.recovered``).  ``name``: optional recovered name for
    the public entry (provenance stays the address)."""
    if proposal.get("refusals"):
        raise Refusal("contract-not-promotable: "
                      + ",".join(r["reason"] for r in proposal["refusals"]))
    stem = _stem(key)
    cs = int(key.split(":")[0], 16)
    mech = f"func_{stem}"
    public = name or f"abi_{stem}"
    if not public.isidentifier():
        raise Refusal(f"recovered-name-not-identifier: {public!r}")

    params = [p["reg"] for p in proposal["params"]]
    returns = list(proposal["returns"])
    dropped = list(proposal["dropped_outputs"])
    machine = set(proposal["machine_private"])
    unknown_machine = machine - {"sp", "ss", "cs", "bp"}
    if unknown_machine:
        raise Refusal("unsupported-machine-private: "
                      + ",".join(sorted(unknown_machine)))
    if "bp" in machine:
        # framed stack args (slice 2 material: no framed functions in a
        # register-convention corpus; refuse rather than mis-emit)
        raise Refusal("stack-args-not-yet-emitted")
    needs_stack = bool(machine & {"sp", "ss"})

    sig = ["mem", "plat=None"]
    body_kwargs = [f"{r}={r}" for r in sorted(params)]
    kw = ["*"] if params or needs_stack else []
    if needs_stack:
        kw.append("stack=(0, 0)")
        body_kwargs += ["ss=stack[0]", "sp=stack[1]"]
    kw += [f"{r}=0" for r in sorted(params)]
    if "cs" in machine:
        body_kwargs.append(f"cs=0x{cs:04X}")
    sig_line = ", ".join(sig + kw)
    call_kw = ", ".join(["**_compat"] + body_kwargs)

    if not returns:
        ret_doc, ret_line = "None", "    return None"
    elif len(returns) == 1:
        ret_doc = returns[0]
        ret_line = f"    return _o['{returns[0]}']"
    else:
        ret_doc = "(" + ", ".join(returns) + ")"
        ret_line = ("    return ("
                    + ", ".join(f"_o['{r}']" for r in returns) + ")")

    prov = f"{name} [{key}]" if name else f"[{key}]"
    stack_doc = ("\n    ``stack``: the (ss, sp) machine-stack view "
                 "(historical memory view; slice-2 removes it)."
                 if needs_stack else "")
    lines = [
        '"""AUTOGENERATED by dos_re.lift.emit_abi -- ABI-recovered contract',
        f'for {key} (M3b slice 1: dual entrypoints over the ONE mechanical',
        'core).  DO NOT hand-edit; regenerate.',
        '',
        f'Public contract: {public}({sig_line}) -> {ret_doc}',
        f'Shadow {mech} preserves the mechanical ABI and XOR-perturbs the',
        f'proven-unobserved outputs {tuple(dropped)!r} -- the end-to-end',
        'contract proof when substituted into the acceptance demo.',
        '"""',
        '',
        f'from {import_base}.{mech} import {mech} as _core',
        '',
        f'_DROPPED = {tuple(dropped)!r}',
        f'_POISON_XOR = 0x{POISON_XOR:04X}',
        '',
        '',
        f'def {public}({sig_line}, **_compat):',
        f'    """Public ABI-recovered entry {prov}: semantic contract only.',
        f'    Returns {ret_doc}.{stack_doc}',
        '    ``_compat``: private verification channel (flags word, virtual',
        '    time base) -- not part of the recovered API."""',
        '    _args = (mem,) if plat is None else (mem, plat)',
        f'    _o, _c = _core(*_args, {call_kw})',
        ret_line,
        '',
        '',
        f'def {mech}(mem, *args, **kw):',
        '    """Contract-proof shadow: exact mechanical ABI; the census-',
        '    dropped outputs are XOR-perturbed so any observation anywhere',
        '    diverges the oracle comparison loudly."""',
        '    _o, _c = _core(mem, *args, **kw)',
        '    _o = dict(_o)',
        '    for _r in _DROPPED:',
        '        _o[_r] = (_o[_r] ^ _POISON_XOR) & 0xFFFF',
        '    return _o, _c',
        '',
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# slice 2: the DE-STACKED algorithmic core (leaf tier)
#
# The mechanical core writes its local push/pop traffic into guest memory
# (observable residue) and threads sp/ss through every contract.  The
# ABI-recovered core keeps those locals in a Python VIRTUAL STACK: the
# machine stack disappears from the contract (no sp/ss parameters, no stack
# memory writes), which is the first real memory-dependency reduction on
# the road to the memoryless stage.  Everything else -- instruction
# semantics, flag bookkeeping, virtual-time cost -- reuses the mechanical
# emitter's own translator, so the compat channel stays bit-identical and
# the seeded differential (dos_re.lift.abi_diff) can compare the two forms
# exactly.

#: virtual-stack slot deltas for the stack family this tier virtualises.
_PUSH_R16 = range(0x50, 0x58)
_POP_R16 = range(0x58, 0x60)
_PUSH_SEG = (0x06, 0x0E, 0x16, 0x1E)
_POP_SEG = (0x07, 0x1F)                    # pop ss (0x17) refuses
_PORT_IO = (0xE4, 0xE5, 0xE6, 0xE7, 0xEC, 0xED, 0xEE, 0xEF)


def _ea_seg_of(i) -> str | None:
    """The segment a memory ModRM operand addresses, or None for mod=3 /
    no ModRM.  Mirrors the emitter's _ea default rule (bp-based -> ss)."""
    if i.modrm is None or i.mod == 3:
        return None
    for p in i.prefixes:
        if p in (0x26, 0x2E, 0x36, 0x3E):
            return SEGS[(p >> 3) & 3]
    if i.mod == 0 and i.rm == 6:
        return "ds"
    return "ss" if i.rm in (2, 3, 6) else "ds"


def _vslot_delta(i) -> int | None:
    """Virtual-stack slot delta for a stack-family instruction this tier
    supports, or None for a non-stack instruction."""
    op = i.op
    if op in _PUSH_R16 or op in _PUSH_SEG or op in (0x68, 0x6A) \
            or (op == 0xFF and i.reg == 6):
        return +1
    if op in _POP_R16 or op in _POP_SEG or (op == 0x8F and i.reg == 0):
        return -1
    if op == 0x60:                          # pusha
        return +8
    if op == 0x61:                          # popa
        return -8
    return None


def check_composable(scan, *, callees=None, boundary_addrs=frozenset(),
                     dispatch_addrs=frozenset()) -> dict[int, int]:
    """Prove one function's machine-stack use is PURELY LOCAL so its stack
    can become Python locals -- allowing direct NEAR calls to already-
    emitted ABI cores (``callees``: target ip -> :class:`CoreContract`),
    which compose at the recovered level with NO return-address mechanics.
    Returns the per-ip entry depth map (slots).

    Refusal-first; each reason is a capability name:
      * leaf-only: far/indirect/interrupt transfers compose in later tiers;
        a near call to a target with NO known ABI contract also refuses
        here (its callee is not yet an ABI core);
      * platform-port-io / flags-word-stack / sp-adjust / frame-or-sp-data /
        ss-write / ss-value-as-data: shapes whose stack or platform coupling
        this tier keeps mechanical;
      * stack-addressed-memory: an ss-segment effective address -- the
        function reads/writes its stack as MEMORY, so the stack must stay
        memory-backed;
      * touches-return-address: a pop below entry depth;
      * unbalanced-stack / depth-join-mismatch: the virtual stack cannot be
        proven consistent;
      * observer/alt-entry: park- or dispatch-carrying functions stay
        mechanical (their frames are externally observable)."""
    from .cpuless import register_effects
    from .contracts import ss_is_data_segment

    callees = callees or {}
    # ss used PURELY as a data-segment selector (no push/pop traffic at all)
    # is an ordinary segment input, not the stack carrier -- the small-model
    # `mov ss:[0x0006], ax` idiom.  Its memory operands are then no more
    # "stack-addressed" than a ds: access.  A function doing BOTH keeps the
    # refusal: there the stack and the data genuinely share one segment.
    ss_data = ss_is_data_segment(scan)
    cs_addrs = {ip for ip in scan.insts}
    if frozenset(boundary_addrs) & cs_addrs:
        raise Refusal("observer-in-function")
    if frozenset(dispatch_addrs) & (cs_addrs - {scan.entry}):
        raise Refusal("alt-entry-in-function")
    for i in scan.insts.values():
        k = i.kind
        if k == CALL and i.target is not None and i.target in callees:
            continue                          # composed near call (delta 0)
        if k == INT:
            if i.int_no in PLATFORM_INT:
                continue                      # a DOS/BIOS service (plat.intr)
            if i.int_no in (3, 0x60, 0x61):
                raise Refusal("game-vectored-int")
            if i.int_no == 0x20:
                raise Refusal("program-terminate")
            raise Refusal("vectored-int-call")
        if k in (CALL, CALL_FAR, CALL_IND, JMP_IND, JMP_FAR, HLT):
            raise Refusal(f"leaf-only:{k}")
        if k == IRET:
            raise Refusal("iret-contract")
        if i.op in (0x9C, 0x9D):
            raise Refusal("flags-word-stack")
        if i.op in (0xC8, 0xC9):
            raise Refusal("frame-or-sp-data")
        if i.op == 0x17 or (i.op == 0x8E and (i.reg & 3) == 2):
            raise Refusal("ss-write")
        if i.op == 0x16 or (i.op == 0x8C and (i.reg & 3) == 2):
            # push ss / mov r,ss: the stack SEGMENT VALUE flows as data
            # (push ss; pop es addressing idiom) -- a de-stacked core has
            # no ss; slice 3 promotes it to a semantic segment parameter.
            raise Refusal("ss-value-as-data")
        if i.op in (0x81, 0x83) and i.mod == 3 and i.rm == 4 \
                and i.reg in (0, 5):
            raise Refusal("sp-adjust")
        e = register_effects(i)
        if e.frame_establish or e.frame_restore or e.frame_restore_to_base:
            raise Refusal("frame-or-sp-data")
        if _vslot_delta(i) is None and ("sp" in e.reads or "sp" in e.writes) \
                and k not in (RET, RETF):
            raise Refusal("frame-or-sp-data")   # sp as general data
        if not ss_data:
            if _ea_seg_of(i) == "ss":
                raise Refusal("stack-addressed-memory")
            if i.op in _STRING_PAIRS or i.op == 0xD7 \
                    or 0xA0 <= i.op <= 0xA3:      # moffs has no ModRM
                for p in i.prefixes:
                    if p == 0x36:
                        raise Refusal("stack-addressed-memory")

    # virtual-stack depth walk: every ip one provable slot depth
    depth: dict[int, int] = {scan.entry: 0}
    work = [scan.entry]
    while work:
        ip = work.pop()
        i = scan.insts[ip]
        d = depth[ip]
        delta = _vslot_delta(i) or 0
        nd = d + delta
        if nd < 0:
            raise Refusal("touches-return-address")
        if i.kind in (RET, RETF):
            if nd != 0:
                raise Refusal("unbalanced-stack")
            continue
        succs = []
        if i.kind in (SEQ, CALL, INT):
            # SEQ, a composed near call (delta 0), and a serviced INT all
            # fall through.  INT must be here: without it the walk stopped at
            # the first interrupt, leaving the rest of the body un-depthed --
            # which both hid a possible post-INT imbalance from the gate and
            # left the emitter without a slot index for those instructions.
            succs = [i.next_ip]
        elif i.kind == JCC:
            succs = [i.next_ip, i.target]
        elif i.kind == JMP:
            succs = [i.target]
        for s in succs:
            if s is None or s not in scan.insts:
                raise Refusal("leaves-region")
            if s in depth:
                if depth[s] != nd:
                    raise Refusal("depth-join-mismatch")
            else:
                depth[s] = nd
                work.append(s)
    return depth


def _abi_needs_plat(scan, callees) -> bool:
    """A core needs the platform interface if it does port I/O, a platform
    INT, or composes a callee that needs it."""
    for i in scan.insts.values():
        if i.op in _PORT_IO:
            return True
        if i.kind == INT and i.int_no in PLATFORM_INT:
            return True
        if i.kind == CALL and i.target in callees \
                and callees[i.target].needs_plat:
            return True
    return False


def _emit_int(i, blk, count) -> None:
    """Emit a DOS/BIOS service INT as a platform effect (``plat.intr``) at
    the same absolute virtual time the mechanical emitter passes: the whole
    register bundle + composed flags word go in, the returned bundle + flags
    come back.  Byte-identical to emit_cpuless's INT branch."""
    off = count - 1
    cost = "_base + _cost" if off == 0 else f"_base + _cost + {off}"
    fw = " | ".join(f"(0x{_FLAG_BITS[f]:X} if {f} else 0)"
                    for f in ("cf", "pf", "af", "zf", "sf", "of", "df",
                              "intf"))
    regd = ", ".join("'%s': %s" % (r, r) for r in _INT_REGS)
    blk.append("_ib = {%s, '_flags': (%s)}" % (regd, fw))
    blk.append(f"_ir = plat.intr(0x{i.int_no:02X}, _ib, {cost})")
    for r in _INT_REGS:
        blk.append(f"{r} = _ir['{r}']")
    blk.append("_if = _ir['flags']")
    for fname, fbit in (("cf", 0x01), ("pf", 0x04), ("af", 0x10),
                        ("zf", 0x40), ("sf", 0x80), ("of", 0x800),
                        ("intf", 0x200), ("df", 0x400)):
        blk.append(f"{fname} = (_if & 0x{fbit:X}) != 0")
    blk.append(f"_fmask |= 0x{_INT_FMASK:X}")


def _emit_portio(i, blk, count) -> None:
    """Emit a port I/O instruction as a platform effect, at the same
    absolute virtual time the mechanical emitter passes (``_base + _cost +
    offset-in-block``)."""
    off = count - 1
    cost = "_base + _cost" if off == 0 else f"_base + _cost + {off}"
    width = 2 if (i.op & 1) else 1
    if i.op in (0xE4, 0xE5):              # in acc, imm8
        port, read = f"0x{(i.imm or 0) & 0xFF:X}", True
    elif i.op in (0xEC, 0xED):            # in acc, dx
        port, read = "dx", True
    elif i.op in (0xE6, 0xE7):            # out imm8, acc
        port, read = f"0x{(i.imm or 0) & 0xFF:X}", False
    else:                                 # out dx, acc
        port, read = "dx", False
    if read:
        blk.append(f"_pv = plat.inp({port}, {width}, {cost})")
        blk.append("ax = _pv" if width == 2 else _r8_write(0, "_pv"))
    else:
        val = "ax" if width == 2 else "(ax & 0xFF)"
        blk.append(f"plat.outp({port}, {val}, {width}, {cost})")


def check_destackable(scan, *, boundary_addrs=frozenset(),
                      dispatch_addrs=frozenset()) -> dict[int, int]:
    """Call-free destackability (:func:`check_composable` with no composed
    callees): every call is leaf-only-refused.  Kept as the leaf-tier name."""
    return check_composable(scan, callees=None, boundary_addrs=boundary_addrs,
                            dispatch_addrs=dispatch_addrs)


def _slot(n: int) -> str:
    return f"_slot_{n}"


def _emit_vstack(i, blk, cs, depth: int) -> bool:
    """Emit a supported stack-family instruction as PLAIN LOCALS.

    ``depth`` is the proven virtual-stack depth (in slots) on entry to this
    instruction -- :func:`check_composable` establishes a UNIQUE depth per ip,
    so every push/pop resolves statically to a numbered slot variable and no
    stack object survives in the generated body.  A push at depth ``d`` writes
    slot ``d``; a pop at depth ``d`` reads slot ``d-1``.  Masking mirrors the
    mechanical ``mem.ww``.  Returns False for non-stack instructions (delegate
    to the shared translator)."""
    op = i.op
    if op in _PUSH_R16:
        blk.append(f"{_slot(depth)} = {_reg16(op & 7)} & 0xFFFF")
        return True
    if op in _POP_R16:
        blk.append(f"{_reg16(op & 7)} = {_slot(depth - 1)}")
        return True
    if op in _PUSH_SEG:
        blk.append(f"{_slot(depth)} = {SEGS[(op >> 3) & 3]} & 0xFFFF")
        return True
    if op in _POP_SEG:
        blk.append(f"{SEGS[(op >> 3) & 3]} = {_slot(depth - 1)}")
        return True
    if op in (0x68, 0x6A):
        pr = _patched_read(i, cs)
        if pr is not None:
            if op == 0x6A:
                blk.append(f"_pi = {pr}")
                blk.append("_pi = (_pi | 0xFF00) if (_pi & 0x80) else _pi")
                blk.append(f"{_slot(depth)} = _pi & 0xFFFF")
            else:
                blk.append(f"{_slot(depth)} = ({pr}) & 0xFFFF")
            return True
        imm = (i.imm or 0) & 0xFFFF
        if op == 0x6A and imm & 0x80:
            imm |= 0xFF00
        blk.append(f"{_slot(depth)} = 0x{imm & 0xFFFF:X}")
        return True
    if op == 0xFF and i.reg == 6:
        blk.append(f"{_slot(depth)} = ({_rm_read(i, True)}) & 0xFFFF")
        return True
    if op == 0x8F and i.reg == 0:
        blk.append(f"_t = {_slot(depth - 1)}")
        blk.extend(_rm_write_lines(i, True, "_t"))
        return True
    if op == 0x60:                          # pusha (0 placeholder for sp)
        for k, r in enumerate(("ax", "cx", "dx", "bx")):
            blk.append(f"{_slot(depth + k)} = {r} & 0xFFFF")
        blk.append(f"{_slot(depth + 4)} = 0")
        for k, r in enumerate(("bp", "si", "di"), start=5):
            blk.append(f"{_slot(depth + k)} = {r} & 0xFFFF")
        return True
    if op == 0x61:                          # popa (discards the saved sp)
        for k, r in enumerate(("di", "si", "bp"), start=1):
            blk.append(f"{r} = {_slot(depth - k)}")
        for k, r in enumerate(("bx", "dx", "cx", "ax"), start=5):
            blk.append(f"{r} = {_slot(depth - k)}")
        return True
    return False


def _core_alias(stem: str) -> str:
    return f"_core_{stem}"


def emit_abi_core(scan, proposal: dict, key: str, *,
                  name: str | None = None,
                  callees=None, abi_base: str = "",
                  boundary_addrs=frozenset(),
                  dispatch_addrs=frozenset()) -> tuple[str, CoreContract]:
    """Generate the TRUE ABI-recovered core module for one composable
    function: semantic signature (no sp/ss, no CPU bundle), virtual local
    stack, direct NEAR calls to already-emitted ABI cores (``callees``:
    target ip -> :class:`CoreContract`) with NO return-address mechanics,
    observed-only returns + the bit-identical compat channel; a public
    entry over the one core.

    Returns ``(source, CoreContract)`` -- the module text and this
    function's own contract, so a bottom-up driver feeds it to later
    callers.  Refuses (named capability) anything outside this tier -- the
    refusal census IS the next-tier work list."""
    callees = callees or {}
    if proposal.get("refusals"):
        raise Refusal("contract-not-promotable")
    vdepth = check_composable(scan, callees=callees,
                              boundary_addrs=boundary_addrs,
                              dispatch_addrs=dispatch_addrs)
    # flag liveness with the composed callees' exit-flag contributions, so a
    # `call G; jnz` idiom is analysed exactly as the mechanical emitter does.
    cc = {ip: CalleeContract(
              name=_core_alias(c.stem), inputs=c.inputs, outputs=c.returns,
              exit_flags=c.exit_flags, needs_plat=c.needs_plat, ret_kind="near",
              df_livein=c.df_livein, flags_livein=c.flags_livein)
          for ip, c in callees.items()}
    exit_flags, df_livein, fl_needed = _check_flag_liveins(scan, callees=cc)
    # flags_livein rule mirrors emit_cpuless.check_promotable: a serviced INT
    # (plat.intr) reads the caller's FLAGS word (its IF/DF bits, which no
    # arithmetic defines), and the property is transitive through a composed
    # callee.  The other mechanical triggers (dynamic/ISR/far-plat/pushf) are
    # already refused, so they cannot arise here.
    flags_livein = (fl_needed
                    or any(i.kind == INT for i in scan.insts.values())
                    or any(i.kind == CALL and i.target in callees
                           and callees[i.target].flags_livein
                           for i in scan.insts.values()))
    needs_plat = _abi_needs_plat(scan, callees)

    cs = int(key.split(":")[0], 16)
    stem = _stem(key)
    public = name or f"abi_{stem}"
    if not public.isidentifier():
        raise Refusal(f"recovered-name-not-identifier: {public!r}")
    params = sorted(p["reg"] for p in proposal["params"])
    returns = list(proposal["returns"])
    machine = set(proposal["machine_private"])
    if machine - {"sp", "ss", "cs"}:
        raise Refusal("unsupported-machine-private: "
                      + ",".join(sorted(machine - {"sp", "ss", "cs"})))

    leaders = sorted(set(scan.block_leaders()))
    bb_of = {ip: n for n, ip in enumerate(leaders)}
    # the ABI callees this body actually calls -> import aliases
    called = sorted({callees[i.target].stem for i in scan.insts.values()
                     if i.kind == CALL and i.target in callees},
                    key=str)

    L: list[str] = []
    A = L.append
    prov = f"{name} [{key}]" if name else f"[{key}]"
    A('"""AUTOGENERATED by dos_re.lift.emit_abi -- DE-STACKED ABI-recovered')
    A(f'core for {key} (M3b slice 2/3).  DO NOT hand-edit; regenerate.')
    A('')
    A('The machine stack is a Python virtual stack: no sp/ss parameters, no')
    A('stack memory writes -- the historical memory image is touched only by')
    A("the function's SEMANTIC reads/writes.  Near calls go DIRECTLY to the")
    A('callee ABI cores (no return-address mechanics).  The compat channel')
    A('(exit flags + virtual-time cost) is bit-identical to the mechanical')
    A('core; dos_re.lift.abi_diff compares the two forms exactly.')
    A('"""')
    for st in called:
        base = f"{abi_base}." if abi_base else ""
        A(f"from {base}core_{st} import _abi_core as {_core_alias(st)}")
    A('')
    A("_PARITY = tuple((1 - bin(v).count('1') % 2) == 1 for v in range(256))")
    A('')
    # GENERATED CONTRACT METADATA (not API): the anonymous parameters' proven
    # roles, so the next stage gets pointer/segment structure explicitly
    # instead of re-deriving it from register arithmetic.  Roles:
    #   "segment"      a segment binding other parameters address through
    #   "pointer"      an offset used ONLY to address memory, paired with a
    #                  segment parameter (a NEAR pointer into that segment)
    #   "value"        a scalar
    #   "mixed"        used both as an address and as a value -- NOT
    #                  classified as a pointer (refusal-first)
    kinds = {p["reg"]: p["kind"] for p in proposal["params"]}
    seg_of = {}
    for s, r, _n in proposal["pointer_pairs"]:
        if kinds.get(r) == "pointer" and s in kinds:
            seg_of.setdefault(r, set()).add(s)
    A("#: generated contract metadata -- roles, not API (see emit_abi).")
    A("_CONTRACT = {")
    A(f"    'key': {key!r},")
    A("    'params': (")
    for k, r in enumerate(params):
        segs = sorted(seg_of.get(r, ()))
        seg_txt = (f", 'in_segment': 'arg_{params.index(segs[0])}'"
                   if len(segs) == 1 and segs[0] in params else "")
        A(f"        {{'name': 'arg_{k}', 'role': {kinds.get(r, 'value')!r}, "
          f"'historical': {r!r}{seg_txt}}},")
    A("    ),")
    A("    'returns': (" + ", ".join(
        f"{{'name': 'ret_{k}', 'historical': {r!r}}}"
        for k, r in enumerate(returns)) + ("," if len(returns) == 1 else "")
      + "),")
    A("}")
    A('')
    A('')
    # PUBLIC CONTRACT SURFACE: semantic inputs are ANONYMOUS POSITIONAL
    # parameters in contract order -- no caller needs to know a register
    # identity.  The compat/platform channel stays keyword-only and private.
    # Register-named locals survive INSIDE the body (a pure alpha-rename with
    # no structural content; see docs/abi_end_state.md).
    pos = [f"arg_{k}" for k in range(len(params))]
    compat_argl = (["_base=0"] if needs_plat else []) \
        + (["_df=0"] if df_livein else []) \
        + (["_flags_in=2"] if flags_livein else [])
    _head = "mem, plat" if needs_plat else "mem"
    _sig = ", ".join([_head] + [f"{p}=0" for p in pos]
                     + (["*"] + compat_argl if compat_argl else []))
    A(f"def _abi_core({_sig}):")
    body: list[str] = []
    B = body.append
    for p, r in zip(pos, params):
        B(f"{r} = {p}")
    B("_cost = 0")
    B("cf = pf = af = zf = sf = of = df = intf = False")
    if flags_livein:
        # every flag local starts MACHINE-CORRECT from the caller word, so a
        # serviced INT sees the caller's IF/DF (nothing is ever undefined).
        for fname, fbit in sorted(_FLAG_BITS.items()):
            B(f"{fname} = (_flags_in & 0x{fbit:X}) != 0")
    if df_livein:
        B("df = _df != 0    # caller DF (hidden compat input)")
    B("_fmask = 0")
    B(f"cs = 0x{cs:04X}")
    B(f"bb = {bb_of[scan.entry]}")
    B("_iters = 0")
    B("while True:")
    B("    _iters += 1")
    B(f"    if _iters > {_DISPATCH_ITER_CAP}:")
    # the same wording as the mechanical emitter, so a spin-wait raises
    # IDENTICALLY on both sides of the differential
    B(f"        raise RuntimeError('CPUless dispatch spin in {key} "
      f"(block %d, cost %d): loop exceeded {_DISPATCH_ITER_CAP} iterations "
      f"-- an unbounded wait (interrupt-updated flag, or a wrong port after "
      f"a state divergence)' % (bb, _cost))")
    for n, leader in enumerate(leaders):
        blk: list[str] = []
        flag_written: set[str] = set()
        ip = leader
        count = 0
        terminated = False

        def _flush_flags():
            if flag_written:
                bits = " | ".join(f"0x{_FLAG_BITS[f]:X}"
                                  for f in sorted(flag_written))
                blk.append(f"_fmask |= {bits}")

        while ip in scan.insts:
            i = scan.insts[ip]
            count += 1
            if i.kind in (RET, RETF):
                blk.append(f"_cost += {count}")
                _flush_flags()
                blk.append("break")
                terminated = True
                break
            if i.kind == JMP:
                blk.append(f"_cost += {count}")
                _flush_flags()
                blk.append(f"bb = {bb_of[i.target]}")
                blk.append("continue")
                terminated = True
                break
            if i.kind == JCC:
                blk.append(f"_cost += {count}")
                _flush_flags()
                if i.op in (0xE0, 0xE1, 0xE2):
                    blk.append("cx = (cx - 1) & 0xFFFF")
                    cond = {0xE0: "cx != 0 and not zf",
                            0xE1: "cx != 0 and zf",
                            0xE2: "cx != 0"}[i.op]
                elif i.op == 0xE3:
                    cond = "cx == 0"
                else:
                    cond = _JCC_EXPR[i.op]
                blk.append(f"if {cond}:")
                blk.append(f"    bb = {bb_of[i.target]}")
                blk.append("    continue")
                blk.append(f"bb = {bb_of[i.next_ip]}")
                blk.append("continue")
                terminated = True
                break
            if i.kind == CALL and i.target in callees:
                _emit_composed_call(blk, callees[i.target], count)
            elif i.kind == INT:
                _emit_int(i, blk, count)
            elif i.op in _PORT_IO:
                _emit_portio(i, blk, count)
            elif not _emit_vstack(i, blk, cs, vdepth[ip]):
                _translate(i, blk, flag_written, cs)
            nxt = i.next_ip
            if nxt in bb_of and nxt != ip:
                blk.append(f"_cost += {count}")
                _flush_flags()
                blk.append(f"bb = {bb_of[nxt]}")
                blk.append("continue")
                terminated = True
                break
            ip = nxt
        if not terminated:
            raise Refusal("block-falls-off-region")
        B(f"    if bb == {n}:  # {cs:04X}:{leader:04X}")
        for ln in blk:
            B(f"        {ln}")
    B("    raise AssertionError('unreachable dispatch')")
    fw = " | ".join(f"(0x{_FLAG_BITS[f]:X} if {f} else 0)"
                    for f in ("cf", "pf", "af", "zf", "sf", "of",
                              "df", "intf"))
    B(f"_flags = ({fw}) & _fmask")
    # results are POSITIONAL, in contract order -- no call site indexes a
    # result by register name.
    out_tup = ", ".join(f"{r} & 0xFFFF" for r in returns)
    B(f"return ({out_tup}{',' if len(returns) == 1 else ''}), "
      "{'flags': _flags, 'fmask': _fmask, 'cost': _cost}")
    for ln in body:
        A(f"    {ln}")
    A('')
    A('')
    if not returns:
        ret_doc, ret_line = "None", "    return None"
    elif len(returns) == 1:
        ret_doc = "one value"
        ret_line = "    return _o[0]"
    else:
        ret_doc = f"{len(returns)} values"
        ret_line = "    return _o"
    # the public entry hides the compat channel; a needs_plat core still
    # takes plat (the caller of the ABI graph supplies it) and starts the
    # virtual clock at 0.
    pub_head = "mem, plat" if needs_plat else "mem"
    pub_compat = [a for a in compat_argl if not a.startswith("_base")]
    psig = ", ".join([pub_head] + [f"{p}=0" for p in pos]
                     + (["*"] + pub_compat if pub_compat else []))
    fwd = ", ".join(pos + [f"{a.split('=')[0]}={a.split('=')[0]}"
                           for a in pub_compat])
    core_call = "mem, plat" if needs_plat else "mem"
    A(f"def {public}({psig}):")
    A(f'    """Public ABI-recovered entry {prov}: semantic contract only.')
    A(f'    Parameters are positional and anonymous; returns {ret_doc}.')
    A(f'    Historical roles, in order: {", ".join(params) or "(none)"}')
    A(f'    -> {", ".join(returns) or "(none)"}."""')
    A(f"    _o, _c = _abi_core({core_call}{', ' + fwd if fwd else ''})")
    A(ret_line)
    A('')
    contract = CoreContract(key=key, stem=stem, inputs=tuple(params),
                            returns=tuple(returns), df_livein=df_livein,
                            exit_flags=frozenset(exit_flags),
                            needs_plat=needs_plat, flags_livein=flags_livein)
    return "\n".join(L), contract


def _emit_composed_call(blk, c: CoreContract, count) -> None:
    """A recovered-level near call: invoke the callee ABI core DIRECTLY
    (its _abi_core, imported as _core_<stem>), unpack its observed returns,
    merge its exit flags through the compat mask, and accumulate its
    virtual-time cost.  NO return-address bytes, NO sp traffic -- the whole
    point of the de-stacked contract.  A needs_plat callee also receives
    ``plat`` and the absolute virtual time at the call site (``_base +
    _cost + offset-in-block``), exactly as the mechanical composition."""
    kw = ["mem, plat"] if c.needs_plat else ["mem"]
    # semantic inputs go POSITIONALLY, in the callee's contract order
    kw += [str(r) for r in c.inputs]
    if c.needs_plat:
        # the callee's _base is its ENTRY virtual time -- reached AFTER this
        # call executes, so it INCLUDES the call (count already counts this
        # instruction).  Mirrors emit_cpuless (upstream fix c3686c4: count-1
        # anchored the callee's plat effects one instruction early).
        off = count
        kw.append("_base=_base + _cost" if off == 0
                  else f"_base=_base + _cost + {off}")
    if c.df_livein:
        kw.append("_df=(1 if df else 0)")
    if c.flags_livein:
        # reconstruct the callee's full FLAGS word: bits this function has
        # defined (in _fmask) from the live locals, the rest from the
        # caller's own word -- byte-identical to the mechanical composition.
        fw = " | ".join(f"(0x{_FLAG_BITS[f]:X} if {f} else 0)"
                        for f in ("cf", "pf", "af", "zf", "sf", "of",
                                  "df", "intf"))
        kw.append(f"_flags_in=((_flags_in & ~_fmask) | (({fw}) & _fmask))")
    blk.append(f"_o, _c = {_core_alias(c.stem)}({', '.join(kw)})")
    for n, r in enumerate(c.returns):
        blk.append(f"{r} = _o[{n}]")
    blk.append("_gm = _c['fmask']")
    blk.append("if _gm:")
    blk.append("    _gf = _c['flags']")
    for fname, fbit in (("cf", 0x01), ("pf", 0x04), ("af", 0x10),
                        ("zf", 0x40), ("sf", 0x80), ("of", 0x800),
                        ("intf", 0x200), ("df", 0x400)):
        blk.append(f"    if _gm & 0x{fbit:X}: "
                   f"{fname} = (_gf & 0x{fbit:X}) != 0")
    blk.append("    _fmask |= _gm")
    blk.append("_cost += _c['cost']")


def emit_shadow_loader(keys: list[str], *, abi_base: str,
                       import_base: str) -> str:
    """The generated loader that substitutes the contract-proof shadows for
    their mechanical modules.

    Two seams, both required:

    * ``sys.modules`` aliasing (the native-override loader's seam) routes
      every LATER import -- unimported callers, and the dispatch registry's
      lazy ``importlib`` resolution -- through the shadow;
    * RETRO-PATCHING covers the imports that already happened: mechanical
      modules bind their callees at module level (``from ...func_X import
      func_X``), and importing one shadow transitively imports mechanical
      callers of OTHER shadowed functions before those shadows register.
      Every already-materialised namespace -- modules still in sys.modules
      under the recovered package, plus each shadowed core's own module
      globals (reached via ``_core.__globals__``; the module object leaves
      sys.modules when aliased but its function bodies still execute
      against it) -- gets its stale bindings rebound to the shadow.

    A shadowed core's binding of its OWN name is deliberately left alone: a
    self-recursive core calling its own shadow would XOR-perturb twice and
    cancel the poison (the self-edge composes conservatively anyway)."""
    stems = ", ".join(f'"{_stem(k)}"' for k in sorted(keys))
    return "\n".join([
        '"""AUTOGENERATED by dos_re.lift.emit_abi -- contract-proof shadow',
        'loader (M3b).  DO NOT hand-edit; regenerate."""',
        '',
        'import importlib',
        'import sys',
        '',
        f'STEMS = ({stems},)',
        '',
        '',
        'def install_shadows(stems=STEMS):',
        '    """Substitute every contract-proof shadow for its mechanical',
        '    module: sys.modules alias for future imports + retro-patch of',
        '    every already-materialised binding.  Idempotent."""',
        '    shadows = {}',
        '    for s in stems:',
        f'        shadows[s] = importlib.import_module("{abi_base}.abi_" + s)',
        '    for s, mod in shadows.items():',
        f'        sys.modules["{import_base}.func_" + s] = mod',
        '    # namespaces holding stale mechanical bindings: recovered-package',
        '    # modules still in sys.modules + each shadowed core\'s globals',
        '    spaces = []',
        '    for name, m in list(sys.modules.items()):',
        f'        if m is not None and name.startswith("{import_base}."):',
        '            spaces.append((None, m.__dict__))',
        '    for s, mod in shadows.items():',
        '        spaces.append((s, getattr(mod, "_core").__globals__))',
        '    patched = 0',
        '    for owner, ns in spaces:',
        '        for s, mod in shadows.items():',
        '            if s == owner:',
        '                continue          # keep the self-edge unshadowed',
        '            f = "func_" + s',
        '            repl = getattr(mod, f)',
        '            if f in ns and ns[f] is not repl:',
        '                ns[f] = repl',
        '                patched += 1',
        '    return len(shadows), patched',
        '',
    ])
