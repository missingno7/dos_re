"""Deterministic analyses over lifted functions.

First pass: FLAG LIVENESS (backward dataflow over the function CFG), feeding
the emitter's first de-carrier transformation — eliding flag computations
that nothing can observe.  The soundness rule is seam-conservatism:

    Flags are OBSERVABLE at every seam — calls, far calls, indirect calls,
    INTs, interpreter fallbacks, string ops, and every exit.  A boundary park
    can unwind out of a callee mid-function and expose the whole machine
    state to the end-to-end oracle digest, so a flag write is dead ONLY if a
    provably-flag-overwriting instruction executes before ANY seam or
    consumer on EVERY path.

The effect model is deliberately asymmetric, which keeps it sound by default:

    * kill sets (flags GUARANTEED overwritten) are enumerated exactly, only
      for instructions the model knows;
    * read sets are over-approximated — any instruction the model does not
      know is treated as reading ALL flags (and killing none), so unknown
      opcodes never enable an elision around them.

Analyses run on ``FunctionScan`` objects, which Recovery IR re-elaborates via
``dos_re.lift.ir``; generated Python is never parsed as an authority.
"""
from __future__ import annotations

from .cfg import FunctionScan
from .decode import CALL, CALL_FAR, CALL_IND, INT, JCC, JMP, SEQ

#: the 8086 status flags the emitter's helpers write
ALL_FLAGS = frozenset(("CF", "PF", "AF", "ZF", "SF", "OF", "DF", "IF"))
#: exact helper kill sets, mirroring cpu.py's clear masks — NEVER widened:
#: an over-claimed kill silently un-lives a flag and produces wrong state.
_ADDSUB6 = frozenset(("CF", "PF", "AF", "ZF", "SF", "OF"))   # set_add/sub_flags
_LOGIC5 = frozenset(("CF", "PF", "ZF", "SF", "OF"))          # set_logic_flags KEEPS AF
_INCDEC5 = frozenset(("PF", "AF", "ZF", "SF", "OF"))         # set_incdec_flags keeps CF

#: JCC condition nibble -> flags read (Intel cc encoding; low bit inverts)
_CC_READS = {
    0x0: frozenset(("OF",)),                # o / no
    0x2: frozenset(("CF",)),                # b / ae
    0x4: frozenset(("ZF",)),                # e / ne
    0x6: frozenset(("CF", "ZF")),           # be / a
    0x8: frozenset(("SF",)),                # s / ns
    0xA: frozenset(("PF",)),                # p / np
    0xC: frozenset(("SF", "OF")),           # l / ge
    0xE: frozenset(("SF", "OF", "ZF")),     # le / g
}


def _jcc_reads(inst) -> frozenset:
    op = inst.op
    if 0x70 <= op <= 0x7F:
        return _CC_READS[(op - 0x70) & 0xE]
    if op in (0xE0, 0xE1):                  # loopnz / loopz
        return frozenset(("ZF",))
    if op in (0xE2, 0xE3):                  # loop / jcxz: CX only
        return frozenset()
    return ALL_FLAGS                        # unknown conditional: conservative


