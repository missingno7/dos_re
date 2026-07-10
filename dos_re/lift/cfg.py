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

#: kinds that terminate a path (function exits)
EXIT_KINDS = (RET, RETF, IRET, JMP_FAR)


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
        if kind == JMP_IND:
            scan.refusals.append(Refusal(ip, "indirect-jump", inst.mnemonic))
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
    if budget_hit or (hi - lo) & 0xFFFF > max_bytes:
        scan.refusals.append(Refusal(scan.entry, "region-budget",
                                     f"insts={len(scan.insts)} span={lo:04X}..{hi:04X}"))
    if not scan.exits and not scan.refusals:
        scan.refusals.append(Refusal(scan.entry, "no-exit",
                                     "no ret/retf/iret/far-jmp reachable"))
    return scan
