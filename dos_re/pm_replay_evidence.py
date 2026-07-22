"""Protected-mode ReplayDriver + execution-evidence observation.

The PM producer half of the dos_re 3.0 replay/Atlas contract.  Everything the
Atlas consumes already has a schema (``FunctionVisit``, ``ObservedTransfer``,
``ReplayExecutionEvidence``, validations); this module makes a real PM runtime
produce it:

* :class:`PMReplayDriver` -- the concrete :class:`~dos_re.replay.ReplayDriver`
  for the flat protected-mode runtime: restore a continuation, replay the
  artifact's input events to a frame point deterministically, capture/project
  the complete machine.
* :class:`PMFunctionObserver` -- entry/exit observation over the CPU's
  non-perturbing ``entry_probes`` seam.  Probes never replace execution,
  count instructions, or re-poll IRQs, so the observed run is byte-identical
  to an unobserved one -- cached states and evidence describe the SAME
  execution.
* :func:`observe_pm_replay` -- oracle evidence pass: replay 0..end once with
  observation, persist visits + transfers via ``set_execution_evidence``.
* :func:`validate_pm_replay` -- full-range oracle validation
  (``verify_interval`` over the whole timeline) that makes a
  candidate-captured artifact ``trusted`` and caches its endpoint states.

Function exits are detected structurally: at entry the observer records the
return address and the ESP it will hold after the callee's ``ret``; a probe at
that return address fires the exit when the stack has unwound to (or above)
that level.  Nested and recursive calls stack; an entry whose return is never
reached stays an ``incomplete`` visit (the model's own term).  Interrupts
inside an interval are fine: the ISR runs above the recorded ESP threshold, so
its passage over a probed return address does not satisfy the exit condition.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping

from .pm_replay_input import FrameClock, FramePaced, ProtectedModeInputAdapter
from .pm_snapshot import apply_pm_continuation, capture_pm_continuation
from .replay import (
    ContinuationState,
    ReplayArtifact,
    ReplayEvidenceRecorder,
    ReplayExecutionIdentity,
    ReplayPoint,
    machine_projection,
    verify_interval,
)
from .verification_contract import (
    VerificationProjectionContract,
    VerificationRepresentation,
)


class ReplayStalled(RuntimeError):
    """A replay's frame clock stopped advancing — it diverged from the record.

    Raised loud instead of spinning forever (the frame-parked-transition and
    non-collapsible-pause failure modes).  Names the stall frame and EIP so the
    divergence can be diagnosed and the artifact excluded from the corpus."""

#: The complete-machine projection every PM profile declares.
PM_PROJECTION_CONTRACT = VerificationProjectionContract(
    projection_id="dos-re-pm-complete-machine",
    representation=VerificationRepresentation.COMPLETE_CONTINUATION,
    schema_id="dos-re-complete-machine-v1",
    required_regions=("memory", "vga-planes"),
)


class PMReplayDriver:
    """Concrete protected-mode ReplayDriver over a runtime factory.

    ``create_runtime()`` must return a freshly booted runtime shell configured
    exactly as the profile demands (oracle: pure interpreted; candidate: with
    its overrides bound) -- the driver then *replaces* that shell's machine
    state from the artifact's continuation, so only device attachment and hook
    composition matter, not the boot path taken.
    """

    def __init__(
        self,
        profile: ReplayExecutionIdentity,
        create_runtime: Callable[[], object],
        *,
        frame_tick_addr: int,
        timeline_id: str,
        observer_factory: Callable[[object], "PMFunctionObserver"] | None = None,
    ) -> None:
        self._profile = profile
        self._create_runtime = create_runtime
        self._frame_tick_addr = int(frame_tick_addr)
        self._timeline_id = str(timeline_id)
        self._observer_factory = observer_factory
        self.runtime = None
        self.observer: PMFunctionObserver | None = None
        self._clock: FrameClock | None = None
        self._adapter: ProtectedModeInputAdapter | None = None
        self._ordinal = 0
        self._event_cursor = 0
        self._pending_events_dos = None

    # -- ReplayDriver protocol -------------------------------------------------

    @property
    def profile(self) -> ReplayExecutionIdentity:
        return self._profile

    @property
    def current_point(self) -> ReplayPoint:
        return ReplayPoint(self._ordinal, self._timeline_id)

    def restore(self, state: ContinuationState, point: ReplayPoint) -> None:
        if point.timeline_id != self._timeline_id:
            raise ValueError("replay point belongs to another timeline")
        rt = self._create_runtime()
        apply_pm_continuation(rt, state)
        self.runtime = rt
        self._ordinal = point.ordinal
        self._adapter = None            # rebuilt against the artifact in replay_to
        self._event_cursor = int(state.event_cursor)
        self._clock = FrameClock(rt.cpu, self._frame_tick_addr, self._on_frame)
        self._clock.frame = point.ordinal
        self._pending_events_dos = rt.dos
        if self.observer is not None:
            self.observer.detach()
            self.observer = None
        if self._observer_factory is not None:
            self.observer = self._observer_factory(rt)

    def _on_frame(self, frame: int) -> None:
        self._ordinal = frame
        if self._adapter is not None:
            from .pm_backend import send_key
            self._adapter.apply(frame, self._pending_events_dos,
                                deliver_key=send_key)

    def replay_to(self, artifact: ReplayArtifact, point: ReplayPoint) -> None:
        if self.runtime is None or self._clock is None:
            raise RuntimeError("driver must restore a continuation first")
        if point.timeline_id != self._timeline_id:
            raise ValueError("replay point belongs to another timeline")
        if point.ordinal < self._ordinal:
            raise ValueError("cannot replay backwards; restore an earlier state")
        if self._adapter is None:
            self._adapter = ProtectedModeInputAdapter(
                artifact.events, event_cursor=self._event_cursor)
        cpu = self.runtime.cpu
        clock = self._clock
        # PROGRESS GUARD.  A frame that does not tick within this many
        # instructions is parked forever, not merely slow: the record's worst
        # legitimate frame (a level-transition sound wait on the instruction
        # clock) is tens of millions of instructions, so a full chunk with no
        # frame advance means the replay has DIVERGED from the recording and the
        # game is waiting on state that will never arrive (e.g. a real-time pause
        # that cannot collapse to zero, or a nondeterministic device wait).  Fail
        # loud with the stall point instead of spinning forever.
        STALL_BUDGET = 200_000_000
        while clock.frame < point.ordinal and not cpu.halted:
            before = clock.frame
            clock.stop_at = point.ordinal
            try:
                cpu.run(STALL_BUDGET)
            except FramePaced:
                break
            if clock.frame == before:
                raise ReplayStalled(
                    f"replay stalled at frame {clock.frame} (eip=0x{cpu.eip:X}): "
                    f"the frame clock did not advance in {STALL_BUDGET:,} "
                    f"instructions -- the replay diverged from the recording "
                    f"(a non-collapsible pause or a nondeterministic wait)")
        self._ordinal = clock.frame
        if self._ordinal != point.ordinal and not cpu.halted:
            raise ReplayStalled(
                f"replay reached frame {self._ordinal} before target "
                f"{point.ordinal} (halted={cpu.halted})")

    def capture(self) -> ContinuationState:
        if self.runtime is None:
            raise RuntimeError("driver has no restored runtime")
        cursor = (self._adapter.event_cursor if self._adapter is not None
                  else self._event_cursor)
        return capture_pm_continuation(self.runtime, event_cursor=cursor)

    def project(self):
        return machine_projection(
            self.capture(), schema_id=self._profile.projection_schema)

    def verification_projection_contract(self) -> VerificationProjectionContract:
        return PM_PROJECTION_CONTRACT


class PMFunctionObserver:
    """Entry/exit + transfer observation over ``cpu.entry_probes``.

    ``functions`` maps flat entry EIPs to stable function identity strings
    (``kegg.identity.function_id``-style).  ``point`` supplies the CURRENT
    stable replay point (frame ordinal).  Visits are frame-granular with a
    COVERING convention: an entry during frame N records boundary N (the state
    BEFORE that frame); an exit records the boundary AFTER the frame in which
    it was DETECTED -- so replaying [first_entry, last_exit]
    boundary-to-boundary always contains every recorded invocation.

    Exits are inferred by LAZY STACK UNWINDING: at every probe firing (and at
    ``finish``), active call frames whose recorded post-return ESP has been
    unwound past are popped and their exits recorded at the current boundary.
    This keeps ``cpu.entry_probes`` READ-ONLY after construction -- the
    per-instruction fast path never sees the dict mutate (a mutating probe
    dict defeats the tracing JIT's guards; measured as a ~20x gameplay
    slowdown when return-address probes were installed/removed per call).
    A detected-late exit only pushes ``last_exit`` later, never earlier, so
    the covering-interval property is preserved; invocation counts and first
    entries are exact.
    """

    def __init__(
        self,
        cpu,
        functions: Mapping[int, str],
        recorder: ReplayEvidenceRecorder,
        point: Callable[[], ReplayPoint],
    ) -> None:
        self.cpu = cpu
        self.functions = {int(k): str(v) for k, v in functions.items()}
        self.recorder = recorder
        self.point = point
        # Active call frames PER STACK: {esp >> 16: [(function_id,
        # esp_after_ret), ...]}.  A single global LIFO breaks on
        # stack-switching interrupt handlers (KE's sound driver mixes on its
        # own stack): frames entered on one stack can never be unwound by ESP
        # observations made on another, so a global list leaks monotonically
        # (measured: ~12 leaked frames per game frame).  Frames on a non-
        # current stack pop when that stack's own next probe fires shallower,
        # or wholesale at a frame boundary (``finish``), where no ISR can be
        # mid-flight.  Granularity is 64 KiB: two distinct stacks inside one
        # 64 KiB window would re-merge (bounded mis-ordering, loud cap below).
        self._stacks: dict[int, list[tuple[str, int]]] = {}
        if cpu.entry_probes is None:
            cpu.entry_probes = {}
        self._probes = cpu.entry_probes
        for eip in self.functions:
            existing = self._probes.get(eip)
            if existing is not None:
                raise RuntimeError(
                    f"entry probe collision at 0x{eip:X}: {existing!r}")
            self._probes[eip] = self._fire

    @staticmethod
    def _unwind(stack: list, esp: int, exit_point: ReplayPoint,
                recorder: ReplayEvidenceRecorder) -> None:
        """Pop every frame this stack has already returned past (wrap-safe)."""
        while stack:
            fid, esp_after = stack[-1]
            if ((esp - esp_after) & 0xFFFFFFFF) >= 0x80000000:
                break                   # still below the frame: call is live
            stack.pop()
            recorder.exit(fid, exit_point)

    @staticmethod
    def _entered_via_call(cpu, eip: int) -> bool:
        """Does the stack top look like a return address from a CALL to here?

        A probed entry address can also be an internal jump target (Watcom
        loops jump straight at function entries); treating each loop
        iteration as an invocation both corrupts the shadow stack (a phantom
        frame per iteration -- measured as unbounded growth in gameplay) and
        inflates counts.  Direct near calls (E8 rel32) are validated exactly;
        indirect near calls (FF /2, lengths 2..7) by opcode shape."""
        mem = cpu.mem
        ret = mem.r32(cpu.r[4])
        if mem.r8((ret - 5) & 0xFFFFFFFF) == 0xE8:
            rel = mem.r32((ret - 4) & 0xFFFFFFFF)
            if rel & 0x80000000:
                rel -= 0x100000000
            if (ret + rel) & 0xFFFFFFFF == eip:
                return True
        for length in (2, 3, 4, 5, 6, 7):
            if mem.r8((ret - length) & 0xFFFFFFFF) == 0xFF \
                    and ((mem.r8((ret - length + 1) & 0xFFFFFFFF) >> 3) & 7) == 2:
                return True
        return False

    def _fire(self, cpu) -> None:
        eip = cpu.eip
        if not self._entered_via_call(cpu, eip):
            return                      # jump entry (loop head), not a call
        esp = cpu.r[4]
        here = self.point()
        stack = self._stacks.get(esp >> 16)
        if stack is None:
            stack = self._stacks[esp >> 16] = []
        # Unwind at the CALLER's stack level (esp+4: just before this call
        # pushed its return address): a sibling called from the same site has
        # esp_after exactly there, and comparing against the callee's esp
        # would leave it looking live forever (measured: 11k stacked frames of
        # one hot function called in a loop).  The enclosing caller itself
        # stays live -- its esp_after lies above its own pushed frame.  The
        # return may have happened within the CURRENT frame, so the covering
        # exit boundary is the one after it.
        self._unwind(stack, (esp + 4) & 0xFFFFFFFF,
                     ReplayPoint(here.ordinal + 1, here.timeline_id),
                     self.recorder)
        fid = self.functions[eip]
        caller = stack[-1][0] if stack else None
        self.recorder.enter(fid, here)
        if caller is not None:
            self.recorder.observe_transfer(caller, fid, "call", here)
        stack.append((fid, (esp + 4) & 0xFFFFFFFF))
        if len(stack) > 100_000:        # fail loud, never silently balloon
            raise RuntimeError(
                "observer shadow stack overflow -- stack tracking has "
                "diverged from the program's real call structure")

    def finish(self) -> None:
        """Close the books at a frame boundary.

        The current stack unwinds by its live ESP; every OTHER stack is
        quiescent here (no ISR is mid-flight at the mainline frame tick), so
        its remaining frames have all returned -- close them at the current
        boundary.  Frames still genuinely live (the current call chain) stay
        open and their visits remain ``incomplete`` -- 'active at the end of
        the interval' is a first-class outcome, not an error."""
        esp = self.cpu.r[4]
        here = self.point()
        current = esp >> 16
        for tag, stack in self._stacks.items():
            if tag == current:
                self._unwind(stack, esp, here, self.recorder)
            else:
                while stack:
                    fid, _ = stack.pop()
                    self.recorder.exit(fid, here)

    def detach(self) -> None:
        for addr in list(self._probes):
            if self._probes[addr] == self._fire:
                del self._probes[addr]
        self._stack.clear()


