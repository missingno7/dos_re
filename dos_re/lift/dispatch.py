"""Resolve the concrete target of a NEAR indirect transfer from live CPU state.

Near indirect jmp/call sites -- jump tables, computed function pointers, dispatch
stubs -- are unresolvable statically, but when a program runs, every target each
site takes is observable for free.  A capture probe traps such a site at its
instruction boundary and calls :func:`resolve_near_indirect_target` to record the
target the interpreter is about to take, building ``{site: [targets]}``
evidence. A CPUless implementation may use that evidence only when every
required observed target is dispatchable.

Two shapes are handled, both genuine dispatches the CPUless dispatch registry
must resolve:

* **memory-indirect** (``call [bx+d]``, ``jmp cs:[bx*2+table]``) -- the target is
  the word at the computed effective address;
* **register-indirect** (``call ax``, ``jmp bx``) -- a computed function pointer
  whose target IS the register value.

The resolver is PURE: it reads an already-decoded instruction plus the register
file and memory and never mutates CPU state (unlike ``CPU8086.decode_ea``, which
fetches the displacement from the stream).  The addressing tables mirror
``decode_ea`` exactly.
"""
from __future__ import annotations

#: word registers by ModRM rm (Intel encoding order).
REG16 = ("ax", "cx", "dx", "bx", "sp", "bp", "si", "di")

#: 16-bit ModRM base/index register names by rm (mod != 3) and the default
#: segment -- BP-based addressing defaults to SS, everything else to DS (this is
#: exactly the split in ``CPU8086.decode_ea``).
_EA = (
    (("bx", "si"), "ds"), (("bx", "di"), "ds"), (("bp", "si"), "ss"), (("bp", "di"), "ss"),
    (("si",), "ds"), (("di",), "ds"), (("bp",), "ss"), (("bx",), "ds"),
)


def _operand_ea(state, inst) -> "tuple[int, int] | None":
    """``(segment value, offset)`` of ``inst``'s memory operand, or ``None``
    when the operand is a register (``mod == 3``).  Pure."""
    mod, rm = inst.modrm >> 6, inst.modrm & 7
    if mod == 3:
        return None
    seg = inst.seg_override
    if mod == 0 and rm == 6:                          # direct [disp16]
        off = (inst.disp or 0) & 0xFFFF
    else:
        regs, default_seg = _EA[rm]
        off = (sum(getattr(state, r) for r in regs) + (inst.disp or 0)) & 0xFFFF
        seg = seg or default_seg
    return getattr(state, seg or "ds") & 0xFFFF, off


def resolve_near_indirect_target(state, mem, inst) -> "str | None":
    """The ``'CS:IP'`` a near indirect jmp/call at ``inst`` transfers to.

    ``state`` is a register file exposing ``cs`` and the word/segment registers by
    name; ``mem`` exposes ``rw(seg, off) -> int``; ``inst`` is a decoded
    instruction (``modrm``, ``mod``, ``rm``, ``disp``, ``seg_override``).  Returns
    ``None`` when ``inst`` carries no ModRM (nothing to resolve).  Does not mutate
    ``state`` or ``mem``.
    """
    if inst.modrm is None:
        return None
    cs = state.cs & 0xFFFF
    ea = _operand_ea(state, inst)
    if ea is None:                        # register-indirect: target = reg value
        return f"{cs:04X}:{getattr(state, REG16[inst.modrm & 7]) & 0xFFFF:04X}"
    segval, off = ea
    return f"{cs:04X}:{mem.rw(segval, off) & 0xFFFF:04X}"


