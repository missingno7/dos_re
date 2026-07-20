"""Checkpoint stepping: VM-until-checkpoint handoff for the source port.

The source-port runtime resumes only from stable logical boundaries (frame /
render / object-update / input).  This module makes that executable: given a
runtime positioned at *any* instruction, step the VM (instruction-exact oracle)
until it reaches the next compatible checkpoint, where game state is consistent
and a native phase-system could take over.

The checkpoint set is the game adapter's curated, evidence-based phase map:
the backend's declared ``checkpoints`` mapping
(address -> "kind: description", e.g. ``(0x1010, 0xD007): "frame: main loop"``).
Categorising by ``kind`` (the text before the first ``:``) lets a caller wait
for a specific phase boundary, e.g. only the object-update checkpoint.

Origin: generalized from the Overkill port's ``overkill/checkpoints.py``
(checkpoint table parameterized instead of imported from the game package).
"""
from __future__ import annotations

from typing import Mapping

Addr = tuple[int, int]


def _kind(desc: str) -> str:
    # Descriptions are "frame: ...", "render: ...", "object-update: ...", "input: ...".
    return desc.split(":", 1)[0].strip()


def checkpoints_by_kind(checkpoint_hooks: Mapping[Addr, str]) -> dict[str, frozenset[Addr]]:
    """Group a checkpoint table (address -> "kind: description") by kind."""
    out: dict[str, set[Addr]] = {}
    for addr, desc in checkpoint_hooks.items():
        out.setdefault(_kind(desc), set()).add(addr)
    return {k: frozenset(v) for k, v in out.items()}


def checkpoints_for(
    checkpoint_hooks: Mapping[Addr, str],
    kinds: "str | tuple[str, ...] | None",
) -> frozenset[Addr]:
    """Resolve a kind (or kinds) to its checkpoint address set; None == all."""
    if kinds is None:
        return frozenset(checkpoint_hooks)
    if isinstance(kinds, str):
        kinds = (kinds,)
    by_kind = checkpoints_by_kind(checkpoint_hooks)
    out: set[Addr] = set()
    for k in kinds:
        if k not in by_kind:
            raise KeyError(f"unknown checkpoint kind {k!r}; known: {sorted(by_kind)}")
        out |= by_kind[k]
    return frozenset(out)


def run_to_next_checkpoint(
    cpu,
    checkpoint_hooks: Mapping[Addr, str],
    *,
    kinds: "str | tuple[str, ...] | None" = None,
    max_steps: int = 5_000_000,
    skip_current: bool = True,
) -> Addr:
    """Step the VM until it reaches the next compatible checkpoint; return it.

    ``kinds`` filters which phase boundaries count (None = any).  ``skip_current``
    steps once first so a call made while already *at* a checkpoint advances to the
    following one (otherwise it would return immediately).  Raises ``TimeoutError``
    if no checkpoint is reached within ``max_steps``.
    """
    targets = checkpoints_for(checkpoint_hooks, kinds)
    if skip_current:
        cpu.step()
    for _ in range(max_steps):
        if cpu.addr() in targets:
            return cpu.addr()
        cpu.step()
    raise TimeoutError(f"no checkpoint in {kinds or 'any'} within {max_steps} steps")
