"""Function-region discovery for the lifter: blocks, exits, calls, refusals.

``scan_function`` walks every statically reachable instruction from an entry
offset, following fallthrough and direct near branches. Direct/indirect calls
and INTs do NOT extend the region (callees run through the VM at execution
time — docs/lifting_design.md §6); they are recorded as external
dependencies. The result is either a liftable region description or a
structured refusal list that analysis and generation tools can retain.

An optional ``probe`` callback cross-checks each decoded instruction length
against the interpreter (the authority). The walker itself stays OS-free and
pure: it sees code bytes only through ``fetch``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .cpuless import register_effects
from .decode import (CALL, CALL_FAR, CALL_IND, HLT, INT, IRET, JCC, JMP,
                     JMP_FAR, JMP_IND, RET, RETF, SEQ, UNSUPPORTED, Inst,
                     decode_one)

#: kinds that terminate a path (function exits).  An indirect jump ends the
#: region as a TAIL EXIT (the 32-bit emitter uses the same treatment): the emitted
#: hook computes the runtime target, sets CS:IP, and hands control back to the
#: VM — a dispatcher lifts as prologue + tail transfer, its cases stay
#: interpreted (and re-enter any hook installed at them).  Observed need:
#: Lemmings' sound-driver dispatcher (jmp rm16) and an ISR chaining to the
#: previous vector (jmp far [old_vec]).
EXIT_KINDS = (RET, RETF, IRET, JMP_FAR, JMP_IND)


@dataclass
class Refusal:
    ip: int
    reason: str          # stable slug, e.g. "indirect-jump", "unsupported-opcode"
    detail: str = ""


@dataclass
class FunctionScan:
    entry: int
    insts: dict[int, Inst] = field(default_factory=dict)   # ip -> Inst (reachable set)
    exits: list[Inst] = field(default_factory=list)
    calls_near: set[int] = field(default_factory=set)      # static near-call targets
    calls_far: set[tuple[int, int]] = field(default_factory=set)
    calls_indirect: list[int] = field(default_factory=list)   # call sites (ips)
    ints: set[int] = field(default_factory=set)             # int numbers used
    refusals: list[Refusal] = field(default_factory=list)
    probe_unchecked: list[int] = field(default_factory=list)  # probe couldn't execute there
    #: (site_ip, target_off) for every CS-override DIRECT-address store in the
    #: region -- statically-visible writes into the code segment.  A target
    #: inside this function's own bytes is SELF-MODIFYING CODE (refused below);
    #: a target inside ANOTHER censused function refuses THAT function at the
    #: whole-document level (irgen_core.build_document) -- either way a lift of
    #: the patched bytes would silently freeze one snapshot's operands into
    #: code the program retunes at runtime (observed: SkyRoads' LZS decoder
    #: patches its per-file bit-width immediates into its own body).
    cs_store_targets: list[tuple[int, int]] = field(default_factory=list)
    #: boundary-head IPs found inside the reachable set (subset of the caller's
    #: declared ``--boundary-heads``).  A boundary head is a scheduler-yield
    #: point (docs/recovery_ir.md): the top-level frame/event
    #: loop yields there each frame instead of returning.  A function whose only
    #: terminating construct is a boundary head is a legitimately non-returning
    #: COROUTINE, not a dead end -- so its presence suppresses the ``no-exit``
    #: refusal (the emitter's resume machinery, emit.py, turns the head into a
    #: boundary event + ResumePoint).  Empty unless the caller declares heads.
    boundary_heads: list[int] = field(default_factory=list)
    #: RESUME points of MANUFACTURED-RETURN sites (``push <addr> ; jmp <indirect>``,
    #: see :func:`manufactured_return`).  The dispatched arm's ``ret`` lands here,
    #: back inside this function, so these are control-flow ARRIVALS: the walk must
    #: follow them or the whole continuation is invisible, and they are forced block
    #: leaders exactly as a dynamic-dispatch alternate entry is.
    manufactured_returns: set[int] = field(default_factory=set)

    @property
    def liftable(self) -> bool:
        return not self.refusals and (bool(self.exits) or bool(self.boundary_heads))

    @property
    def region(self) -> tuple[int, int]:
        """(lo, hi_exclusive) span of the reachable set — report only; the set
        itself is authoritative (regions may be discontiguous)."""
        if not self.insts:
            return (self.entry, self.entry)
        lo = min(self.insts)
        hi = max(i.ip + i.length for i in self.insts.values())
        return (lo, hi)

    def block_leaders(self) -> list[int]:
        leaders = {self.entry} | set(self.manufactured_returns)
        for inst in self.insts.values():
            if inst.kind in (JCC, JMP) and inst.target is not None:
                leaders.add(inst.target)
                if inst.kind == JCC:
                    leaders.add(inst.next_ip)
        return sorted(leaders & set(self.insts))

    def leader_of(self, extra_leaders=frozenset()) -> dict[int, int]:
        """Map every reached ip to the leader of its basic block.

        ``extra_leaders`` are additional FORCED leaders the caller knows about
        (the emitter forces dynamic-dispatch arrival points), so the caller and
        the scan agree on where blocks begin -- a divergent leader set would let
        them disagree about whether a given jmp is a computed call."""
        leaders = set(self.block_leaders()) | set(extra_leaders)
        out: dict[int, int] = {}
        for lead in sorted(leaders):
            p = lead
            while p in self.insts:
                out[p] = lead
                nxt = self.insts[p].next_ip
                if nxt in leaders or nxt not in self.insts \
                        or self.insts[p].kind not in (SEQ, CALL, CALL_FAR,
                                                      CALL_IND):
                    break
                p = nxt
        return out


#: Opcodes that WRITE their modrm r/m operand unconditionally (rm,reg ALU
#: forms, mov/xchg stores, shifts, pop rm).  Sub-op-dependent writers (80/81/83,
#: C6/C7, F6/F7, FE/FF, 8F) are resolved in :func:`cs_direct_store_target`.
_RM_WRITE_OPS = frozenset({
    0x00, 0x01, 0x08, 0x09, 0x10, 0x11, 0x18, 0x19,   # add/or/adc/sbb rm,reg
    0x20, 0x21, 0x28, 0x29, 0x30, 0x31,               # and/sub/xor rm,reg
    0x86, 0x87, 0x88, 0x89,                           # xchg / mov rm,reg
    0xC0, 0xC1, 0xD0, 0xD1, 0xD2, 0xD3,               # shifts/rotates rm
})


def cs_direct_store_target(inst: Inst) -> int | None:
    """The code-segment offset this instruction stores to, if it is a
    STATICALLY-RESOLVABLE code write: a CS-override (0x2E) store whose memory
    operand is a direct 16-bit address (moffs, or modrm mod=00 rm=110).
    ``None`` for everything else.  Indirect/indexed code writes and writes via
    a data segment that happens to alias the code segment are out of static
    reach -- this catches the pattern real 16-bit games use for in-place
    operand patching (``mov cs:[imm16], al``)."""
    if 0x2E not in inst.prefixes:
        return None
    op = inst.op
    if op in (0xA2, 0xA3):                      # mov moffs8/16, al/ax
        return None if inst.imm is None else inst.imm & 0xFFFF
    if inst.modrm is None or (inst.modrm >> 6) != 0 or (inst.modrm & 7) != 6:
        return None                             # not a direct-address operand
    sub = (inst.modrm >> 3) & 7
    writes = (op in _RM_WRITE_OPS
              or (op in (0x80, 0x81, 0x83) and sub != 7)     # imm ALU, not cmp
              or (op in (0xC6, 0xC7) and sub == 0)           # mov rm,imm
              or (op in (0xF6, 0xF7) and sub in (2, 3))      # not/neg
              or (op == 0xFE and sub in (0, 1))              # inc/dec rm8
              or (op == 0xFF and sub in (0, 1))              # inc/dec rm16
              or (op == 0x8F and sub == 0))                  # pop rm16
    if not writes or inst.disp is None:
        return None
    return inst.disp & 0xFFFF


def inst_byte_offsets(scan: "FunctionScan") -> set[int]:
    """Every code-segment byte offset occupied by the scan's instructions."""
    out: set[int] = set()
    for i in scan.insts.values():
        out.update((i.ip + k) & 0xFFFF for k in range(i.length))
    return out


