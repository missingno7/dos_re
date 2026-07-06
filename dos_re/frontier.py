"""Explicit cold-start frontier classification — a triage manifest for the last
interpreted addresses.

Late in a port, coverage reports converge on a small residue of addresses that
never landed a hook.  Left unclassified, they become an undifferentiated
"unknown" bucket that quietly erodes trust in the coverage numbers.  This
module gives each leftover an explicit identity: a real hook candidate, an
intentionally interpreted bootstrap fragment, a bounded-original rare branch
owned by a larger hook, or a harmless scratch/tail inside an already-lifted
block.

The manifest is *data the game adapter owns* — a tuple of
:class:`FrontierEntry` — and a triage record, not an execution dependency.

Origin: generalized from the Overkill port's ``overkill/frontier_manifest.py``
(whose 25-entry manifest kept its cold-start coverage report precise to the
end); the Overkill entries stayed behind as game knowledge.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

Addr = tuple[int, int]


class FrontierCategory(StrEnum):
    FINAL_ORCHESTRATOR = "final-orchestrator"
    SAME_IP_LOOP_GATE = "same-ip-loop-gate"
    DO_NOT_HOOK_BOOTSTRAP = "do-not-hook-bootstrap"
    BOUNDED_ORIGINAL_RARE_BRANCH = "bounded-original-rare-branch"
    UNCLASSIFIED_HARMLESS_TAIL = "unclassified-harmless-scratch-tail"
    HOOK_CANDIDATE = "hook-candidate"


@dataclass(frozen=True)
class FrontierEntry:
    addr: Addr
    name: str
    island: str
    category: FrontierCategory
    status: str
    owner: Addr | None = None
    notes: str = ""


def by_addr(manifest: Iterable[FrontierEntry]) -> dict[Addr, FrontierEntry]:
    return {entry.addr: entry for entry in manifest}


def fmt_addr(addr: Addr) -> str:
    return f"{addr[0]:04X}:{addr[1]:04X}"


def frontier_summary_lines(manifest: Iterable[FrontierEntry]) -> list[str]:
    lines = ["== explicit cold-start frontier manifest =="]
    for entry in manifest:
        owner = f" owner={fmt_addr(entry.owner)}" if entry.owner else ""
        lines.append(
            f"  - {fmt_addr(entry.addr)} {entry.category.value:<34} "
            f"{entry.status:<24} {entry.name}{owner}"
        )
    return lines
