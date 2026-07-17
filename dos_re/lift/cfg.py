"""Function-region discovery for the lifter: blocks, exits, calls, refusals.

``scan_function`` walks every statically reachable instruction from an entry
offset, following fallthrough and direct near branches. Direct/indirect calls
and INTs do NOT extend the region (callees run through the VM at execution
time — docs/lifting_design.md §6); they are recorded as external
dependencies. The result is either a liftable region description or a
structured refusal list — the M0 census consumes both.

An optional ``probe`` callback cross-checks each decoded instruction length
against the interpreter (the authority). The walker itself stays OS-free and
pure: it sees code bytes only through ``fetch``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .decode import (CALL, CALL_FAR, CALL_IND, HLT, INT, IRET, JCC, JMP,
                     JMP_FAR, JMP_IND, RET, RETF, SEQ, UNSUPPORTED, Inst,
                     decode_one)

#: kinds that terminate a path (function exits).  An indirect jump ends the
#: region as a TAIL EXIT (the 32-bit pipeline's proven treatment): the emitted
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

    @property
    def liftable(self) -> bool:
        return not self.refusals and bool(self.exits)

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
        leaders = {self.entry}
        for inst in self.insts.values():
            if inst.kind in (JCC, JMP) and inst.target is not None:
                leaders.add(inst.target)
                if inst.kind == JCC:
                    leaders.add(inst.next_ip)
        return sorted(leaders & set(self.insts))


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


def scan_function(fetch: Callable[[int], int], entry: int, *,
                  max_insts: int = 4096, max_bytes: int = 16384,
                  probe: Callable[[int], int | None] | None = None) -> FunctionScan:
    """Discover the statically reachable region of the function at ``entry``.

    ``probe(ip)`` (optional) returns the interpreter-measured IP-DELTA of one
    ``step()`` at ``ip``, or None when the interpreter could not execute there
    (recorded, not fatal). Only non-transfer (SEQ) instructions are probed:
    for those, delta == encoded length (every decode/operand fetch advances
    ``s.ip`` byte-by-byte, including the interpreter's inlined fast paths),
    so a successful probe that disagrees with the static decode is fatal —
    either an operand-length bug or a transfer misclassified as SEQ. Transfer
    encodings are fixed-size and covered by the decoder's unit tests.
    """
    scan = FunctionScan(entry=entry)
    work = [entry]
    budget_hit = False
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
    if not scan.exits and not scan.refusals:
        scan.refusals.append(Refusal(scan.entry, "no-exit",
                                     "no ret/retf/iret/far-jmp/indirect-jmp reachable"))

    # Statically-visible code writes (CS-override direct stores).  A write into
    # the function's OWN instruction bytes is self-modifying code: a lift would
    # freeze whatever operands the snapshot happened to hold, then silently
    # decode garbage when the program retunes them (SkyRoads' LZS decoder
    # patches its per-file bit-width immediates exactly this way -- the lifted
    # copy read one file's widths for every file).  Refuse loud; the routine
    # belongs to the keep-interpreted / hand-hook queue.  Writes landing in
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