#: general-register index -> name, for `push r16` / `mov r16, imm16` (opcode low 3 bits).
_GPR16 = ("ax", "cx", "dx", "bx", "sp", "bp", "si", "di")


def manufactured_return(scan, jmp, leader) -> int | None:
    """The MANUFACTURED-RETURN idiom: ``push <code addr> ; ... ; jmp <indirect>``.

    Returns the pushed offset when this near indirect JMP is a computed CALL --
    the block put a return address on the stack itself -- and ``None`` when it is
    an ordinary tail transfer.

    WHY IT MATTERS.  A near indirect jmp is normally a TAIL: the dispatched arm's
    ``ret`` is the container's exit, popping the container's caller's frame.  That
    is only true when nothing of ours is on top of the stack.  A ``ret`` returns to
    WHATEVER IS ON TOP -- so if the block pushed a word first, the arm returns
    THERE, back inside this function, and both the CFG walk and the emitter must
    follow it.  Read as a tail, the continuation is invisible: never scanned, never
    emitted, silently dropped.

    This is a general x86 construct, not one program's quirk.  It is how a CALL
    with a COMPUTED target is written -- ``call rel16`` cannot express one -- so
    the return address is manufactured by hand and each arm's own ``ret`` delivers
    control back.

    RECOGNISED TIGHTLY: a positive match on the idiom, never an inference from
    stack depth.  Depth cannot discriminate it -- the FRAMELESS STACK-ARG tail (a
    dispatcher that pushes ARGUMENTS which the arms pop before returning) also sits
    at nonzero depth and IS a genuine tail.  What separates them is the pushed
    VALUE being a statically-known code offset.  So the window from the block
    leader to the jmp must hold EXACTLY ONE stack-affecting instruction, and it
    must push a value known statically:

      * ``push imm16`` (0x68), or
      * ``push r16`` (0x50+r) whose register was last set by ``mov r16, imm16``
        (0xB8+r) earlier in the same block and not written since.

    Anything else returns ``None`` and keeps the existing tail behaviour, because
    it is not this idiom.  What a CALLER does with a recognised offset that is not
    part of the function is the caller's decision -- it must not silently tail."""
    if jmp.kind != JMP_IND or leader is None:
        return None                         # a CALL_IND pushes its own next_ip
    insts = scan.insts
    imm_regs: dict[int, int] = {}           # gpr index -> literal it holds
    pushed: int | None = None
    stack_ops = 0
    ip = leader
    while ip != jmp.ip:
        i = insts.get(ip)
        if i is None:                       # the block does not reach the jmp
            return None
        e = register_effects(i)
        if e.stack_delta:
            stack_ops += 1
            if i.op == 0x68 and i.imm is not None:
                pushed = i.imm & 0xFFFF
            elif 0x50 <= i.op <= 0x57:
                pushed = imm_regs.get(i.op - 0x50)
            else:
                pushed = None
        if 0xB8 <= i.op <= 0xBF and i.imm is not None and i.modrm is None:
            imm_regs[i.op - 0xB8] = i.imm & 0xFFFF       # `mov r16, imm16`
        else:
            for r in tuple(imm_regs):                    # any other write kills it
                if _GPR16[r] in e.writes:
                    del imm_regs[r]
        ip = i.next_ip
    if stack_ops != 1:
        return None
    return pushed


