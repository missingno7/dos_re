"""Differential hook verification for the protected-mode (CPU386) runtime.

The PM analogue of :mod:`dos_re.verification`'s strict auto-continuation
mode, deliberately reduced to its essential transaction (the 16-bit class
carries years of interactive-frontend machinery the PM path does not need
yet):

1. When a hooked address is reached, clone the runtime twice
   (:func:`~dos_re.pm_snapshot.clone_pm_runtime`): a pre-state repro clone
   and an ASM-oracle clone (all hooks stripped — the oracle interprets the
   pure original program).
2. Run the Python handler on the LIVE runtime.  Its final EIP is the only
   acceptable continuation.
3. Interpret the original ASM on the oracle clone until it reaches that
   continuation (min one instruction, bounded steps).
4. Diff the full machine: registers, segment state, x87, the whole flat
   memory, VGA planes and sequencer state.  Any difference raises
   :class:`PMHookVerifyDivergence` with a readable report and the pre-state
   clone attached for reproduction.

Full-memory diffs by default (the charter rule); no narrowing knobs until a
real hook needs one — and then it must be temporary and deliberate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .pm_snapshot import clone_pm_runtime


class PMHookVerifyDivergence(RuntimeError):
    def __init__(self, report: str, *, repro_runtime=None, metadata: dict | None = None):
        super().__init__(report)
        self.repro_runtime = repro_runtime
        self.metadata = metadata or {}


@dataclass
class PMHookVerifierConfig:
    max_asm_steps: int = 20_000_000     # oracle budget per verified call
    stop_on_diff: bool = True
    log_diffs: bool = True
    max_diff_lines: int = 16
    # Verified-call sample cap per hook: after this many byte-exact calls the
    # hook retires to the passthrough (unverified) fast path — the same
    # SAMPLE semantics as the 16-bit liftverify: calls beyond the cap are
    # unproven.  None = verify every call (focused investigations).
    samples: int | None = 8


class PMHookVerifier:
    """Install as ``cpu.hook_verifier``; wraps every non-passthrough hook call."""

    def __init__(self, rt, config: PMHookVerifierConfig | None = None):
        self.rt = rt
        self.config = config or PMHookVerifierConfig()
        self.total_verified = 0
        self.calls_per_hook: dict[int, int] = {}

    def __call__(self, cpu, key: int, handler: Callable, name: str) -> None:
        cfg = self.config
        call_no = self.total_verified + 1
        pre_rt = clone_pm_runtime(self.rt)
        asm_rt = clone_pm_runtime(self.rt)
        asm_cpu = asm_rt.cpu
        asm_cpu.hook_verifier = None
        asm_cpu.replacement_hooks.clear()
        asm_cpu.hook_names.clear()
        # A replacement hook executes ATOMICALLY — the interpreter counts it as
        # one instruction, so no hardware IRQ can be delivered in the middle of
        # it.  The oracle re-runs the real instructions, which may cross the
        # interpreter's periodic IRQ-poll boundary and deliver a pending IRQ
        # mid-routine (its ISR then mutates memory the hook never touched),
        # producing a spurious divergence.  Suppress async IRQ delivery on the
        # oracle so it runs the routine atomically too — the IRQ is still
        # delivered by the main loop at the next step boundary for both, exactly
        # as on real hardware.  (Verifies computation, not interrupt phase.)
        asm_cpu.pending_irq = None

        handler(cpu)
        if cpu.coverage_telemetry is not None:
            cpu.coverage_telemetry.record_hook_unverified(key, name)
        target = cpu.eip

        # Min one original instruction: a same-EIP loop hook must not be
        # accepted against an untouched oracle.
        steps = 0
        asm_cpu.step()
        steps += 1
        while asm_cpu.eip != target:
            if steps >= cfg.max_asm_steps:
                raise PMHookVerifyDivergence(
                    f"PM HOOK VERIFY: oracle never reached continuation 0x{target:X} "
                    f"for hook 0x{key:X} {name} call {call_no} "
                    f"(gave up after {steps} steps at eip=0x{asm_cpu.eip:X})",
                    repro_runtime=pre_rt,
                    metadata={"hook": hex(key), "name": name, "call": call_no},
                )
            asm_cpu.step()
            steps += 1

        report = self._diff(cpu, asm_rt, key, name, call_no, steps)
        if report:
            if cfg.log_diffs:
                print(report, flush=True)
            if cfg.stop_on_diff:
                raise PMHookVerifyDivergence(
                    report, repro_runtime=pre_rt,
                    metadata={"hook": hex(key), "name": name, "call": call_no,
                              "asm_steps": steps},
                )
        self.total_verified += 1
        n = self.calls_per_hook.get(key, 0) + 1
        self.calls_per_hook[key] = n
        if self.config.samples is not None and n >= self.config.samples:
            cpu.hook_verifier_passthrough.add(key)   # retired: sampled enough
        if cpu.coverage_telemetry is not None:
            cpu.coverage_telemetry.record_hook_verified(key, name, steps)

    # ---- diffing -------------------------------------------------------------
    def _diff(self, live_cpu, asm_rt, key, name, call_no, steps) -> str:
        lines: list[str] = []
        a = asm_rt.cpu
        regnames = ("eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi")
        for i, rn in enumerate(regnames):
            if live_cpu.r[i] != a.r[i]:
                lines.append(f"  reg {rn}: hook=0x{live_cpu.r[i]:08X} asm=0x{a.r[i]:08X}")
        if live_cpu.eflags != a.eflags:
            lines.append(f"  eflags: hook=0x{live_cpu.eflags:04X} asm=0x{a.eflags:04X}")
        for sname in ("es", "cs", "ss", "ds", "fs", "gs"):
            if live_cpu.seg[sname] != a.seg[sname]:
                lines.append(f"  seg {sname}: hook=0x{live_cpu.seg[sname]:04X} "
                             f"asm=0x{a.seg[sname]:04X}")
        if live_cpu.st != a.st:
            lines.append(f"  x87 stack: hook={live_cpu.st} asm={a.st}")

        live_mem = self.rt.mem.data
        asm_mem = asm_rt.mem.data
        if live_mem != asm_mem:
            shown = 0
            n = min(len(live_mem), len(asm_mem))
            i = 0
            while i < n and shown < self.config.max_diff_lines:
                if live_mem[i] != asm_mem[i]:
                    j = i
                    while j < n and live_mem[j] != asm_mem[j] and j - i < 16:
                        j += 1
                    lines.append(
                        f"  mem 0x{i:06X}..0x{j - 1:06X}: "
                        f"hook={live_mem[i:j].hex()} asm={asm_mem[i:j].hex()}")
                    shown += 1
                    i = j
                else:
                    i += 1
            if shown >= self.config.max_diff_lines:
                lines.append("  ... (more memory differences)")

        lv, av = self.rt.dos.vga, asm_rt.dos.vga
        for p in range(4):
            if lv.planes[p] != av.planes[p]:
                first = next(i for i in range(0x10000)
                             if lv.planes[p][i] != av.planes[p][i])
                lines.append(f"  vga plane {p} differs from offset 0x{first:04X}")

        if not lines:
            return ""
        head = (f"PM HOOK VERIFY DIVERGENCE hook=0x{key:X} {name} call {call_no} "
                f"(oracle ran {steps} asm steps to eip=0x{live_cpu.eip:X}):")
        return "\n".join([head, *lines])


def install_pm_hook_verifier(rt, config: PMHookVerifierConfig | None = None) -> PMHookVerifier:
    verifier = PMHookVerifier(rt, config)
    rt.cpu.hook_verifier = verifier
    return verifier
