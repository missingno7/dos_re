"""CPU-ABI inference -- the first CPUless de-carrier analysis (M3, stage 2).

The CPUless wall (docs/dos_re_2.0.md section 1a) removes the CPU-shaped
carrier: generated functions stop communicating through emulated registers,
flags, and the machine stack, and use arguments/returns/locals instead.  The
transformation is driven by ANALYSES OVER THE SHARED RECOVERY IR -- never by
parsing generated Python.  This module supplies the foundational analyses:

  * :func:`register_effects` -- per-instruction register read/write sets +
    memory/stack effects for the 16-bit subset the emitter supports, with a
    structured REFUSAL taxonomy for everything else (fail loud, never guess);
  * :func:`abi_scan` -- per-function aggregation: register INPUTS (live-in at
    entry, by backward dataflow over the CFG), register OUTPUTS (every
    register the function may write -- the differential compares the FULL
    register file at boundaries, so scratch writes are observable and must be
    reproduced), stack discipline, and the promotability classification;
  * :func:`classify_corpus` -- the promotion census over a whole recovery IR:
    which functions the CPUless emitter can take TODAY (tier "leaf"), which
    need call-ABI composition, and which need new capabilities (each refusal
    names the missing capability -- the M3 work list).

Everything here is game-agnostic; concrete addresses arrive only inside the
IR being analyzed.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .decode import (SEQ, JMP, JMP_FAR, JCC, CALL, CALL_FAR, CALL_IND,
                     JMP_IND, RET, RETF, IRET, INT, HLT)

# Register names: 16-bit file + segments.  8-bit halves map onto their word
# register (reads/writes of AL count as AX) -- the ABI works in words; the
# emitter refines widths later.
W16 = ("ax", "cx", "dx", "bx", "sp", "bp", "si", "di")
SEGS = ("es", "cs", "ss", "ds")
ALL_REGS = frozenset(W16) | frozenset(SEGS)

#: EA base/index register reads by rm when mod != 3 (16-bit addressing).
_EA_REGS = ({"bx", "si"}, {"bx", "di"}, {"bp", "si"}, {"bp", "di"},
            {"si"}, {"di"}, {"bp"}, {"bx"})


@dataclass
class Effects:
    reads: frozenset = frozenset()
    writes: frozenset = frozenset()
    mem_read: bool = False
    mem_write: bool = False
    stack_delta: int | None = 0        # bytes; None = data-dependent/unknown
    refusal: str | None = None         # non-None: not analyzable yet (why)


def _r8(n: int) -> str:
    return W16[n & 3]                  # AL/CL/DL/BL + AH/CH/DH/BH -> word reg


def _ea(inst, *, w: set[str]) -> Effects:
    """Memory-operand register reads for a ModRM instruction (mod != 3)."""
    if inst.mod == 0 and inst.rm == 6:
        return Effects(reads=frozenset(), writes=frozenset(w))
    return Effects(reads=frozenset(_EA_REGS[inst.rm]), writes=frozenset(w))


def register_effects(inst) -> Effects:  # noqa: C901  (a decode table is a table)
    """Register/memory/stack effects of one decoded instruction.

    Returns an :class:`Effects` whose ``refusal`` names the missing capability
    when the instruction is outside the analyzed subset -- callers must treat
    that as "cannot promote", never as "no effect"."""
    op = inst.op
    R, W = set(), set()
    mr = mw = False
    sd: int | None = 0
    # 16-bit real mode: even opcode = byte form, odd = word form (the classic
    # encoding rule the families below follow; byte halves map to their word
    # register for ABI purposes).
    wide = (op & 1) == 1

    def _ea_segment() -> str:
        # Implicit segment of a memory EA: an override prefix wins; BP-based
        # addressing defaults to SS; everything else to DS.  The segment is an
        # ABI INPUT (the adapter must supply it).
        for p in inst.prefixes:
            if p in (0x26, 0x2E, 0x36, 0x3E):
                return SEGS[(p >> 3) & 3]
        if inst.modrm is not None and inst.mod != 3 and (
                inst.rm in (2, 3) or (inst.rm == 6 and inst.mod != 0)):
            return "ss"                   # BP-based EA defaults to SS
        return "ds"                       # incl. moffs (no ModRM)

    def modrm_rm(wide_write: bool, also_read_rm: bool = False):
        nonlocal mr, mw
        if inst.mod == 3:
            r = W16[inst.rm] if wide else _r8(inst.rm)
            (W if wide_write else R).add(r)
            if also_read_rm:
                R.add(r)
        else:
            if inst.mod == 0 and inst.rm == 6:
                pass                      # direct address: no base/index reads
            else:
                R.update(_EA_REGS[inst.rm])
            R.add(_ea_segment())
            if wide_write:
                mw = True
            if also_read_rm or not wide_write:
                mr = True

    # --- ALU family 00-3B (+ 80-83 group): op r/m,r | r,r/m | acc,imm ------
    if op <= 0x3D and (op & 0x07) <= 0x05 and (op & 0xC7) not in (0x06, 0x07, 0xC6, 0xC7):
        is_cmp = 0x38 <= op <= 0x3D
        form = op & 0x07
        if form in (0, 1):               # r/m op= r
            R.add(_r8(inst.reg) if form == 0 else W16[inst.reg])
            modrm_rm(wide_write=not is_cmp, also_read_rm=True)
            if is_cmp:
                # cmp reads rm; modrm_rm(False, True) marked reads
                pass
        elif form in (2, 3):             # r op= r/m
            tgt = _r8(inst.reg) if form == 2 else W16[inst.reg]
            R.add(tgt)
            if not is_cmp:
                W.add(tgt)
            modrm_rm(wide_write=False)
        else:                            # acc op= imm
            R.add("ax")
            if not is_cmp:
                W.add("ax")
        return Effects(frozenset(R), frozenset(W), mr, mw, sd)
    if op in (0x80, 0x81, 0x83):         # grp1 r/m, imm
        is_cmp = inst.reg == 7
        modrm_rm(wide_write=not is_cmp, also_read_rm=True)
        return Effects(frozenset(R), frozenset(W), mr, mw, sd)

    # --- push/pop -----------------------------------------------------------
    if op in (0x06, 0x0E, 0x16, 0x1E):   # push seg
        R.update({SEGS[(op >> 3) & 3], "sp", "ss"}); W.add("sp"); mw = True
        return Effects(frozenset(R), frozenset(W), mr, True, -2)
    if op in (0x07, 0x17, 0x1F):         # pop seg
        W.add(SEGS[(op >> 3) & 3]); R.update({"sp", "ss"}); W.add("sp")
        return Effects(frozenset(R), frozenset(W), True, mw, +2)
    if 0x50 <= op <= 0x57:
        R.update({W16[op & 7], "sp", "ss"}); W.add("sp")
        return Effects(frozenset(R), frozenset(W), mr, True, -2)
    if 0x58 <= op <= 0x5F:
        W.update({W16[op & 7], "sp"}); R.update({"sp", "ss"})
        return Effects(frozenset(R), frozenset(W), True, mw, +2)
    if op == 0x9C:                       # pushf
        R.update({"sp", "ss"}); W.add("sp")
        return Effects(frozenset(R), frozenset(W), mr, True, -2)
    if op == 0x9D:                       # popf
        R.update({"sp", "ss"}); W.add("sp")
        return Effects(frozenset(R), frozenset(W), True, mw, +2)
    if op == 0x8F and inst.reg == 0:     # pop r/m
        R.update({"sp", "ss"}); W.add("sp")
        modrm_rm(wide_write=True)
        return Effects(frozenset(R), frozenset(W), True, mw, +2)

    # --- inc/dec r16, xchg, mov, lea ---------------------------------------
    if 0x40 <= op <= 0x4F:
        r = W16[op & 7]; R.add(r); W.add(r)
        return Effects(frozenset(R), frozenset(W), mr, mw, sd)
    if op in (0xFE, 0xFF) and inst.reg in (0, 1):     # inc/dec r/m
        modrm_rm(wide_write=True, also_read_rm=True)
        return Effects(frozenset(R), frozenset(W), mr, mw, sd)
    if op == 0xFF and inst.reg == 6:                  # push r/m
        R.update({"sp", "ss"}); W.add("sp")
        modrm_rm(wide_write=False)
        return Effects(frozenset(R), frozenset(W), mr, True, -2)
    if op in (0x86, 0x87):               # xchg r, r/m
        r = _r8(inst.reg) if op == 0x86 else W16[inst.reg]
        R.add(r); W.add(r)
        modrm_rm(wide_write=True, also_read_rm=True)
        return Effects(frozenset(R), frozenset(W), mr, mw, sd)
    if 0x91 <= op <= 0x97:               # xchg ax, r
        r = W16[op & 7]; R.update({r, "ax"}); W.update({r, "ax"})
        return Effects(frozenset(R), frozenset(W), mr, mw, sd)
    if op == 0x90:                       # nop
        return Effects()
    if op in (0x88, 0x89):               # mov r/m, r
        R.add(_r8(inst.reg) if op == 0x88 else W16[inst.reg])
        modrm_rm(wide_write=True)
        return Effects(frozenset(R), frozenset(W), mr, mw, sd)
    if op in (0x8A, 0x8B):               # mov r, r/m
        W.add(_r8(inst.reg) if op == 0x8A else W16[inst.reg])
        modrm_rm(wide_write=False)
        return Effects(frozenset(R), frozenset(W), mr, mw, sd)
    if op == 0x8C:                       # mov r/m, sreg
        R.add(SEGS[inst.reg & 3])
        modrm_rm(wide_write=True)
        return Effects(frozenset(R), frozenset(W), mr, mw, sd)
    if op == 0x8E:                       # mov sreg, r/m
        W.add(SEGS[inst.reg & 3])
        modrm_rm(wide_write=False)
        return Effects(frozenset(R), frozenset(W), mr, mw, sd)
    if op == 0x8D:                       # lea r16, m
        W.add(W16[inst.reg])
        if inst.mod != 3 and not (inst.mod == 0 and inst.rm == 6):
            R.update(_EA_REGS[inst.rm])
        return Effects(frozenset(R), frozenset(W), False, False, sd)
    if 0xB0 <= op <= 0xB7:               # mov r8, imm
        W.add(_r8(op & 7))
        return Effects(frozenset(R), frozenset(W), mr, mw, sd)
    if 0xB8 <= op <= 0xBF:               # mov r16, imm
        W.add(W16[op & 7])
        return Effects(frozenset(R), frozenset(W), mr, mw, sd)
    if op in (0xC6, 0xC7) and inst.reg == 0:          # mov r/m, imm
        modrm_rm(wide_write=True)
        return Effects(frozenset(R), frozenset(W), mr, mw, sd)
    if op in (0xA0, 0xA1):               # mov acc, moffs
        W.add("ax"); R.add(_ea_segment())
        return Effects(frozenset(R), frozenset(W), True, mw, sd)
    if op in (0xA2, 0xA3):               # mov moffs, acc
        R.update({"ax", _ea_segment()})
        return Effects(frozenset(R), frozenset(W), mr, True, sd)
    if op in (0x68, 0x6A):               # push imm (186)
        R.update({"sp", "ss"}); W.add("sp")
        return Effects(frozenset(R), frozenset(W), mr, True, -2)
    if op in (0xC4, 0xC5):               # les/lds r16, m
        W.update({W16[inst.reg], "es" if op == 0xC4 else "ds"})
        if inst.mod != 3 and not (inst.mod == 0 and inst.rm == 6):
            R.update(_EA_REGS[inst.rm])
        return Effects(frozenset(R), frozenset(W), True, mw, sd)

    # --- test / not-neg-mul-div (grp3) / shifts / conversions ---------------
    if op in (0x84, 0x85):               # test r/m, r
        R.add(_r8(inst.reg) if op == 0x84 else W16[inst.reg])
        modrm_rm(wide_write=False)
        return Effects(frozenset(R), frozenset(W), mr, mw, sd)
    if op in (0xA8, 0xA9):               # test acc, imm
        R.add("ax")
        return Effects(frozenset(R), frozenset(W), mr, mw, sd)
    if op in (0xF6, 0xF7):
        grp = inst.reg
        if grp == 0:                     # test r/m, imm
            modrm_rm(wide_write=False)
        elif grp in (2, 3):              # not/neg r/m
            modrm_rm(wide_write=True, also_read_rm=True)
        elif grp in (4, 5):              # mul/imul
            modrm_rm(wide_write=False)
            R.add("ax"); W.update({"ax", "dx"} if op == 0xF7 else {"ax"})
        elif grp in (6, 7):              # div/idiv
            modrm_rm(wide_write=False)
            R.update({"ax"} if op == 0xF6 else {"ax", "dx"})
            W.update({"ax"} if op == 0xF6 else {"ax", "dx"})
        return Effects(frozenset(R), frozenset(W), mr, mw, sd)
    if op in (0xC0, 0xC1, 0xD0, 0xD1, 0xD2, 0xD3):    # shifts/rotates
        if op in (0xD2, 0xD3):
            R.add("cx")
        modrm_rm(wide_write=True, also_read_rm=True)
        return Effects(frozenset(R), frozenset(W), mr, mw, sd)
    if op == 0x98:                       # cbw
        R.add("ax"); W.add("ax")
        return Effects(frozenset(R), frozenset(W), mr, mw, sd)
    if op == 0x99:                       # cwd
        R.add("ax"); W.add("dx")
        return Effects(frozenset(R), frozenset(W), mr, mw, sd)
    if op in (0x9E,):                    # sahf
        R.add("ax")
        return Effects(frozenset(R), frozenset(W), mr, mw, sd)
    if op in (0x9F,):                    # lahf
        W.add("ax")
        return Effects(frozenset(R), frozenset(W), mr, mw, sd)
    if op == 0xD7:                       # xlat
        R.update({"ax", "bx"}); W.add("ax")
        return Effects(frozenset(R), frozenset(W), True, mw, sd)
    if op in (0xF5, 0xF8, 0xF9, 0xFA, 0xFB, 0xFC, 0xFD):  # cmc/clc/stc/cli/sti/cld/std
        return Effects()

    # --- string ops ----------------------------------------------------------
    if 0xA4 <= op <= 0xA7 or 0xAA <= op <= 0xAF:
        R.update({"si", "di", "ds", "es"} if op in (0xA4, 0xA5, 0xA6, 0xA7)
                 else ({"di", "es", "ax"} if op in (0xAA, 0xAB, 0xAE, 0xAF)
                       else {"si", "ds"}))
        W.update({"si", "di"} if op in (0xA4, 0xA5, 0xA6, 0xA7)
                 else ({"di"} if op in (0xAA, 0xAB, 0xAE, 0xAF) else {"si", "ax"}))
        if any(p in (0xF2, 0xF3) for p in inst.prefixes):
            R.add("cx"); W.add("cx")
        mr = op not in (0xAA, 0xAB)
        mw = op in (0xA4, 0xA5, 0xAA, 0xAB)
        return Effects(frozenset(R), frozenset(W), mr, mw, sd)

    # --- control flow ---------------------------------------------------------
    if inst.kind == JCC:
        if op in (0xE0, 0xE1, 0xE2):     # loop/loopz/loopnz decrement CX
            R.add("cx"); W.add("cx")
        elif op == 0xE3:                 # jcxz reads CX
            R.add("cx")
        return Effects(frozenset(R), frozenset(W), mr, mw, sd)
    if inst.kind in (JMP, JMP_FAR):
        return Effects()
    if inst.kind == RET:
        R.update({"sp", "ss"}); W.add("sp")
        n = 2 + ((inst.imm or 0) if op == 0xC2 else 0)
        return Effects(frozenset(R), frozenset(W), True, mw, +n)
    if inst.kind in (RETF, IRET):
        R.update({"sp", "ss"}); W.add("sp")
        return Effects(frozenset(R), frozenset(W), True, mw,
                       +4 if inst.kind == RETF else +6)
    if inst.kind == CALL:
        # Linked near call: callee ABI composes at a higher level; the call
        # itself pushes/pops the return address symmetrically.
        return Effects(frozenset({"sp", "ss"}), frozenset({"sp"}), True, True, 0,
                       refusal="call-abi-composition")
    if inst.kind in (CALL_FAR, CALL_IND, JMP_IND):
        return Effects(refusal="indirect-or-far-transfer")
    if inst.kind == INT:
        return Effects(refusal="int-platform-effect")
    if inst.kind == HLT:
        return Effects(refusal="hlt")
    if op in (0xE4, 0xE5, 0xE6, 0xE7, 0xEC, 0xED, 0xEE, 0xEF):
        return Effects(refusal="port-io-platform-effect")

    return Effects(refusal=f"unanalyzed-opcode-{op:02X}")


# ---------------------------------------------------------------------------

@dataclass
class AbiReport:
    entry: int
    inputs: frozenset            # registers live-in at entry
    outputs: frozenset           # registers the function may write
    reads_mem: bool
    writes_mem: bool
    max_stack_use: int | None    # deepest net push depth seen (bytes), None=unknown
    refusals: dict = field(default_factory=dict)   # capability -> [ip, ...]
    tier: str = ""               # "leaf" | "calls-only" | "blocked"

    def as_json(self) -> dict:
        return {"entry": f"{self.entry:04X}",
                "inputs": sorted(self.inputs), "outputs": sorted(self.outputs),
                "reads_mem": self.reads_mem, "writes_mem": self.writes_mem,
                "max_stack_use": self.max_stack_use,
                "refusals": {k: [f"{ip:04X}" for ip in v]
                             for k, v in sorted(self.refusals.items())},
                "tier": self.tier}


def _successors(scan) -> dict[int, list[int]]:
    succ: dict[int, list[int]] = {}
    by_ip = scan.insts
    for i in scan.insts.values():
        s: list[int] = []
        if i.kind in (SEQ, CALL, CALL_FAR, CALL_IND, INT):
            s.append(i.next_ip)
        elif i.kind == JCC:
            s.append(i.next_ip)
            if i.target is not None:
                s.append(i.target)
        elif i.kind == JMP and i.target is not None:
            s.append(i.target)
        # RET/RET_FAR/IRET/JMP_FAR/JMP_IND: no in-function successor
        succ[i.ip] = [t for t in s if t in by_ip]
    return succ


def abi_scan(scan) -> AbiReport:
    """Infer the CPU ABI of one scanned function from its instruction list.

    Register INPUTS = live-in at entry by backward may-liveness over the
    in-function CFG (a register read on SOME path before being written).
    OUTPUTS = every register any instruction may write (the boundary
    differential observes the full register file, so scratch counts).
    Refusal sites are aggregated by capability -- the M3 work list."""
    effs = {i.ip: register_effects(i) for i in scan.insts.values()}
    succ = _successors(scan)
    refusals: dict[str, list[int]] = {}
    for ip, e in effs.items():
        if e.refusal:
            refusals.setdefault(e.refusal, []).append(ip)

    # Backward may-liveness (sets grow to fixpoint).
    live_out: dict[int, frozenset] = {ip: frozenset() for ip in effs}
    changed = True
    while changed:
        changed = False
        for i in sorted(scan.insts.values(), key=lambda x: -x.ip):
            out = frozenset().union(*(
                (live_out[s] - effs[s].writes) | effs[s].reads
                for s in succ[i.ip])) if succ[i.ip] else frozenset()
            if out != live_out[i.ip]:
                live_out[i.ip] = out
                changed = True
    entry = scan.entry
    e0 = effs[entry]
    inputs = (live_out[entry] - e0.writes) | e0.reads
    outputs = frozenset().union(*(e.writes for e in effs.values())) if effs else frozenset()

    # Stack use: DFS over the CFG tracking net push depth in bytes (delta<0
    # grows the stack).  Unknown (None) when any reachable instruction has a
    # data-dependent delta or a refusal hides its stack behavior.
    max_use: int | None = 0
    depth: dict[int, int] = {entry: 0}
    work = [entry]
    while work:
        ip = work.pop()
        e = effs[ip]
        if e.stack_delta is None or (e.refusal and e.refusal.startswith("unanalyzed")):
            max_use = None
            break
        after = depth[ip] - e.stack_delta          # bytes currently pushed
        if max_use is not None:
            max_use = max(max_use, after, depth[ip])
        for s in succ[ip]:
            if s not in depth:
                depth[s] = after
                work.append(s)

    blocked = {k: v for k, v in refusals.items() if k != "call-abi-composition"}
    tier = ("leaf" if not refusals
            else "calls-only" if not blocked
            else "blocked")
    return AbiReport(entry=entry, inputs=inputs, outputs=outputs,
                     reads_mem=any(e.mem_read for e in effs.values()),
                     writes_mem=any(e.mem_write for e in effs.values()),
                     max_stack_use=max_use, refusals=refusals, tier=tier)


def classify_corpus(ir: dict) -> dict:
    """The promotion census over a whole recovery IR: per-function ABI reports
    plus the tier summary (the M3 work list, most-promotable first)."""
    from .ir import scan_from_ir_record

    reports: dict[str, AbiReport] = {}
    for key, rec in sorted(ir["functions"].items()):
        if not rec.get("liftable", True):
            r = AbiReport(entry=int(key.split(":")[1], 16), inputs=frozenset(),
                          outputs=frozenset(), reads_mem=True, writes_mem=True,
                          max_stack_use=None,
                          refusals={"ir-not-liftable": [0]}, tier="blocked")
            reports[key] = r
            continue
        scan = scan_from_ir_record(rec)
        reports[key] = abi_scan(scan)
    tiers: dict[str, list[str]] = {"leaf": [], "calls-only": [], "blocked": []}
    for key, r in reports.items():
        tiers[r.tier].append(key)
    capability_counts: dict[str, int] = {}
    for r in reports.values():
        for cap, ips in r.refusals.items():
            capability_counts[cap] = capability_counts.get(cap, 0) + len(ips)
    return {
        "_notice": "GENERATED by dos_re.lift.cpuless.classify_corpus -- the "
                   "M3 promotion census. Regenerate, do not hand-edit.",
        "tiers": {k: sorted(v) for k, v in tiers.items()},
        "tier_counts": {k: len(v) for k, v in tiers.items()},
        "missing_capabilities": dict(sorted(capability_counts.items(),
                                            key=lambda kv: -kv[1])),
        "functions": {k: r.as_json() for k, r in reports.items()},
    }
