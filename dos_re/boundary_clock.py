"""The exact-park boundary clock -- the generic tick-boundary scheduler seam.

Extracted from the Lemmings pilot's ``tick_clock`` (mechanism only; the head
addresses and their kinds are PORT FACTS the port passes in).  One clock
definition is shared by every driver -- viewer, headless replay, verifiers,
the bisector, the strict-VMless runner -- so record-time and replay-time agree
at the identical instruction (docs/replay_architecture.md).

THE CONTRACT (validated on the pilot, cross-mode): per boundary, deliver the
frame's timer IRQs, then run to park at the ``passes_per_boundary``-th pass of
a registered boundary head.  A park is a GAME event, so the boundary index
counts game progress -- replays reproduce identically on the interpreted oracle and
on a lifted graph.

EXACT PARK (why a sentinel raises rather than sets a flag): a replacement-hook
sentinel that only set a flag would not stop ``cpu.run`` immediately -- the
run finishes its step budget and overshoots the boundary by an arbitrary,
run-speed-dependent amount, so a faster candidate and the slower interpreter
park at DIFFERENT instructions and their state digests differ even when the
two are equivalent.  The sentinel instead executes its own instruction and
raises ``BoundaryReached``, which unwinds ``cpu.run`` at exactly one
instruction past the boundary -- identical for every run speed.

PASS-COUNTED PARK (why not every head pass, and why not a step threshold): a
head can be BOTH the idle input-poll AND the pacing loop of delay/fade
sequences.  Parking on EVERY pass makes each delay iteration its own boundary
(interactive play crawls); arming the park on an INSTRUCTION-COUNT threshold
is wrong the other way, because ``cpu.instruction_count`` is HOST-asymmetric
(the interpreter counts one per instruction, a lifted graph counts per hook
call).  The boundary is therefore defined by a GAME event only: the clock
parks exactly at the quota-exhausting registered-head pass since the
boundary's IRQs were delivered.  Head kinds refine the pacing:

    frame_gate  -- one pass IS one frame/tick: the head consumes the WHOLE
                   quota (parks on its first pass -- original cadence);
    pacing_spin -- a delay/poll iteration: costs 1 unit of the quota.

Two observation modes, ONE pass counter (the clock is identical either way):

  * interpreter sentinels -- replacement hooks at the head addresses that
    interpret exactly one original instruction and count the pass; used by
    runtimes with no lifted graph (the oracle).
  * the emitted observer hook -- lifted functions emitted with
    ``boundary_heads`` call ``cpu.boundary_hook(cpu, head_cs, head_ip,
    resume_ip)`` after each observed head instruction (dos_re.lift.emit); on
    quota the hook re-points CS:IP at the RESUME entry and raises, so the park
    AND the resume both execute as host code -- the strict-VMless wall never
    needs an interpreted sentinel.
"""
from __future__ import annotations

from pathlib import Path


class BoundaryReached(Exception):
    """Raised by a boundary sentinel/observer to unwind ``cpu.run`` at an
    exact park."""