def _end_point(artifact: ReplayArtifact) -> ReplayPoint:
    raw = artifact.metadata.get("end_point")
    if not isinstance(raw, Mapping):
        raise ValueError("replay artifact has no recorded end point")
    return ReplayPoint.from_json(raw)


def observe_pm_replay(
    artifact_path: str | Path,
    oracle_profile: ReplayExecutionIdentity,
    create_runtime: Callable[[], object],
    functions: Mapping[int, str],
    *,
    provenance: Mapping[str, object] | None = None,
) -> ReplayEvidenceRecorder:
    """Oracle evidence pass: replay 0..end once, observing function execution.

    Registers the oracle profile (base state shared with the recording),
    replays the whole timeline with :class:`PMFunctionObserver` attached,
    caches the oracle's end state, and persists the observed visits and
    transfers with ``set_execution_evidence`` -- after which
    ``ExecutionAtlas.ingest_replay`` accepts the artifact (once it is also
    validated; see :func:`validate_pm_replay`).
    """
    artifact = ReplayArtifact.open(artifact_path)
    if oracle_profile.role != "oracle":
        raise ValueError("evidence observation requires an oracle profile")
    end = _end_point(artifact)
    frame_tick = int(artifact.metadata["frame_tick_addr"])
    # The frame tick is the timeline seam, not ordinary evidence: its hook
    # raises FramePaced and re-dispatches on resume, so a probe there would
    # double-count entries at every pause boundary.
    functions = {int(k): str(v) for k, v in functions.items()
                 if int(k) != frame_tick}
    recorder = ReplayEvidenceRecorder()

    driver = PMReplayDriver(
        oracle_profile, create_runtime,
        frame_tick_addr=frame_tick, timeline_id=artifact.timeline_id,
        observer_factory=lambda rt: PMFunctionObserver(
            rt.cpu, functions, recorder,
            lambda: driver.current_point),
    )
    start = ReplayPoint(0, artifact.timeline_id)
    registered = {p.profile_id for p, _ in artifact.profiles()}
    if oracle_profile.profile_id not in registered:
        base = artifact.restore(artifact.capture_profile(), start)
        artifact.register_profile(oracle_profile, base_point=start, base_state=base)
    driver.restore(artifact.restore(oracle_profile, start), start)
    driver.replay_to(artifact, end)
    if driver.observer is not None:
        driver.observer.finish()
    if not artifact.has_cached(oracle_profile, end):
        artifact.cache(oracle_profile, end, driver.capture(),
                       metadata={"kind": "oracle-observation-end"})
    evidence = recorder.evidence(oracle_profile, provenance=dict(provenance or {}))
    artifact.set_execution_evidence(oracle_profile, evidence,
                                    visits=recorder.visits)
    return recorder


