"""Which ADDRESS SPACE does an ``ss``-addressed operand belong to?

One segment register can carry two unrelated things at once.  In a small-model
16-bit program ``ss`` addresses the machine stack AND, very often, ordinary
program data parked at the bottom of the same segment.  Every analysis that
treats "ss" as a single thing gets one of the two wrong.

The three cases this module separates, and why they are genuinely different:

* ``MACHINE_STACK`` — push/pop/call/ret residue and bp-relative frame slots.
  This is CPU carrier, and a de-stacked core deletes it: it becomes locals.
* ``SS_DATA`` — program data that merely happens to live in the stack
  segment.  This SURVIVES de-stacking; it is real state the program reads and
  writes, and ``ss`` is an ordinary data-segment input for it.
* ``AMBIGUOUS`` — a computed ``ss:`` address whose value cannot be placed on
  either side of the boundary.  It might read the terrain map; it might read
  the live frame.  Nothing here guesses which.

The previous rule (``ss_globals_only``, a single ``SS_GLOBALS_FLOOR``) was a
special case of this: it accepted only CONSTANT displacements below a
port-supplied floor.  That is sound but far too narrow — a program addressing
a 2-D array in the stack segment legitimately computes offsets well above any
"globals" floor, and the constant-only rule refuses the whole function.  This
module keeps the floor as EVIDENCE and adds the missing distinction, rather
than weakening the floor or widening it until the interesting case slips
through.

**The proof obligation is explicit and external.**  A computed ``ss:`` access
is classified ``SS_DATA_UNPROVEN``, never silently promoted.  It becomes
``SS_DATA`` only when a caller supplies positive evidence that the address is
sp-independent — the differential's two-run discriminator (drive the
mechanical side twice with different initial sp; writes that move with sp are
stack, writes identical under both are data).  Absent that evidence it
refuses.  A classifier that assumed the benign case would be exactly the
silent-fallback failure this codebase exists to prevent.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .effects import SEG_PREFIX

#: opcodes whose memory operand IS the machine stack, with no modrm to read.
_IMPLICIT_STACK_OPS = (
    frozenset(range(0x50, 0x60))                      # push/pop r16
    | frozenset({0x06, 0x0E, 0x16, 0x1E,              # push sreg
                 0x07, 0x17, 0x1F,                    # pop sreg
                 0x9C, 0x9D,                          # pushf / popf
                 0xC2, 0xC3, 0xCA, 0xCB, 0xCF,        # ret / retf / iret
                 0xE8, 0x9A,                          # call near / far
                 0xC8, 0xC9})                         # enter / leave
)


class Space(Enum):
    MACHINE_STACK = "machine-stack"
    SS_DATA = "ss-data"
    SS_DATA_UNPROVEN = "ss-data-unproven"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class SsAccess:
    ip: int
    space: Space
    reason: str
    disp: int | None = None
    width: int = 0
    computed: bool = False


def _has_override(inst, seg: int) -> bool:
    return any(p == seg for p in inst.prefixes)


def _frame_shaped(inst) -> bool:
    """A bp-based effective address that DEFAULTS to ss.

    ``[bp+d]``, ``[bp+si]``, ``[bp+di]`` are the frame idiom: bp holds a
    caller-established frame pointer, so these are genuine stack accesses even
    though no prefix says so.  ``[bp+disp16]`` with mod=0 rm=6 is NOT this --
    that encoding is plain ``[disp16]`` and defaults to ds.
    """
    if inst.modrm is None or inst.mod == 3:
        return False
    if any(p in (0x26, 0x2E, 0x3E) for p in inst.prefixes):
        return False                      # explicitly redirected elsewhere
    if inst.mod == 0 and inst.rm == 6:
        return False                      # [disp16], a ds form
    return inst.rm in (2, 3, 6)           # [bp+si], [bp+di], [bp+disp]


def classify_ss(scan, *, stack_floor: int | None = None) -> list:
    """Every ``ss``-addressed operand in one function, placed in a space.

    ``stack_floor`` is the port-supplied, evidence-backed offset below which
    the live machine stack provably never descends.  It is a per-program
    layout fact -- boot sp, stack depth, memory model -- and this module has
    no default for it: without it, no constant displacement can be PROVEN to
    be data, and everything computed stays unproven.
    """
    out = []
    for ip, i in sorted(scan.insts.items()):
        if i.op in _IMPLICIT_STACK_OPS:
            out.append(SsAccess(ip, Space.MACHINE_STACK,
                                "implicit stack traffic"))
            continue
        explicit = _has_override(i, 0x36)
        if _frame_shaped(i) and not explicit:
            out.append(SsAccess(ip, Space.MACHINE_STACK,
                                "bp-relative frame slot"))
            continue
        if not explicit:
            continue                       # not an ss operand at all
        # -- an EXPLICIT ss: override.  Data, or a computed address? --------
        if i.modrm is not None and i.mod != 3 and i.mod == 0 and i.rm == 6:
            disp, computed = (i.disp or 0), False
        elif 0xA0 <= i.op <= 0xA3:
            disp, computed = (i.imm or 0), False
        elif i.modrm is not None and i.mod != 3:
            disp, computed = (i.disp or 0), True
        else:
            continue                       # register form; no memory operand
        width = _width_of(i)
        if computed:
            out.append(SsAccess(ip, Space.SS_DATA_UNPROVEN,
                                "computed ss address: needs sp-independence "
                                "evidence", disp, width, True))
        elif stack_floor is None:
            out.append(SsAccess(ip, Space.AMBIGUOUS,
                                "no stack floor supplied", disp, width))
        elif disp + width <= stack_floor:
            out.append(SsAccess(ip, Space.SS_DATA,
                                f"constant offset below the {stack_floor:#x} "
                                f"stack floor", disp, width))
        else:
            out.append(SsAccess(ip, Space.AMBIGUOUS,
                                f"constant offset {disp:#x}+{width} reaches "
                                f"the live stack (floor {stack_floor:#x})",
                                disp, width))
    return out


def _width_of(inst) -> int:
    from .effects import effects_of
    eff = effects_of(inst)
    if eff.refusal is not None:
        return 2                           # conservative: assume the wider
    return eff.mem_width or 2


def ss_data_verdict(accesses, *, sp_independence_proven: bool = False):
    """``(ok, reason)`` for treating ``ss`` as a data-segment input.

    ``sp_independence_proven`` is the differential's evidence that this
    function's ss writes do NOT move with the stack pointer.  It upgrades
    computed accesses from unproven to data.  It is a positive claim a caller
    must have earned; defaulting it to True would make the whole
    classification decorative.
    """
    if not accesses:
        return False, "no ss accesses"
    spaces = {a.space for a in accesses}
    if Space.AMBIGUOUS in spaces:
        bad = next(a for a in accesses if a.space is Space.AMBIGUOUS)
        return False, f"ambiguous-ss-address:{bad.reason}"
    if Space.SS_DATA_UNPROVEN in spaces and not sp_independence_proven:
        return False, "computed-ss-address-unproven"
    if not (spaces & {Space.SS_DATA, Space.SS_DATA_UNPROVEN}):
        return False, "ss is machine stack only"
    return True, "ss carries data"


def requires_sp_independence_proof(scan, stack_floor: int | None) -> bool:
    """Does this function carry an UNDISCHARGED sp-independence obligation?

    True when it addresses ss with a computed effective address.  Such a core
    may be emitted -- its shape is composable -- but it must not reach the
    VERIFIED ledger until the differential has shown its ss writes do not
    move with the stack pointer.  Consumers ask this rather than trusting a
    flag threaded through emission, so the question always has exactly one
    answer derived from the code being verified.
    """
    return any(a.space is Space.SS_DATA_UNPROVEN
               for a in classify_ss(scan, stack_floor=stack_floor))


def has_frame_access(accesses) -> bool:
    """True when any access is a genuine frame slot.

    A function that reads its own frame through ss AND uses ss for data is not
    disqualified -- de-stacking removes the frame -- but the caller may want
    to know, because the differential must then separate two live uses of one
    segment rather than one.
    """
    return any(a.space is Space.MACHINE_STACK
               and a.reason == "bp-relative frame slot" for a in accesses)
