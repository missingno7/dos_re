"""Composition verification for recovered *non-leaf* PM routines.

The strict :class:`~dos_re.pm_verification.PMHookVerifier` diffs the *whole*
machine — including the throwaway stack frame the routine and all its nested
sub-calls churn.  That is the right contract for a leaf, but a composed routine
(one that calls other routines) leaves the exact spill/scratch of every callee
on the stack, which a clean recovered re-implementation has no reason to
reproduce byte-for-byte.

This verifier keeps the same clone-and-run transaction but diffs only the
**observable** state: every byte the routine writes *outside* its own transient
stack frame — i.e. everything except the window ``[min_esp, entry_esp)`` that
the call and its callees transiently used.  That window is pure scratch: it
lies below the caller's stack pointer, so nothing above the routine can read it
after the return.

Contract: the recovered handler must reproduce the routine's game-state memory
effects and set a correct continuation (eip/esp).  Registers are **not** diffed
— they are ABI detail, and a composed routine is installed only where its
result registers are unused by the caller (verify that separately before using
this).  Use the strict verifier for anything whose return value is consumed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .pm_snapshot import clone_pm_runtime


class PMCompositionDivergence(RuntimeError):
    def __init__(self, report: str, *, repro_runtime=None, metadata: dict | None = None):
        super().__init__(report)
        self.repro_runtime = repro_runtime
        self.metadata = metadata or {}


@dataclass
class PMCompositionConfig:
    max_asm_steps: int = 4_000_000
    samples: int | None = None          # cap proven calls per hook (None = all)


class PMCompositionVerifier:
    """A ``cpu.hook_verifier`` that checks observable-state equivalence."""

    def __init__(self, rt, config: PMCompositionConfig | None = None):
        self.rt = rt
        self.config = config or PMCompositionConfig()
        self.total_verified = 0
        self.calls_per_hook: dict[int, int] = {}

    def __call__(self, cpu, key: int, handler: Callable, name: str) -> None:
        cfg = self.config
        done = self.calls_per_hook.get(key, 0)
        if cfg.samples is not None and done >= cfg.samples:
            handler(cpu)                     # already proven — just run it
            self.calls_per_hook[key] = done + 1
            return

        entry_esp = cpu.r[4]
        ret = int.from_bytes(cpu.mem.data[entry_esp:entry_esp + 4], "little")
        pre_rt = clone_pm_runtime(self.rt)
        asm_rt = clone_pm_runtime(self.rt)
        asm_cpu = asm_rt.cpu
        asm_cpu.hook_verifier = None
        asm_cpu.replacement_hooks.clear()
        asm_cpu.hook_names.clear()
        asm_cpu.pending_irq = None           # re-run the routine atomically

        # Interpret the original to its return, tracking the deepest esp — the
        # bottom of the transient frame that is legitimately scratch.
        min_esp = entry_esp
        steps = 0
        asm_cpu.step(); steps += 1
        if asm_cpu.r[4] < min_esp:
            min_esp = asm_cpu.r[4]
        while not (asm_cpu.eip == ret and asm_cpu.r[4] > entry_esp):
            asm_cpu.step(); steps += 1
            if asm_cpu.r[4] < min_esp:
                min_esp = asm_cpu.r[4]
            if steps >= cfg.max_asm_steps:
                raise PMCompositionDivergence(
                    f"PM COMPOSITION: oracle never returned from 0x{key:X} "
                    f"{name} (gave up after {steps} steps at eip=0x{asm_cpu.eip:X})",
                    repro_runtime=pre_rt,
                    metadata={"hook": hex(key), "name": name})

        handler(cpu)                         # recovered routine on the LIVE cpu

        self._diff(cpu, asm_rt, key, name, min_esp, entry_esp, pre_rt)
        self.total_verified += 1
        self.calls_per_hook[key] = done + 1

    def _diff(self, live_cpu, asm_rt, key, name, min_esp, entry_esp, pre_rt):
        a = memoryview(live_cpu.mem.data)
        b = memoryview(asm_rt.mem.data)
        # Everything except the transient stack window [min_esp, entry_esp).
        if a[:min_esp] == b[:min_esp] and a[entry_esp:] == b[entry_esp:]:
            return
        # Locate the first observable differences for the report.
        diffs = []
        la = live_cpu.mem.data
        lb = asm_rt.mem.data
        for lo, hi in ((0, min_esp), (entry_esp, len(la))):
            i = lo
            while i < hi and len(diffs) < 16:
                if la[i] != lb[i]:
                    j = i
                    while j < hi and la[j] != lb[j]:
                        j += 1
                    diffs.append((i, la[i:j].hex(), lb[i:j].hex()))
                    i = j
                else:
                    i += 1
            if len(diffs) >= 16:
                break
        lines = "\n".join(
            f"  mem 0x{off:06X}: recovered={hv} oracle={ov}"
            for off, hv, ov in diffs)
        raise PMCompositionDivergence(
            f"PM COMPOSITION DIVERGENCE hook=0x{key:X} {name} "
            f"(stack-frame window 0x{min_esp:X}..0x{entry_esp:X} ignored):\n{lines}",
            repro_runtime=pre_rt,
            metadata={"hook": hex(key), "name": name})


def install_pm_composition_verifier(rt, config: PMCompositionConfig | None = None):
    verifier = PMCompositionVerifier(rt, config)
    rt.cpu.hook_verifier = verifier
    return verifier