def resolve_far_indirect_target(state, mem, inst) -> "str | None":
    """The ``'SEG:OFF'`` a FAR indirect jmp/call (``FF /3``, ``FF /5``) at
    ``inst`` transfers to -- the far pointer (offset word, then segment word)
    stored at the operand's effective address.

    The far counterpart of :func:`resolve_near_indirect_target`, and the same
    evidence channel: a capture probe traps the site and records the target it
    is about to take, so the ``{site: [targets]}`` map the CPUless promoter's
    evidence gate consumes covers far dispatch as well as near.  The only
    structural difference is that the target's SEGMENT comes from memory rather
    than from the current CS, so the key names a different segment -- the wire
    format ("SEG:OFF") is unchanged.

    ``FF /3`` and ``FF /5`` require a MEMORY operand (a register cannot hold a
    32-bit far pointer), so a ``mod == 3`` encoding is not a far transfer at
    all and returns ``None``, as does an instruction with no ModRM.  Pure.
    """
    if inst.modrm is None:
        return None
    ea = _operand_ea(state, inst)
    if ea is None:
        return None
    segval, off = ea
    tip = mem.rw(segval, off) & 0xFFFF
    tcs = mem.rw(segval, (off + 2) & 0xFFFF) & 0xFFFF
    return f"{tcs:04X}:{tip:04X}"


# ---------------------------------------------------------------------------
# DISPATCH-ARM ABSORPTION: a switch arm is an ALTERNATE ENTRY of its container
# ---------------------------------------------------------------------------
#
# A compiler lowers a dense `switch` into a jump table plus a SHARED EPILOGUE:
# the container establishes the frame, computes an index and tail-dispatches
# (`jmp cs:[bx*2+table]`); each arm runs its case body and falls into the one
# `leave; ret` the container's prologue set up.  An arm is therefore NOT a
# standalone function -- it is a second ENTRY POINT into the container's body,
# sharing the container's frame and its epilogue.
#
# A function carver that only follows static edges cannot see that: the arms are
# unreachable from the container's entry, so each one is carved as its own
# "function" whose scan re-derives the SAME shared tail.  In isolation an arm
# looks structurally broken -- `leave` with no `enter`, `pop si; pop di` with no
# matching pushes -- and the CPUless gate refuses it (`leave-without-enter`,
# `frame-restore-without-establish`).  That refusal is correct: the arm's frame
# base genuinely lives in the container.  It is also fatal to composition,
# because the container's own evidence gate then sees an UNPROMOTED dynamic
# target and refuses the container too, holding out the entire dispatch cluster.
#
# ABSORPTION is the graph-completeness repair: union the arms' reachable
# instructions back into the container's scan and declare each arm ip a dispatch
# ALTERNATE ENTRY of the container.  The container becomes one function with
# several entry points -- which is what the original object code always was.
# The establish and the restore are then in the SAME scan, so the frame checks
# pass on their own terms (nothing is suppressed), the jump table resolves as an
# intra-function landing (`_LOCAL`), and the arms need no standalone promotion.
#
# SOUNDNESS.  Absorption is only sound when the arm really is a re-carving of the
# container's own bytes, so it is CHECKED, never assumed:
#   * every instruction the two scans share must be byte-identical (same ip,
#     same encoded bytes) -- proof they decode the one instruction stream;
#   * the arm must not establish a frame of its OWN in bytes the container does
#     not already contain (a genuine callee that happens to be jumped to);
#   * the arm scan must itself be liftable (no refusals to launder).
# Anything else raises :class:`ArmAbsorptionRefusal` -- loud, never a merge that
# guesses.  The merged scan is then handed to the ordinary promotion gate, which
# proves the frame/stack contract for the composed whole exactly as for any
# other function.


class ArmAbsorptionRefusal(Exception):
    """A dispatch arm could not be proven to be an alternate entry of its
    container (see :func:`absorb_dispatch_arms`).  Carries a stable slug."""


def _frame_establish_ips(scan) -> set:
    """ips in ``scan`` that establish a frame: the atomic ``enter`` (0xC8), or
    the hand-rolled ``push bp`` (0x55) / ``mov bp,sp`` pair.  Mirrors
    ``emit_cpuless._check_frame_pointer``'s establish criteria."""
    out = set()
    push_bp = any(i.op == 0x55 for i in scan.insts.values())
    for ip, i in scan.insts.items():
        if i.op == 0xC8:
            out.add(ip)
        elif push_bp and i.op == 0x8B and i.modrm == 0xEC:   # mov bp,sp
            out.add(ip)
    return out


