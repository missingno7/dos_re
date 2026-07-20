"""Read-only effective-address evidence for memory representation work.

docs/memory_schema.md §4 and §14.2: the recovery IR pins instruction bytes and
flags memory operands, but records no DECODED address expression.  Because the
bytes are pinned, the single decoder can re-elaborate every memory site into a
symbolic address expression -- so the IR is sufficient and no format change is
needed, which preserves the single-source-of-truth rule.

This pass is READ-ONLY.  It emits address expressions, candidate regions,
segment aliases, and structured blockers.  It rewrites nothing and promotes
nothing: region selection (§6) consumes it, and §6 deliberately no longer
prescribes a first region because the evidence has to choose one.

Game-agnostic by construction: no address, offset, or region of any particular
program appears here.  A port supplies its own roots and reads the census.

Refusal-first: an address shape this pass cannot express symbolically is
REPORTED as a blocker with its site, never approximated.  An approximated
address expression would silently widen or narrow an ownership closure, and
the closure is what any ownership or representation decision rests on.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .cpuless import SEGS

#: 16-bit ModRM r/m -> (base register, index register).  ``None`` where the
#: encoding has no such component.  rm=6 with mod=0 is the disp16 form and
#: carries neither; with mod!=0 it is [bp+disp].
_RM16 = {
    0: ("bx", "si"), 1: ("bx", "di"), 2: ("bp", "si"), 3: ("bp", "di"),
    4: ("si", None), 5: ("di", None), 6: ("bp", None), 7: ("bx", None),
}

#: segment-override prefixes
_SEG_PREFIX = {0x26: "es", 0x2E: "cs", 0x36: "ss", 0x3E: "ds"}

#: Access width in bytes, keyed by opcode, for the forms this census
#: expresses.  EXPLICIT rather than derived from opcode parity -- parity is
#: not a width rule (0x8C `mov r/m16, Sreg` and 0xC4 `LES r16, m16:16` are
#: both even-opcoded yet 2 and 4 bytes wide).  An opcode absent here is a
#: REPORTED blocker, because a guessed width mis-sizes a region.
ACCESS_WIDTH = {
    0x00: 1, 0x01: 2, 0x02: 1, 0x03: 2,          # add
    0x08: 1, 0x09: 2, 0x0A: 1, 0x0B: 2,          # or
    0x10: 1, 0x11: 2, 0x12: 1, 0x13: 2,          # adc
    0x18: 1, 0x19: 2, 0x1A: 1, 0x1B: 2,          # sbb
    0x20: 1, 0x21: 2, 0x22: 1, 0x23: 2,          # and
    0x28: 1, 0x29: 2, 0x2A: 1, 0x2B: 2,          # sub
    0x30: 1, 0x31: 2, 0x32: 1, 0x33: 2,          # xor
    0x38: 1, 0x39: 2, 0x3A: 1, 0x3B: 2,          # cmp
    0x80: 1, 0x81: 2, 0x83: 2,                   # grp1 r/m, imm
    0x84: 1, 0x85: 2,                            # test
    0x86: 1, 0x87: 2,                            # xchg
    0x88: 1, 0x89: 2, 0x8A: 1, 0x8B: 2,          # mov r/m <-> r
    0x8C: 2, 0x8E: 2,                            # mov r/m16 <-> Sreg
    0x8F: 2,                                     # pop r/m16 (a WRITE)
    0x8D: 2,                                     # lea (address, not a load)
    0xA0: 1, 0xA1: 2, 0xA2: 1, 0xA3: 2,          # mov acc <-> moffs
    0xC4: 4, 0xC5: 4,                            # les / lds (seg:off pair)
    0xC6: 1, 0xC7: 2,                            # mov r/m, imm
    0xD0: 1, 0xD1: 2, 0xD2: 1, 0xD3: 2,          # shift grp2
    0xF6: 1, 0xF7: 2,                            # grp3 (test/neg/mul/div)
    0xFE: 1, 0xFF: 2,                            # inc/dec/push/call/jmp r/m
}


@dataclass(frozen=True)
class AddressExpr:
    """One memory site's symbolic address.

    ``segment`` is the register NAME that supplies the segment (after the
    default rule and any override), not its runtime value -- the census is
    static.  ``base``/``index`` are register names or None; ``disp`` is the
    signed displacement; ``width`` the access size in bytes.
    """

    key: str                 # CS:IP of the owning function
    ip: int                  # site address
    segment: str
    base: str | None
    index: str | None
    disp: int
    width: int
    writes: bool             # the site stores (best-effort from the opcode)

    @property
    def is_static(self) -> bool:
        """A fixed address: no register components, so a candidate scalar
        global.  These are the regions with no stride and no index question."""
        return self.base is None and self.index is None

    def region_shape(self) -> str:
        if self.is_static:
            return f"{self.segment}:[{self.disp:#06x}]"
        parts = [p for p in (self.base, self.index) if p]
        return f"{self.segment}:[{'+'.join(parts)}{self.disp:+#x}]"


@dataclass
class Blocker:
    """A site whose address this pass will not express symbolically.

    ``segments`` names the segment REGISTERS the site addresses, when that
    much is known even though the offset is not.  A blocker with segments is
    not merely a report: ``possible_touchers`` uses it to keep an unprovable
    access visible to the ownership closure, instead of letting an
    unexpressible address quietly mean "no access".
    """

    key: str
    ip: int
    reason: str
    segments: tuple = ()


@dataclass
class Census:
    sites: list = field(default_factory=list)
    blockers: list = field(default_factory=list)

    def static_clusters(self) -> dict:
        """(segment, disp) -> sites.  Candidate fixed-base scalars."""
        out = defaultdict(list)
        for s in self.sites:
            if s.is_static:
                out[(s.segment, s.disp)].append(s)
        return dict(out)

    def indexed_clusters(self) -> dict:
        """(segment, base, index) -> sites.  Candidate arrays/structs: the
        displacements within one cluster are the field-offset evidence."""
        out = defaultdict(list)
        for s in self.sites:
            if not s.is_static:
                out[(s.segment, s.base, s.index)].append(s)
        return dict(out)

    def segment_aliases(self) -> dict:
        """disp -> {segment: site count}, for STATIC sites reached through
        more than one segment register.

        A small-model program may
        address the same bytes as ``ds:X`` and ``ss:X``; a region that looks
        tiny by byte extent then has an ownership closure spanning every
        function using either spelling.  Canonicalizing the two is only sound
        where the segment registers are PROVEN equal for the relevant
        lifetime -- equal by memory model is not equal by architecture -- so
        this reports the alias and refuses to merge on its own authority.
        """
        by_disp = defaultdict(lambda: defaultdict(int))
        for s in self.sites:
            if s.is_static:
                by_disp[s.disp][s.segment] += 1
        return {d: dict(segs) for d, segs in by_disp.items()
                if len(segs) > 1}

    def closure(self, disp: int) -> set:
        """Every function PROVABLY touching a static offset, through any
        segment -- the ownership-closure size used to assess whether a region
        is a small, well-bounded ownership candidate.

        This is the DEFINITE half only.  It is computed from expressible
        addresses, so a site whose address this pass cannot express does not
        appear here no matter what it touches.  Any caller deciding ownership
        must also consult :meth:`possible_touchers`; :meth:`closure_verdict`
        pairs them so the incomplete half cannot be forgotten.
        """
        return {s.key for s in self.sites
                if s.is_static and s.disp == disp}

    def possible_touchers(self, segment: str) -> set:
        """Functions that MIGHT touch ``segment`` at an unexpressible offset.

        Implicit-address string instructions (movs/stos/lods/cmps/scas) walk
        ds:si and es:di under si/di/cx the census does not track, so an offset
        cannot be attributed.  Attributing the SEGMENT is still sound and is
        what keeps them from vanishing: a `stosb` cannot reach a ds region
        unless es aliases ds, but a `lodsb` addresses ds directly and could
        reach any offset in it.

        Returning them separately, rather than folding them into closure(), is
        deliberate -- a definite toucher and a possible one call for different
        decisions, and collapsing the two would either overstate the closure
        or hide the doubt.
        """
        return {b.key for b in self.blockers if segment in b.segments}

    def closure_verdict(self, disp: int, segment: str):
        """(definite, possible) -- the whole truth about who touches a region.

        Ownership is only PROVEN when ``possible`` is empty.  When it is not,
        the region has touchers this pass cannot rule out, and promoting it
        needs either a stronger analysis or a port-supplied disjointness fact
        with evidence -- never an assumption that silence means absence.
        """
        return self.closure(disp), self.possible_touchers(segment)


def _segment_of(i) -> str:
    """The segment a memory operand addresses: an explicit override, else the
    default rule (bp-based effective addresses default to SS)."""
    for p in i.prefixes:
        if p in _SEG_PREFIX:
            return _SEG_PREFIX[p]
    if i.modrm is not None and i.mod != 3:
        if i.mod == 0 and i.rm == 6:
            return "ds"                       # disp16 form
        base, _idx = _RM16.get(i.rm, (None, None))
        if base == "bp":
            return "ss"
    return "ds"


#: opcodes whose memory operand is a STORE (reg -> r/m direction, or an
#: explicitly-destination form).  Best-effort and deliberately conservative:
#: a site wrongly called read-only would understate a region's writers.
_STORE_OPS = frozenset({0x8F,
                        0x00, 0x01, 0x08, 0x09, 0x10, 0x11, 0x18, 0x19,
                        0x20, 0x21, 0x28, 0x29, 0x30, 0x31, 0x38, 0x39,
                        0x80, 0x81, 0x83, 0x86, 0x87, 0x88, 0x89, 0x8C,
                        0xA2, 0xA3, 0xC6, 0xC7, 0xD0, 0xD1, 0xD2, 0xD3,
                        0xF6, 0xF7, 0xFE, 0xFF})


#: the implicit-address string instructions, by opcode: mnemonic and the
#: segment REGISTERS each addresses.  They carry no modrm, so they are
#: invisible to modrm-driven site discovery unless named here.
#:
#: The segment half is what makes the resulting blocker useful rather than
#: merely honest.  The offset is unknowable to this pass (si/di walk under cx),
#: but the segment is fixed by the instruction: lods/cmps read ds:si,
#: stos/scas write-or-scan es:di, movs does both.  A source-only op therefore
#: cannot reach an es region, and a dest-only op cannot reach a ds region
#: unless the two segments alias -- which is a fact a port must supply with
#: evidence, not something to assume in either direction.
#:
#: Note these are the DEFAULTS: a segment-override prefix redirects the ds:si
#: side (never the es:di side, which is not overridable on x86).  All 814
#: occurrences in the Lemmings corpus use the defaults, but the override is
#: handled rather than assumed away.
_STRING_OPS = {0xA4: ("movsb", ("ds", "es")), 0xA5: ("movsw", ("ds", "es")),
               0xA6: ("cmpsb", ("ds", "es")), 0xA7: ("cmpsw", ("ds", "es")),
               0xAA: ("stosb", ("es",)),      0xAB: ("stosw", ("es",)),
               0xAC: ("lodsb", ("ds",)),      0xAD: ("lodsw", ("ds",)),
               0xAE: ("scasb", ("es",)),      0xAF: ("scasw", ("es",))}


def sites_of(scan, key: str):
    """Every memory site in one function as (AddressExpr | Blocker)."""
    out = []
    for ip, i in sorted(scan.insts.items()):
        moffs = 0xA0 <= i.op <= 0xA3
        # STRING OPS ADDRESS MEMORY WITHOUT A MODRM.  movs/stos/lods/cmps/scas
        # reach ds:si and/or es:di implicitly, so the `no modrm -> not a memory
        # instruction` shortcut below would drop them SILENTLY -- and a census
        # that under-reports touchers understates an ownership closure, which
        # is exactly the input an ownership decision trusts. Refuse per the
        # refusal-first rule: their addresses are register-driven (si/di walked
        # by cx), so they need a range proof this census cannot supply.
        if i.op in _STRING_OPS:
            mnem, segs = _STRING_OPS[i.op]
            # A segment override redirects the ds:si side only; es:di is not
            # overridable.  Carrying the real segment (rather than the default)
            # keeps possible_touchers() honest for prefixed forms.
            if "ds" in segs:
                src = next((_SEG_PREFIX[p] for p in i.prefixes
                            if p in _SEG_PREFIX), "ds")
                segs = tuple(src if s == "ds" else s for s in segs)
            out.append(Blocker(key, ip,
                               f"implicit-string-access:{mnem}", segs))
            continue
        if not moffs and (i.modrm is None or i.mod == 3):
            continue
        width = ACCESS_WIDTH.get(i.op)
        if width is None:
            out.append(Blocker(key, ip, f"width-unknown:op={i.op:#04x}"))
            continue
        if moffs:
            out.append(AddressExpr(key, ip, _segment_of(i), None, None,
                                   i.imm or 0, width, i.op in _STORE_OPS))
            continue
        if i.mod == 0 and i.rm == 6:
            out.append(AddressExpr(key, ip, _segment_of(i), None, None,
                                   i.disp or 0, width, i.op in _STORE_OPS))
            continue
        shape = _RM16.get(i.rm)
        if shape is None:
            out.append(Blocker(key, ip, f"unmodelled-rm:{i.rm}"))
            continue
        base, index = shape
        out.append(AddressExpr(key, ip, _segment_of(i), base, index,
                               i.disp or 0, width, i.op in _STORE_OPS))
    return out


def build(ir: dict, scan_for) -> Census:
    """The corpus census.  ``scan_for`` is injected (contracts.scan_for) so
    this module stays independent of the IR loading policy."""
    c = Census()
    for key, rec in ir.get("functions", {}).items():
        scan, why = scan_for(rec)
        if scan is None:
            c.blockers.append(Blocker(key, 0, f"unscannable:{why}"))
            continue
        for item in sites_of(scan, key):
            (c.sites if isinstance(item, AddressExpr)
             else c.blockers).append(item)
    return c


#: segment-register loads: where a function BINDS es/ds (memory_schema
#: section 9 pointer provenance).  0x8E /r = mov Sreg, r/m16 (reg field
#: selects the segment register); 0xC4/0xC5 = les/lds; 0x07/0x1F/0x17 =
#: pop seg.  A load from a STATIC address means the segment value lives in
#: a global -- readable from the image, so the region a cluster addresses
#: becomes a decidable fact instead of a runtime mystery.
_SREG_NAMES = {0: "es", 1: "cs", 2: "ss", 3: "ds"}


def seg_loads_of(scan, key: str) -> list:
    """(sreg, source) per segment-register load site.  source is
    ('global', seg, disp) for a static load, ('reg', name) for a register
    move, ('pop'|'les'|'lds', detail) otherwise."""
    out = []
    for ip, i in sorted(scan.insts.items()):
        if i.op == 0x8E and i.modrm is not None:
            sreg = _SREG_NAMES.get(i.reg & 3)
            if i.mod == 3:
                src = ("reg", i.rm)
            elif i.mod == 0 and i.rm == 6:
                src = ("global", _segment_of(i), i.disp)
            else:
                base, idx = _RM16.get(i.rm, (None, None))
                src = ("indexed", base, idx, i.disp)
            out.append((key, ip, sreg, src))
        elif i.op in (0xC4, 0xC5):
            which = "les" if i.op == 0xC4 else "lds"
            sreg = "es" if i.op == 0xC4 else "ds"
            if i.modrm is not None and i.mod == 0 and i.rm == 6:
                out.append((key, ip, sreg, (which, "global", i.disp)))
            else:
                out.append((key, ip, sreg, (which, "dynamic")))
        elif i.op in (0x07, 0x17, 0x1F):
            sreg = {0x07: "es", 0x17: "ss", 0x1F: "ds"}[i.op]
            out.append((key, ip, sreg, ("pop",)))
    return out