def validate_pm_replay(
    artifact_path: str | Path,
    oracle_profile: ReplayExecutionIdentity,
    candidate_profile: ReplayExecutionIdentity,
    create_oracle_runtime: Callable[[], object],
    create_candidate_runtime: Callable[[], object],
):
    """Full-range oracle validation: makes the captured timeline trusted.

    Replays the ENTIRE timeline on both profiles and compares complete-machine
    projections at the endpoints (``verify_interval``); the recorded validation
    is exactly what ``ReplayArtifact.trusted`` requires.  Both endpoint states
    are cached for each profile, so later interval verification restores from
    them instead of replaying from the base.
    """
    artifact = ReplayArtifact.open(artifact_path)
    end = _end_point(artifact)
    start = ReplayPoint(0, artifact.timeline_id)
    frame_tick = int(artifact.metadata["frame_tick_addr"])
    base = artifact.restore(artifact.capture_profile(), start)
    for profile in (oracle_profile, candidate_profile):
        registered = {p.profile_id for p, _ in artifact.profiles()}
        if profile.profile_id not in registered:
            artifact.register_profile(profile, base_point=start, base_state=base)
    oracle = PMReplayDriver(
        oracle_profile, create_oracle_runtime,
        frame_tick_addr=frame_tick, timeline_id=artifact.timeline_id)
    candidate = PMReplayDriver(
        candidate_profile, create_candidate_runtime,
        frame_tick_addr=frame_tick, timeline_id=artifact.timeline_id)
    return verify_interval(artifact, oracle, candidate, start, end)
