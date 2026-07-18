"""What an instruction DOES -- declared once, consumed by every analysis.

dos_re proves program behaviour well.  This module exists so it stops arguing
with itself about what an instruction means.

Before this, each consumer answered "does this touch memory / how wide /
through which segment / which flags" from its own private opcode table:
``ea_census.ACCESS_WIDTH`` and ``_STORE_OPS``, ``contracts._SS_ACCESS_WIDTH``,
``cfg._RM_WRITE_OPS``, two copies of ``_STRING_OPS``, three of ``_SEG_PREFIX``,
``emit_cpuless._flags_defined_by``.  Tables that answer overlapping questions
in separate files drift, and had: the census could not see string instructions
at all (814 of them, across 99 functions), so it under-reported the memory
touchers that an M4 ownership decision reads.

This is deliberately NOT a p-code/IR/SSA layer.  It describes only the facts
existing consumers already ask for, and it REFUSES anything it cannot describe
faithfully rather than guessing -- an unmodelled opcode must not silently look
like "touches no memory".

SCOPE, stated so it is not mistaken for more than it is:

* memory accesses -- read/write, segment (override-aware), EA components,
  width, and whether the address is implicit;
* flags defined, and the direction flag where it is genuinely consumed;
* implicit pointer updates (si/di stepped by DF);
* an explicit refusal for everything else.

General-purpose register operand tracking is NOT here.  No current consumer
reads such a set from a table -- ``emit_cpuless`` derives liveness through its
own machinery -- so adding one would be abstraction without a customer.  Only
the IMPLICIT register participants that no operand decoding would reveal
(string-op si/di/cx, xlat's bx/al, mul/div's ax/dx) are recorded.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .decode import INT

# --------------------------------------------------------------------------
# vocabulary

#: segment-override prefixes.  The one copy; the duplicates in decode.py,
#: decode32.py and ea_census.py are the drift this module exists to end.
SEG_PREFIX = {0x26: "es", 0x2E: "cs", 0x36: "ss", 0x3E: "ds"}

#: modrm r/m -> (base, index) for 16-bit addressing.
#:
#: A LONE si/di (rm=4,5) goes in the BASE slot, not the index slot.  Either
#: spelling names the same address, but ``Census.indexed_clusters()`` keys on
#: ``(segment, base, index)``, so the choice decides cluster IDENTITY -- and
#: the census is the incumbent.  Putting si in the index slot silently
#: re-keyed 1,900 sites in the corpus shadow, which is a migration hazard
#: rather than a bug, and exactly the kind of thing worth pinning in a
#: comment before someone "tidies" it.
RM16 = {0: ("bx", "si"), 1: ("bx", "di"), 2: ("bp", "si"), 3: ("bp", "di"),
        4: ("si", None), 5: ("di", None), 6: ("bp", None), 7: ("bx", None)}

ARITH_FLAGS = frozenset({"cf", "pf", "af", "zf", "sf", "of"})
ALL_FLAGS = frozenset({"cf", "pf", "af", "zf", "sf", "of", "df", "intf"})


class EffectsRefusal(Exception):
    """This module will not describe that instruction."""


@dataclass(frozen=True)
class MemAccess:
    """One memory operand.  ``reads and writes`` together = read-modify-write.

    ``segment`` is the segment REGISTER NAME after applying any override, not
    a value -- resolving it to a number is the caller's business (and, for
    string destinations, is not overridable at all).
    """

    reads: bool
    writes: bool
    segment: str
    width: int
    base: str | None = None
    index: str | None = None
    disp: int | None = None
    #: True when no modrm expresses this address (string ops' ds:si / es:di).
    implicit: bool = False

    @property
    def is_static(self) -> bool:
        return self.base is None and self.index is None and not self.implicit


@dataclass(frozen=True)
class Effects:
    mem: tuple = ()
    flags_written: frozenset = frozenset()
    flags_read: frozenset = frozenset()
    #: registers stepped implicitly by DF (string ops).
    ptr_updates: tuple = ()
    #: implicit register participants only -- see the module docstring.
    implicit_reads: frozenset = frozenset()
    implicit_writes: frozenset = frozenset()
    refusal: str | None = None

    @property
    def touches_memory(self) -> bool:
        return bool(self.mem)

    @property
    def writes_memory(self) -> bool:
        return any(m.writes for m in self.mem)

    @property
    def reads_memory(self) -> bool:
        return any(m.reads for m in self.mem)

    @property
    def mem_width(self) -> int | None:
        """The widest memory access, or None.  Matches what a width table
        meant when an instruction had exactly one memory operand."""
        return max((m.width for m in self.mem), default=None)

    @staticmethod
    def refused(reason: str) -> "Effects":
        return Effects(refusal=reason)


# --------------------------------------------------------------------------
# the declarative core
#
# Each entry names the ROLE of the memory operand, not a hand-computed answer
# to some consumer's question.  Everything a consumer wants is derived from
# the role plus the encoding.

#: (role, width) for opcodes whose memory operand is a plain modrm r/m.
#: role: "r" read, "w" write, "rw" read-modify-write, "none" no memory.
_RM: dict[int, tuple[str, int]] = {}

# ALU families: add/or/adc/sbb/and/sub/xor all WRITE their destination;
# cmp is the odd one out and only READS.  Encoding is regular:
#   +0 rm8,r8   +1 rm16,r16   +2 r8,rm8   +3 r16,rm16   +4 al,imm8  +5 ax,imm16
for _base, _name in ((0x00, "add"), (0x08, "or"), (0x10, "adc"), (0x18, "sbb"),
                     (0x20, "and"), (0x28, "sub"), (0x30, "xor"),
                     (0x38, "cmp")):
    _dest = "r" if _name == "cmp" else "rw"
    _RM[_base + 0] = (_dest, 1)
    _RM[_base + 1] = (_dest, 2)
    _RM[_base + 2] = ("r", 1)          # reg is the destination; rm is read
    _RM[_base + 3] = ("r", 2)
    # +4/+5 are acc,imm -- no memory operand at all

_RM.update({
    0x84: ("r", 1), 0x85: ("r", 2),          # test rm,r -- READ ONLY
    0x86: ("rw", 1), 0x87: ("rw", 2),        # xchg
    0x88: ("w", 1), 0x89: ("w", 2),          # mov rm,r
    0x8A: ("r", 1), 0x8B: ("r", 2),          # mov r,rm
    0x8C: ("w", 2),                          # mov rm,sreg
    0x8E: ("r", 2),                          # mov sreg,rm
    # 0x8D LEA computes an ADDRESS and touches NO memory.  It was in the
    # census width table, which made every `lea bx,[X]` look like a read of X.
    0x8D: ("none", 0),
    0x8F: ("w", 2),                          # pop rm
    0xC4: ("r", 4), 0xC5: ("r", 4),          # les/lds: 4 bytes (off+seg)
    0xC6: ("w", 1), 0xC7: ("w", 2),          # mov rm,imm
})

#: moffs forms: the offset is the immediate, not a modrm.
_MOFFS = {0xA0: ("r", 1), 0xA1: ("r", 2), 0xA2: ("w", 1), 0xA3: ("w", 2)}

#: opcodes whose memory role depends on the modrm REG field (the sub-opcode).
#: A per-opcode table cannot express these, which is why the census called
#: `cmp [x],5`, `push [x]` and `test [x],1` memory WRITES.
_GROUP: dict[int, dict[int, tuple[str, int]]] = {
    # grp1 rm,imm -- /7 is cmp (read only), the rest write
    0x80: {r: (("r" if r == 7 else "rw"), 1) for r in range(8)},
    0x81: {r: (("r" if r == 7 else "rw"), 2) for r in range(8)},
    0x83: {r: (("r" if r == 7 else "rw"), 2) for r in range(8)},
    # grp2 shifts -- always read-modify-write
    0xC0: {r: ("rw", 1) for r in range(8)},
    0xC1: {r: ("rw", 2) for r in range(8)},
    0xD0: {r: ("rw", 1) for r in range(8)},
    0xD1: {r: ("rw", 2) for r in range(8)},
    0xD2: {r: ("rw", 1) for r in range(8)},
    0xD3: {r: ("rw", 2) for r in range(8)},
    # grp3 -- /0,/1 test (read), /2 not, /3 neg (rmw), /4../7 mul/div (read)
    0xF6: {0: ("r", 1), 1: ("r", 1), 2: ("rw", 1), 3: ("rw", 1),
           4: ("r", 1), 5: ("r", 1), 6: ("r", 1), 7: ("r", 1)},
    0xF7: {0: ("r", 2), 1: ("r", 2), 2: ("rw", 2), 3: ("rw", 2),
           4: ("r", 2), 5: ("r", 2), 6: ("r", 2), 7: ("r", 2)},
    # grp4/5 -- /0 inc /1 dec are rmw; call/jmp/push only READ the operand
    0xFE: {0: ("rw", 1), 1: ("rw", 1)},
    0xFF: {0: ("rw", 2), 1: ("rw", 2), 2: ("r", 2), 3: ("r", 4),
           4: ("r", 2), 5: ("r", 4), 6: ("r", 2)},
}

#: string instructions: (mnemonic, source-side, dest-side, width).
#: The source side honours a segment override; the destination side is es:di
#: and is NOT overridable on x86.  This is the fact the census lacked.
_STRING = {
    0xA4: ("movsb", True, True, 1), 0xA5: ("movsw", True, True, 2),
    0xA6: ("cmpsb", True, True, 1), 0xA7: ("cmpsw", True, True, 2),
    0xAA: ("stosb", False, True, 1), 0xAB: ("stosw", False, True, 2),
    0xAC: ("lodsb", True, False, 1), 0xAD: ("lodsw", True, False, 2),
    0xAE: ("scasb", False, True, 1), 0xAF: ("scasw", False, True, 2),
}
#: which string ops WRITE their es:di operand (vs merely compare against it)
_STRING_WRITES_DEST = {0xA4, 0xA5, 0xAA, 0xAB}

#: stack-touching opcodes with no modrm operand: (reads, writes, width).
_STACK = {}
for _o in range(0x50, 0x58):
    _STACK[_o] = (False, True, 2)            # push r16
for _o in range(0x58, 0x60):
    _STACK[_o] = (True, False, 2)            # pop r16
for _o in (0x06, 0x0E, 0x16, 0x1E):
    _STACK[_o] = (False, True, 2)            # push sreg
for _o in (0x07, 0x17, 0x1F):
    _STACK[_o] = (True, False, 2)            # pop sreg
_STACK[0x9C] = (False, True, 2)              # pushf
_STACK[0x9D] = (True, False, 2)              # popf
_STACK[0xC3] = (True, False, 2)              # ret
_STACK[0xC2] = (True, False, 2)
_STACK[0xCB] = (True, False, 4)              # retf
_STACK[0xCA] = (True, False, 4)
_STACK[0xE8] = (False, True, 2)              # call near
_STACK[0x9A] = (False, True, 4)              # call far
_STACK[0xCF] = (True, False, 6)              # iret: ip, cs, flags

#: opcodes with no memory operand at all, and no need for one to be modelled.
_NO_MEM = frozenset(
    set(range(0x40, 0x50))                   # inc/dec r16
    | set(range(0xB0, 0xC0))                 # mov r,imm
    | set(range(0x70, 0x80))                 # jcc rel8
    | {0x04, 0x05, 0x0C, 0x0D, 0x14, 0x15, 0x1C, 0x1D,
       0x24, 0x25, 0x2C, 0x2D, 0x34, 0x35, 0x3C, 0x3D}   # acc,imm ALU
    | {0x90, 0x98, 0x99, 0xA8, 0xA9,
       0xE0, 0xE1, 0xE2, 0xE3, 0xE9, 0xEB,
       0xF5, 0xF8, 0xF9, 0xFA, 0xFB, 0xFC, 0xFD,
       0x27, 0x2F, 0x37, 0x3F, 0xEA}
    | set(range(0x91, 0x98))                 # xchg ax,r
)

#: port I/O -- a real side effect, but NOT a memory access.  Naming it keeps
#: it from falling into the refusal bucket and looking like an unknown.
_PORT_IO = frozenset({0xE4, 0xE5, 0xE6, 0xE7, 0xEC, 0xED, 0xEE, 0xEF})


# --------------------------------------------------------------------------
# derivation

def segment_of(inst) -> str:
    """The segment register a modrm operand addresses: an explicit override,
    else the default rule (bp-based effective addresses default to ss)."""
    for p in inst.prefixes:
        if p in SEG_PREFIX:
            return SEG_PREFIX[p]
    if inst.modrm is not None and inst.mod != 3:
        if inst.mod == 0 and inst.rm == 6:
            return "ds"                       # [disp16]
        base, _idx = RM16.get(inst.rm, (None, None))
        if base == "bp":
            return "ss"
    return "ds"


def _source_segment(inst) -> str:
    """ds unless overridden -- the string-op SOURCE side only."""
    for p in inst.prefixes:
        if p in SEG_PREFIX:
            return SEG_PREFIX[p]
    return "ds"


def _modrm_mem(inst, role: str, width: int) -> tuple:
    """The MemAccess for a modrm operand, or () when it is a register form."""
    if role == "none" or inst.modrm is None or inst.mod == 3:
        return ()
    seg = segment_of(inst)
    if inst.mod == 0 and inst.rm == 6:
        base = index = None
        disp = inst.disp or 0
    else:
        shape = RM16.get(inst.rm)
        if shape is None:
            raise EffectsRefusal(f"unmodelled-rm:{inst.rm}")
        base, index = shape
        disp = inst.disp or 0
    return (MemAccess(reads="r" in role, writes="w" in role, segment=seg,
                      width=width, base=base, index=index, disp=disp),)


def _string_flags(inst) -> frozenset:
    """cmps/scas define subtraction flags -- but only when a rep prefix cannot
    make the count zero, in which case nothing is defined statically."""
    if inst.op in (0xA6, 0xA7, 0xAE, 0xAF) and not any(
            p in (0xF2, 0xF3) for p in inst.prefixes):
        return ARITH_FLAGS
    return frozenset()


def _shift_flags(inst) -> frozenset:
    """Shift/rotate flag effects depend on the COUNT, which is why a plain
    opcode table cannot express them.

    C0/C1 take an immediate count: zero defines nothing, one also defines OF.
    D0/D1 are count-1 forms.  D2/D3 take the count from cl, so statically
    nothing is guaranteed (cl may be zero)."""
    if inst.reg not in (0, 1, 2, 3, 4, 5, 7):
        return frozenset()
    logical = {"zf", "sf", "pf"} if inst.reg in (4, 5, 7) else set()
    if inst.op in (0xC0, 0xC1):
        n = (inst.imm or 0) & 0x1F
        if n == 0:
            return frozenset()
        base = {"cf"} | logical
        if n == 1:
            base |= {"of"}
        return frozenset(base)
    if inst.op in (0xD0, 0xD1):
        return frozenset({"cf", "of"} | logical)
    return frozenset()                        # D2/D3: count in cl


def _flags_written(inst) -> frozenset:
    op = inst.op
    if inst.kind == INT:
        return ALL_FLAGS
    if op == 0x9D:                            # popf: whole word from the stack
        return ALL_FLAGS
    if op in (0xF8, 0xF9):
        return frozenset({"cf"})
    if op == 0x27:                            # daa (OF left undefined)
        return frozenset({"cf", "af", "zf", "sf", "pf"})
    if op in (0xFC, 0xFD):
        return frozenset({"df"})
    if op in (0xFA, 0xFB):
        return frozenset({"intf"})
    if op in (0xC0, 0xC1, 0xD0, 0xD1, 0xD2, 0xD3):
        return _shift_flags(inst)
    if op in (0xA6, 0xA7, 0xAE, 0xAF):
        return _string_flags(inst)
    if op in (0xF6, 0xF7) and inst.reg in (4, 5):        # mul/imul: CF+OF
        return frozenset({"cf", "of"})
    if op in (0x69, 0x6B):                               # imul r,rm,imm
        return frozenset({"cf", "of"})
    if (op <= 0x3D and (op & 7) <= 5
            and (op & 0xC7) not in (0x06, 0x07, 0xC6, 0xC7)) \
            or op in (0x80, 0x81, 0x83, 0x84, 0x85, 0xA8, 0xA9) \
            or (op in (0xF6, 0xF7) and inst.reg == 3):
        return ARITH_FLAGS
    if 0x40 <= op <= 0x4F or (op in (0xFE, 0xFF) and inst.reg in (0, 1)):
        return frozenset({"pf", "af", "zf", "sf", "of"})
    return frozenset()


def effects_of(inst) -> Effects:
    """Everything the analyses need to know about one instruction.

    Refuses rather than guesses: an opcode this module does not model returns
    ``Effects.refused(...)``, never a confident "touches nothing".
    """
    op = inst.op
    flags_w = _flags_written(inst)

    # -- string instructions: implicit ds:si and/or es:di ------------------
    if op in _STRING:
        mnem, has_src, has_dst, width = _STRING[op]
        acc = []
        reads = set()
        writes = set()
        ptr = []
        if has_src:
            acc.append(MemAccess(reads=True, writes=False,
                                 segment=_source_segment(inst), width=width,
                                 index="si", implicit=True))
            reads.add("si")
            writes.add("si")
            ptr.append("si")
        if has_dst:
            # es:di is NOT overridable
            acc.append(MemAccess(reads=op in (0xA6, 0xA7, 0xAE, 0xAF),
                                 writes=op in _STRING_WRITES_DEST,
                                 segment="es", width=width,
                                 index="di", implicit=True))
            reads.add("di")
            writes.add("di")
            ptr.append("di")
        if any(p in (0xF2, 0xF3) for p in inst.prefixes):
            reads.add("cx")
            writes.add("cx")
        if op in (0xAA, 0xAB, 0xAC, 0xAD, 0xAE, 0xAF):
            (writes if op in (0xAC, 0xAD) else reads).add(
                "ax" if width == 2 else "al")
        return Effects(mem=tuple(acc), flags_written=flags_w,
                       flags_read=frozenset({"df"}),
                       ptr_updates=tuple(ptr),
                       implicit_reads=frozenset(reads),
                       implicit_writes=frozenset(writes))

    # -- xlat: reads ds:[bx+al] -------------------------------------------
    if op == 0xD7:
        return Effects(
            mem=(MemAccess(reads=True, writes=False,
                           segment=_source_segment(inst), width=1,
                           base="bx", implicit=True),),
            implicit_reads=frozenset({"bx", "al"}),
            implicit_writes=frozenset({"al"}))

    # -- moffs -------------------------------------------------------------
    if op in _MOFFS:
        role, width = _MOFFS[op]
        return Effects(
            mem=(MemAccess(reads="r" in role, writes="w" in role,
                           segment=_source_segment(inst), width=width,
                           disp=inst.imm or 0),),
            flags_written=flags_w)

    # -- modrm-addressed, sub-opcode dependent ------------------------------
    if op in _GROUP:
        table = _GROUP[op]
        if inst.reg not in table:
            return Effects.refused(f"unmodelled-subop:{op:#04x}/{inst.reg}")
        role, width = table[inst.reg]
        try:
            mem = _modrm_mem(inst, role, width)
        except EffectsRefusal as e:
            return Effects.refused(str(e))
        extra_r, extra_w = set(), set()
        if op in (0xF6, 0xF7) and inst.reg in (4, 5, 6, 7):
            extra_r.add("ax")
            extra_w.update({"ax", "dx"})
        return Effects(mem=mem, flags_written=flags_w,
                       implicit_reads=frozenset(extra_r),
                       implicit_writes=frozenset(extra_w))

    # -- modrm-addressed, fixed role ---------------------------------------
    if op in _RM:
        role, width = _RM[op]
        try:
            mem = _modrm_mem(inst, role, width)
        except EffectsRefusal as e:
            return Effects.refused(str(e))
        return Effects(mem=mem, flags_written=flags_w)

    # -- stack traffic ------------------------------------------------------
    if op in _STACK:
        reads, writes, width = _STACK[op]
        return Effects(
            mem=(MemAccess(reads=reads, writes=writes, segment="ss",
                           width=width, base="sp", implicit=True),),
            flags_written=flags_w,
            implicit_reads=frozenset({"sp"}),
            implicit_writes=frozenset({"sp"}))

    if op in _PORT_IO:
        return Effects(flags_written=flags_w)

    if op in _NO_MEM:
        return Effects(flags_written=flags_w)

    if inst.kind == INT:
        # an interrupt's memory effects are the handler's, not this
        # instruction's -- describing them here would be a fiction
        return Effects(flags_written=flags_w)

    return Effects.refused(f"unmodelled-opcode:{op:#04x}")