def flag_effects(inst) -> tuple[frozenset, frozenset, bool]:
    """(reads, exact_kills, is_seam) for one instruction.

    ``exact_kills`` lists flags the instruction GUARANTEES to overwrite (the
    emitter's own helper set); unknown instructions read everything and kill
    nothing.  ``is_seam`` marks points where machine state escapes (flags
    become observable regardless of local dataflow)."""
    op = inst.op
    kind = inst.kind
    if kind in (CALL, CALL_FAR, CALL_IND, INT):
        return ALL_FLAGS, frozenset(), True
    if kind == JCC:
        return _jcc_reads(inst), frozenset(), False
    if kind != SEQ:                          # terminators: state escapes
        return ALL_FLAGS, frozenset(), True

    # --- SEQ instructions the emitter emits natively -----------------------
    if op < 0x40 and (op & 0x07) <= 5:       # ALU r/m,reg | reg,r/m | acc,imm
        group = (op >> 3) & 7
        reads = frozenset(("CF",)) if group in (2, 3) else frozenset()  # adc/sbb
        kills = _LOGIC5 if group in (1, 4, 6) else _ADDSUB6   # or/and/xor keep AF
        return reads, kills, False
    if op in (0xF8, 0xF9):                   # clc / stc
        return frozenset(), frozenset(("CF",)), False
    if op == 0xF5:                           # cmc
        return frozenset(("CF",)), frozenset(("CF",)), False
    if op in (0xFC, 0xFD):                   # cld / std
        return frozenset(), frozenset(("DF",)), False
    if op in (0xFA, 0xFB):                   # cli / sti
        return frozenset(), frozenset(("IF",)), False
    if op in (0x80, 0x81, 0x82, 0x83):       # ALU r/m,imm
        group = inst.reg
        reads = frozenset(("CF",)) if group in (2, 3) else frozenset()
        kills = _LOGIC5 if group in (1, 4, 6) else _ADDSUB6
        return reads, kills, False
    if op in (0x84, 0x85, 0xA8, 0xA9):       # test (set_logic_flags: keeps AF)
        return frozenset(), _LOGIC5, False
    if 0x40 <= op <= 0x4F:                   # inc/dec r16
        return frozenset(), _INCDEC5, False
    if op in (0xFE, 0xFF) and inst.reg in (0, 1):   # inc/dec r/m
        return frozenset(), _INCDEC5, False
    if op in (0x88, 0x89, 0x8A, 0x8B, 0x8C, 0x8E,   # mov family
              0xA0, 0xA1, 0xA2, 0xA3, 0xC6, 0xC7) or 0xB0 <= op <= 0xBF:
        return frozenset(), frozenset(), False
    if op in (0x86, 0x87) or 0x90 <= op <= 0x97:    # xchg / nop
        return frozenset(), frozenset(), False
    if op == 0x8D or op in (0xC4, 0xC5):            # lea / les / lds
        return frozenset(), frozenset(), False
    if 0x50 <= op <= 0x5F or op in (0x06, 0x0E, 0x16, 0x1E,
                                    0x07, 0x17, 0x1F, 0x68, 0x6A, 0x8F):
        return frozenset(), frozenset(), False      # push/pop family
    if op == 0x98 or op == 0x99 or op == 0xD7:      # cbw / cwd / xlat
        return frozenset(), frozenset(), False
    # Everything else (shifts, grp3, string ops, flag ops, pushf/popf, in/out,
    # interp_one fallbacks): reads-all, kills-nothing — sound, never elided
    # around.  Shifts/grp3 DO write flags, but their helpers compute flags
    # internally with the result, so their sites are not elidable anyway and
    # under-claiming their kills only costs precision, never soundness.
    return ALL_FLAGS, frozenset(), False


def dead_flag_sites(scan: FunctionScan) -> set[int]:
    """IPs whose emitted flag-write line can be elided (seam-conservative).

    Backward dataflow to fixpoint over the block CFG: live-out of a block is
    the union of successors' live-in; exits and seams are live-ALL."""
    from .decode import RET, RETF, IRET, JMP_FAR, JMP_IND

    leaders = scan.block_leaders()
    leader_set = set(leaders)
    blocks: dict[int, list] = {}
    succs: dict[int, list[int]] = {}
    for leader in leaders:
        body = []
        ip = leader
        while True:
            inst = scan.insts[ip]
            body.append(inst)
            if inst.kind not in (SEQ, CALL, CALL_FAR, CALL_IND, INT):
                break
            nxt = inst.next_ip
            if nxt in leader_set or nxt not in scan.insts:
                break
            ip = nxt
        blocks[leader] = body
        term = body[-1]
        out: list[int] = []
        if term.kind == JCC:
            if term.target in leader_set:
                out.append(term.target)
            if term.next_ip in leader_set:
                out.append(term.next_ip)
        elif term.kind == JMP:
            if term.target in leader_set:
                out.append(term.target)
        elif term.kind in (RET, RETF, IRET, JMP_FAR, JMP_IND):
            pass                              # exit: live-ALL handled below
        else:                                 # fell through into next leader
            if term.next_ip in leader_set:
                out.append(term.next_ip)
        succs[leader] = out

    live_in: dict[int, frozenset] = {b: frozenset() for b in leaders}

    def block_live_out(leader: int) -> frozenset:
        term = blocks[leader][-1]
        if term.kind in (RET, RETF, IRET, JMP_FAR, JMP_IND):
            return ALL_FLAGS                  # state escapes at every exit
        out: frozenset = frozenset()
        for s in succs[leader]:
            out |= live_in[s]
        # A terminator that leaves the region (jmp to non-leader) is an
        # escape too; succs empty for non-exit means exactly that.
        if not succs[leader]:
            out = ALL_FLAGS
        return out

    changed = True
    while changed:
        changed = False
        for leader in leaders:
            live = block_live_out(leader)
            for inst in reversed(blocks[leader]):
                reads, kills, seam = flag_effects(inst)
                live = (live - kills) | reads
                if seam:
                    live = ALL_FLAGS
            if live != live_in[leader]:
                live_in[leader] = live
                changed = True

    dead: set[int] = set()
    for leader in leaders:
        live = block_live_out(leader)
        for inst in reversed(blocks[leader]):
            reads, kills, seam = flag_effects(inst)
            if kills and not (kills & live):
                dead.add(inst.ip)
            live = (live - kills) | reads
            if seam:
                live = ALL_FLAGS
    return dead