def dispatch_arm_candidates(scan, cs, dyn_evidence, *,
                            include_in_scan=False) -> list:
    """The observed dispatch targets of ``scan``'s near jump-table sites -- the
    arms of the switch this function dispatches.

    ``dyn_evidence`` maps ``"CS:IP"`` site -> the observed target keys.  Only a
    NEAR indirect jump (``jmp_ind`` with a ModRM whose /digit is 4) into this
    same code segment qualifies: a far indirect jump leaves the segment and an
    indirect CALL is a call, not an alternate entry.

    By default only the arms OUTSIDE the scan are returned (the ones absorption
    must pull in).  ``include_in_scan`` also returns the landings the static CFG
    already reaches: those need no fusion, but they are still alternate entries
    of this container rather than functions in their own right -- a carver that
    also carved them as entries produces duplicate bodies unless they are
    recognised as owned.  Returns the arm offsets, sorted and de-duplicated."""
    arms = set()
    for ip, i in scan.insts.items():
        if i.kind != "jmp_ind" or i.modrm is None or ((i.modrm >> 3) & 7) != 4:
            continue
        for tgt in dyn_evidence.get(f"{cs:04X}:{ip:04X}".upper(), ()):
            tcs, tip = (int(x, 16) for x in tgt.split(":"))
            if tcs == cs and (include_in_scan or tip not in scan.insts):
                arms.add(tip)
    return sorted(arms)


def absorb_dispatch_arms(container, arm_scans):
    """Fuse dispatch ARMS into their CONTAINER's scan, returning a NEW scan in
    which each arm ip is a reachable alternate entry.

    ``container`` is the container's :class:`~dos_re.lift.cfg.FunctionScan`;
    ``arm_scans`` maps each arm's entry offset to the scan carved at that entry.
    The container scan is not mutated.  Raises :class:`ArmAbsorptionRefusal`
    when an arm cannot be proven to be a re-carving of the container's own
    instruction stream (see the module notes above).

    The caller must pass the absorbed arm ips as ``dispatch_addrs`` to the
    promotion gate/emitter so they become FORCED block leaders and exported
    alternate entries -- absorbing without declaring them would leave the jump
    table's landings unreachable in the emitted body."""
    import copy

    merged = copy.copy(container)
    merged.insts = dict(container.insts)
    merged.exits = list(container.exits)
    merged.calls_near = set(container.calls_near)
    merged.calls_far = set(container.calls_far)
    merged.calls_indirect = list(container.calls_indirect)
    merged.ints = set(container.ints)
    merged.refusals = list(container.refusals)
    merged.cs_store_targets = list(container.cs_store_targets)
    merged.boundary_heads = list(container.boundary_heads)
    for ip in sorted(arm_scans):
        arm = arm_scans[ip]
        if arm is None or ip in container.insts:
            continue        # already an intra-scan landing: nothing to fuse
        if arm.refusals:
            raise ArmAbsorptionRefusal("arm-not-liftable")
        if ip not in arm.insts:
            raise ArmAbsorptionRefusal("arm-entry-not-decoded")
        new_ips = set(arm.insts) - set(merged.insts)
        for aip, inst in arm.insts.items():
            have = merged.insts.get(aip)
            if have is not None and have.raw != inst.raw:
                raise ArmAbsorptionRefusal("arm-overlap-byte-conflict")
        # A frame establish in bytes the container does not already contain
        # means this is a self-framing function reached by an indirect jump --
        # a genuine tail CALL, not a shared-epilogue arm.  Absorbing it would
        # splice a second frame into the container's body.
        if _frame_establish_ips(arm) & new_ips:
            raise ArmAbsorptionRefusal("arm-establishes-own-frame")
        for aip, inst in arm.insts.items():
            merged.insts.setdefault(aip, inst)
        have_exits = {e.ip for e in merged.exits}
        merged.exits += [e for e in arm.exits if e.ip not in have_exits]
        merged.calls_near |= arm.calls_near
        merged.calls_far |= arm.calls_far
        merged.calls_indirect = sorted(set(merged.calls_indirect)
                                       | set(arm.calls_indirect))
        merged.ints |= arm.ints
        have_cs = set(merged.cs_store_targets)
        merged.cs_store_targets += [t for t in arm.cs_store_targets
                                    if t not in have_cs]
        have_bh = set(merged.boundary_heads)
        merged.boundary_heads += [h for h in arm.boundary_heads
                                  if h not in have_bh]
    return merged
