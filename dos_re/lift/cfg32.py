"""Function-region discovery over the 32-bit decoder (flat CPU386 code).

The protected-mode counterpart of :mod:`.cfg`: same walk, same refusal
census, over :func:`~.decode32.decode32` and flat linear addresses.  Far
transfers don't exist in the flat model; indirect jumps refuse as before
(jump tables are the classic Watcom ``switch`` — a lift refusal until a
bounded-table strategy earns its way in with evidence).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .cfg import Refusal
from .decode import (CALL, CALL_IND, HLT, INT, IRET, JCC, JMP, JMP_IND,
                     RET, SEQ, UNSUPPORTED)
from .decode32 import Inst32, decode32

EXIT_KINDS = (RET, IRET)


@dataclass
class FunctionScan32:
    entry: int
    insts: dict[int, Inst32] = field(default_factory=dict)
    exits: list[Inst32] = field(default_factory=list)
    calls_near: set[int] = field(default_factory=set)
    calls_indirect: list[int] = field(default_factory=list)
    ints: set[int] = field(default_factory=set)
    refusals: list[Refusal] = field(default_factory=list)
    probe_unchecked: list[int] = field(default_factory=list)

    @property
    def liftable(self) -> bool:
        return not self.refusals and bool(self.exits)

    @property
    def region(self) -> tuple[int, int]:
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


def scan_function32(fetch: Callable[[int], int], entry: int, *,
                    max_insts: int = 4096, max_bytes: int = 65536,
                    probe: Callable[[int], int | None] | None = None) -> FunctionScan32:
    """Discover the statically reachable region of the function at flat ``entry``.

    ``probe(ip)`` (optional) returns the interpreter-measured fetched-byte
    count of one ``step()`` at ``ip`` (None = couldn't execute there).  Only
    SEQ instructions are probed; a disagreement is a fatal refusal.
    """
    scan = FunctionScan32(entry=entry)
    work = [entry]
    budget_hit = False
    while work:
        ip = work.pop() & 0xFFFFFFFF
        if ip in scan.insts:
            continue
        if len(scan.insts) >= max_insts:
            budget_hit = True
            break
        try:
            inst = decode32(fetch, ip)
        except ValueError as exc:
            scan.refusals.append(Refusal(ip, "undecodable", str(exc)))
            continue
        scan.insts[ip] = inst

        if probe is not None and inst.kind == SEQ:
            measured = probe(ip)
            if measured is None:
                scan.probe_unchecked.append(ip)
            elif measured != inst.length:
                scan.refusals.append(Refusal(
                    ip, "decoder-mismatch",
                    f"static={inst.length} interpreter={measured} bytes={inst.raw.hex()}"))
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
            work.append(inst.target)
        elif kind == JMP:
            work.append(inst.target)
        elif kind == CALL:
            scan.calls_near.add(inst.target)
            work.append(inst.next_ip)
        elif kind == CALL_IND:
            scan.calls_indirect.append(ip)
            work.append(inst.next_ip)
        elif kind == INT:
            if inst.int_no is not None:
                scan.ints.add(inst.int_no)
            work.append(inst.next_ip)

    lo, hi = scan.region
    if budget_hit or (hi - lo) > max_bytes:
        scan.refusals.append(Refusal(scan.entry, "region-budget",
                                     f"insts={len(scan.insts)} span={lo:X}..{hi:X}"))
    if not scan.exits and not scan.refusals:
        scan.refusals.append(Refusal(scan.entry, "no-exit", "no ret/iret reachable"))
    return scan