def scan_function(fetch: Callable[[int], int], entry: int, *,
                  max_insts: int = 4096, max_bytes: int = 16384,
                  probe: Callable[[int], int | None] | None = None,
                  boundary_heads: "frozenset[int] | None" = None) -> FunctionScan:
    """Discover the statically reachable region of the function at ``entry``.

    ``probe(ip)`` (optional) returns the interpreter-measured IP-DELTA of one
    ``step()`` at ``ip``, or None when the interpreter could not execute there
    (recorded, not fatal). Only non-transfer (SEQ) instructions are probed:
    for those, delta == encoded length (every decode/operand fetch advances
    ``s.ip`` byte-by-byte, including the interpreter's inlined fast paths),
    so a successful probe that disagrees with the static decode is fatal —
    either an operand-length bug or a transfer misclassified as SEQ. Transfer
    encodings are fixed-size and covered by the decoder's unit tests.

    ``boundary_heads`` (optional) is the set of scheduler-yield IPs the caller
    declared for THIS segment.  When the walk finds no ret/retf/iret/far/indirect
    exit but the reachable set contains a boundary head, the function is a
    boundary-delimited COROUTINE loop (the top-level frame/event loop that every
    DOS game has: it yields one frame at the boundary instead of returning) — so
    the ``no-exit`` refusal is suppressed and the function stays liftable, its
    boundary head(s) recorded for the emitter's resume machinery.  Without this a
    selected generated graph cannot cover that loop.
    """
    heads = boundary_heads or frozenset()
    scan = FunctionScan(entry=entry)
    work = [entry]
    budget_hit = False
    # OUTER FIXPOINT.  A MANUFACTURED RETURN (`push <addr> ; jmp <indirect>`) is a
    # control-flow ARRIVAL back inside this function -- the dispatched arm's `ret`
    # lands on the pushed word.  It cannot be seen while the worklist is running
    # (recognising it needs the block structure the walk is still building), so
    # after the walk settles we look for such sites, enqueue any resume point not
    # yet reached, and walk again.  Without this the entire continuation is
    # invisible to every later stage: not scanned, not emitted, silently dropped.
    while True:
        while work:
            ip = work.pop() & 0xFFFF
            if ip in scan.insts:
                continue
            if len(scan.insts) >= max_insts:
                budget_hit = True
                break
            inst = decode_one(fetch, ip)
            scan.insts[ip] = inst

            if probe is not None and inst.kind == SEQ:
                measured = probe(ip)
                if measured is None:
                    scan.probe_unchecked.append(ip)
                elif measured != inst.length:
                    scan.refusals.append(Refusal(
                        ip, "decoder-mismatch",
                        f"static={inst.length} interpreter-delta={measured} bytes={inst.raw.hex()}"))
                    continue

            kind = inst.kind
            if kind == UNSUPPORTED:
                scan.refusals.append(Refusal(ip, "unsupported-opcode",
                                             f"{inst.mnemonic} bytes={inst.raw.hex()}"))
                continue
            if kind == HLT:
                scan.refusals.append(Refusal(ip, "hlt", ""))
                continue

            if kind in EXIT_KINDS:
                scan.exits.append(inst)
                continue
            if kind == SEQ:
                work.append(inst.next_ip)
            elif kind == JCC:
                work.append(inst.next_ip)
                work.append(inst.target)          # type: ignore[arg-type]
            elif kind == JMP:
                work.append(inst.target)          # type: ignore[arg-type]
            elif kind == CALL:
                scan.calls_near.add(inst.target)  # type: ignore[arg-type]
                work.append(inst.next_ip)
            elif kind == CALL_FAR:
                scan.calls_far.add(inst.far_target)  # type: ignore[arg-type]
                work.append(inst.next_ip)
            elif kind == CALL_IND:
                scan.calls_indirect.append(ip)
                work.append(inst.next_ip)
            elif kind == INT:
                if inst.int_no is not None:
                    scan.ints.add(inst.int_no)
                work.append(inst.next_ip)
        if budget_hit:
            break
        # Look for MANUFACTURED RETURNS now that the walk has settled and the
        # block structure exists.  A recognised resume point is recorded (it is a
        # forced block leader from here on, exactly like a dynamic-dispatch
        # arrival) and, when it has not been reached yet, enqueued for another
        # pass -- the continuation it opens may itself contain such a site.
        # The resume point is code that runs AFTER the dispatch, so it cannot lie
        # inside the straight-line window this recogniser reads; forcing it as a
        # leader therefore cannot invalidate the match that found it.
        leader_of = scan.leader_of()
        fresh = []
        for jip, jinst in list(scan.insts.items()):
            if jinst.kind != JMP_IND:
                continue
            t = manufactured_return(scan, jinst, leader_of.get(jip))
            if t is None:
                continue
            scan.manufactured_returns.add(t)
            if t not in scan.insts:
                fresh.append(t)
        if not fresh:
            break
        work = fresh

    lo, hi = scan.region
    # Budget on DECODED bytes, not the lo..hi span: regions may legitimately
    # be discontiguous (a small function tail-jumping to a shared far tail —
    # Lemmings' per-frame 1010:3944, 39 insts across a 17KB span).  The
    # runaway protection is the instruction budget + the decoder cross-check;
    # span alone punished real functions for their layout.
    decoded_bytes = sum(i.length for i in scan.insts.values())
    if budget_hit or decoded_bytes > max_bytes:
        scan.refusals.append(Refusal(scan.entry, "region-budget",
                                     f"insts={len(scan.insts)} bytes={decoded_bytes} "
                                     f"span={lo:04X}..{hi:04X}"))
    scan.boundary_heads = sorted(h for h in heads if h in scan.insts)
    if not scan.exits and not scan.refusals and not scan.boundary_heads:
        scan.refusals.append(Refusal(scan.entry, "no-exit",
                                     "no ret/retf/iret/far-jmp/indirect-jmp reachable"))

    # Statically-visible code writes (CS-override direct stores).  A write into
    # the function's OWN instruction bytes is self-modifying code: a lift would
    # freeze whatever operands the snapshot happened to hold, then silently
    # decode garbage when the program retunes them (SkyRoads' LZS decoder
    # patches its per-file bit-width immediates exactly this way -- the lifted
    # copy read one file's widths for every file).  Refuse loud; the routine
    # requires runtime-code evidence or a separately selected implementation. Writes landing in
    # OTHER functions are recorded on the scan and adjudicated document-wide
    # (irgen_core.build_document), where every censused region is known.
    for ip, inst in scan.insts.items():
        t = cs_direct_store_target(inst)
        if t is not None:
            scan.cs_store_targets.append((ip, t))
    if scan.cs_store_targets:
        own = inst_byte_offsets(scan)
        for site, t in scan.cs_store_targets:
            if t in own:
                scan.refusals.append(Refusal(site, "self-modifying",
                                             f"cs:[{t:04X}] is inside this function's own code"))
                break
    return scan