class BoundaryClock:
    """The one clock: ``heads`` maps (cs, ip) -> kind ("frame_gate" |
    "pacing_spin") -- the port's boundary-head facts.  ``missing_head_hint``
    is appended to the budget-exhausted error so the failure teaches the port
    where to record the missing head fact."""

    def __init__(self, heads: dict[tuple[int, int], str], *,
                 passes_per_boundary: int = 128,
                 missing_head_hint: str = "register the head as a recovery "
                                          "fact and regenerate -- do not "
                                          "raise the budget."):
        self.heads = dict(heads)
        self.passes_per_boundary = passes_per_boundary
        self.missing_head_hint = missing_head_hint

    # -- internals -------------------------------------------------------

    def _park_costs(self, quota: int) -> dict[tuple[int, int], int]:
        return {k: (quota if kind == "frame_gate" else 1)
                for k, kind in self.heads.items()}

    # -- observation modes ------------------------------------------------

    def ensure_sentinels(self, rt) -> None:
        """Install the interpreter boundary sentinels once (idempotent).

        A sentinel interprets EXACTLY ONE original instruction (never the
        installed lifted hook that may live at the same address) and then
        raises ``BoundaryReached``.  Interpreting one instruction -- rather
        than delegating to a whole lifted routine -- keeps the park SYMMETRIC
        between the interpreted oracle (no hook at the address) and a lifted
        graph (a hook there): both advance by the same single instruction and
        park at the same cs:ip, so a state digest compares like-for-like."""
        if getattr(rt, "_bclock_sentinels_installed", False):
            return
        from dos_re.hooks import interpret_current_instruction_without_hook
        rt._bclock_sentinels_installed = True
        # [passes_remaining]: decremented on every registered-head pass; the
        # pass that takes it to zero parks exactly there.  Reset per boundary.
        if not hasattr(rt, "_bclock_passes_left"):
            rt._bclock_passes_left = [1]
        passes_left = rt._bclock_passes_left
        costs = self._park_costs(self.passes_per_boundary)
        for key in self.heads:
            def sentinel(cpu, _cost=costs[key]):
                interpret_current_instruction_without_hook(cpu)
                passes_left[0] -= _cost
                if passes_left[0] <= 0:
                    raise BoundaryReached

            # owns_time: the inner step() already counted the head
            # instruction; step()'s dispatch +1 would make an oracle pass
            # cost 2 while a VMless emitted-observer pass costs 1 -- a
            # virtual-time asymmetry.
            sentinel.owns_time = True
            rt.cpu.replacement_hooks[key] = sentinel
            rt.cpu.hook_names.setdefault(key, "boundary_sentinel_%04X" % key[1])

    def ensure_observer_hook(self, rt) -> None:
        """Arm the VMless boundary observer (idempotent).  Shares the SAME
        pass counter as the interpreter sentinels -- one clock definition.
        On the oracle runtime no lifted code runs, so the hook is inert."""
        if getattr(rt, "_bclock_observer_armed", False):
            return
        rt._bclock_observer_armed = True
        if not hasattr(rt, "_bclock_passes_left"):
            rt._bclock_passes_left = [1]
        passes_left = rt._bclock_passes_left
        costs = self._park_costs(self.passes_per_boundary)

        def boundary_hook(cpu, head_cs, head_ip, resume_ip):
            passes_left[0] -= costs.get((head_cs, head_ip), 1)
            if passes_left[0] <= 0:
                cpu.s.cs = head_cs & 0xFFFF
                cpu.s.ip = resume_ip & 0xFFFF
                raise BoundaryReached

        rt.cpu.boundary_hook = boundary_hook

    # -- the driver API ----------------------------------------------------

    def advance_boundary(self, rt, *, timer_irqs_per_frame: int = 3,
                         steps_per_frame: int = 50_000,
                         passes_per_boundary: int | None = None) -> None:
        """Advance the runtime by exactly one boundary: deliver the frame's
        timer IRQs, then run until the quota-exhausting registered-head pass
        and park exactly there.  ``steps_per_frame`` only scales the safety
        budget; a real boundary exits earlier."""
        cpu = rt.cpu
        quota = (self.passes_per_boundary if passes_per_boundary is None
                 else passes_per_boundary)
        # A runtime carrying the VMless graph observes boundaries through the
        # EMITTED observers (host code); interpreter sentinels would require
        # the head-owning routines to stay interpreted -- the exact wall
        # violation.  The oracle (no lifted graph) keeps the sentinels.
        if getattr(rt, "_vmless_boundary_observers", False):
            self.ensure_observer_hook(rt)
        else:
            self.ensure_sentinels(rt)
            self.ensure_observer_hook(rt)
        # Reset the pass quota BEFORE delivering IRQs: the counter still
        # holds the previous boundary's exhausted value, and an ISR that
        # passed a head would otherwise park during delivery -- outside the
        # park handler.
        rt._bclock_passes_left[0] = max(1, quota)
        from dos_re.interrupts import deliver_interrupt
        for _ in range(max(0, timer_irqs_per_frame)):
            deliver_interrupt(rt, 0x08)
        # EVERY boundary ends at an exact pass-counted park -- there is no
        # mode-change break: a break at a run-chunk edge is step-count-based
        # and step counts are HOST-ASYMMETRIC (a lifted call is one step), so
        # it parks the oracle and a lifted graph at different instructions.
        # The budget is a last-resort safety net only.
        ran = 0
        budget = steps_per_frame * 400
        try:
            while ran < budget:
                n = cpu.run(2000)
                if n == 0:
                    break
                ran += n
            else:
                # BUDGET EXHAUSTED without a head park: the game is spinning
                # in a wait loop that is NOT registered -- a missing boundary
                # head (coverage gap), not a normal frame.  Fail loud with
                # the evidence needed to record the head fact.
                raise RuntimeError(
                    f"boundary_clock: boundary exhausted its {budget:,}-step "
                    f"budget without reaching a registered head; the game is "
                    f"parked in an UNREGISTERED wait loop near "
                    f"{cpu.s.cs & 0xFFFF:04X}:{cpu.s.ip & 0xFFFF:04X} "
                    f"(mode {rt.dos.video_mode & 0x7F:02X}).  Sample the "
                    f"spin; {self.missing_head_hint}")
        except BoundaryReached:
            pass


def boundary_shadowed_entries(lift_dir, sentinel_keys) -> set[str]:
    """Entries whose lifted body would SHADOW a boundary -- must not be
    installed as lifted replacements when boundaries are observed by
    INTERPRETER sentinels (a lifted body containing a head executes straight
    through the boundary in host code without the per-step hook check).

    Dissolved on the strict-VMless path by the emitted observers (the head is
    observed INSIDE the lifted body); still used by transitional/hybrid tiers.
    A sentinel address that is a function's own ENTRY is fine (the sentinel
    hook takes precedence at that address), so only INTERIOR occurrences
    shadow.  Returns "CS:IP" strings for install ``skip=``; parses the
    emitted ``# CCCC:IIII`` instruction comments (the same address ledger
    liftlink/liftverify rely on)."""
    import re
    lift_dir = Path(lift_dir)
    sentinels = set(sentinel_keys)
    addr_re = re.compile(r"#\s+([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4})\b")
    shadowed: set[str] = set()
    for path in lift_dir.glob("lifted_*.py"):
        _, ecs, eip = path.stem.split("_")
        entry = (int(ecs, 16), int(eip, 16))
        for m in addr_re.finditer(path.read_text(encoding="utf-8")):
            addr = (int(m.group(1), 16), int(m.group(2), 16))
            if addr in sentinels and addr != entry:
                shadowed.add("%04X:%04X" % entry)
                break
    return shadowed
