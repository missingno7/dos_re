"""The single dos_re execution and launch lifecycle.

Every game exposes one ``scripts/play.py`` built from :class:`GameFrontend`.
The player resolves one immutable execution plan before constructing a runtime,
then dispatches real-mode, protected-mode, differential-verification, or
presentation behavior through frontend methods. Recovery level is a property
of each selected implementation, never a player mode.

The canonical CLI owns development/release policy, snapshots, ReplayArtifact
recording and playback, exact verification intervals, pacing, presentation,
and persistence. A frontend supplies game identities, coverage, implementation
and service catalogs, runtime construction, backend activators, and—when
verification is supported—an oracle/candidate driver pair.

This module is in the frontend ring. Viewer dependencies stay lazy so planning,
headless execution, and verification do not import numpy or pygame.
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import inspect
import sys
from datetime import datetime
from pathlib import Path

from dos_re.dos import ConsoleInputWouldBlock
from dos_re.execution import (
    BootstrapArtifact,
    DependencyCapability,
    ExeBootstrapProvider,
    ExecutionConfiguration,
    ExecutionPlan,
    ExecutionPlanError,
    ImplementationCatalog,
    ImplementationDescriptor,
    ImplementationEntry,
    ImplementationOrigin,
    OverrideCategory,
    ProgramCoverage,
    RuntimeServiceCatalog,
    RuntimeServiceDescriptor,
    execution_composition_digest,
    format_execution_plan,
    plan_execution,
    profile_configuration,
)
from dos_re.replay_input import (
    MOUSE_CHANNEL,
    SCAN_CHANNEL,
    RealModeInputAdapter,
    mouse_payload,
    mouse_sample,
    scan_payload,
)
from dos_re.keyboard import KeyDispatcher, scancode_table
from dos_re.x86 import HaltExecution, UnsupportedInstruction
# The EXE loader is imported lazily inside default frontend methods. A detached
# frontend can therefore construct its selected runtime without importing the
# interpreter loader. ``write_snapshot`` itself is EXE-free.
from dos_re.replay import (
    GUEST_INSTRUCTION_COORDINATE,
    ReplayExecutionIdentity,
    ReplayArtifact,
    ReplayDriver,
    ReplayError,
    ReplayPoint,
    ReplayPointCoordinate,
    ReplayRecording,
    bisect_divergence,
    verify_checkpointed,
    verify_interval,
)
from dos_re.snapshot import (
    apply_runtime_continuation,
    capture_runtime_continuation,
    write_snapshot,
)

def _timestamp_dir(root: Path, prefix: str) -> Path:
    return Path(root) / f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}"


class _RealReplayRecorder:
    """Viewer capture state; persistence is delegated only to ReplayRecording."""

    def __init__(self, frontend, args, rt, *, root: Path, name: str, metadata: dict):
        directory = _timestamp_dir(root, f"replay_{_safe_name(name)}")
        profile = frontend.replay_profile(args, rt)
        base = frontend.capture_replay_state(rt, event_cursor=0)
        timeline = f"real-mode-frame-boundaries:{frontend.name}:v1"
        self.recording = ReplayRecording(
            directory, timeline_id=timeline, profile=profile,
            base_state=base, metadata=metadata)
        schema_id, value = frontend.replay_point_coordinate(
            rt, args, point_ordinal=0)
        self.recording.mark(0, schema_id=schema_id, value=value)
        self.directory = directory
        self._args = args
        self._start_boundary = 0
        self._last_mouse = None

    @property
    def active(self) -> bool:
        return self.recording.active

    @property
    def event_count(self) -> int:
        return self.recording.event_count

    def start(self, *, boundary: int) -> Path:
        self._start_boundary = int(boundary)
        return self.directory

    def _ordinal(self, boundary: int) -> int:
        return max(0, int(boundary) - self._start_boundary)

    def record_scan(self, *, boundary: int, scancode: int) -> None:
        self.recording.add(
            self._ordinal(boundary), SCAN_CHANNEL, scan_payload(scancode))

    def record_mouse(
        self, *, boundary: int, u: float, v: float, buttons: int,
    ) -> tuple[float, float, int]:
        sample = mouse_sample(u, v, buttons)
        if sample != self._last_mouse:
            self.recording.add(
                self._ordinal(boundary), MOUSE_CHANNEL,
                mouse_payload(*sample))
            self._last_mouse = sample
        return sample

    def mark(self, frontend, args, rt, *, boundary: int) -> None:
        ordinal = self._ordinal(boundary)
        schema_id, value = frontend.replay_point_coordinate(
            rt, args, point_ordinal=ordinal)
        self.recording.mark(
            ordinal, schema_id=schema_id, value=value)

    def stop(self, frontend, rt, *, boundary: int) -> Path:
        end = self._ordinal(boundary)
        self.mark(frontend, self._args, rt, boundary=boundary)
        state = frontend.capture_replay_state(
            rt, event_cursor=self.recording.event_count)
        self.recording.finish(end, end_state=state)
        return self.directory


class _RealReplayPlayback:
    def __init__(
        self, artifact: ReplayArtifact, profile: ReplayExecutionIdentity,
    ):
        self.artifact = artifact
        self.profile = profile
        self.adapter = RealModeInputAdapter(artifact.events)
        meta = artifact.metadata
        self.end_point = ReplayPoint.from_json(meta["end_point"])
        self.mouse_present_hint = bool(meta["mouse_present"])

    @classmethod
    def open(cls, path: str | Path):
        artifact = ReplayArtifact.open(path)
        profile_id = str(artifact.metadata["recording_profile_id"])
        profiles = {profile.profile_id: profile for profile, _ in artifact.profiles()}
        if profile_id not in profiles:
            raise ValueError(f"recording profile is absent from artifact: {profile_id!r}")
        return cls(artifact, profiles[profile_id])

    @property
    def events(self):
        return self.artifact.events

    @property
    def next_event_index(self) -> int:
        return self.adapter.event_cursor

    def finished(self, boundary: int) -> bool:
        return int(boundary) >= self.end_point.ordinal

    def apply_to_runtime(self, boundary, rt, *, deliver, single=False):
        return self.adapter.apply_to_runtime(
            boundary, rt, deliver=deliver, single=single)

    def coordinate_after(self, boundary: int) -> ReplayPointCoordinate:
        return self.artifact.timeline_coordinate(ReplayPoint(
            int(boundary) + 1, self.artifact.timeline_id))


def _safe_name(value: str) -> str:
    cleaned = "".join(
        ch if ch.isalnum() or ch in "-_" else "_" for ch in str(value).strip())
    return cleaned or "input"


def _content_identity(path: str | Path | None) -> str:
    if path:
        candidate = Path(path)
        if candidate.is_file():
            return hashlib.sha256(candidate.read_bytes()).hexdigest()
    return hashlib.sha256(str(path or "").encode("utf-8")).hexdigest()


def _implementation_identity(frontend) -> str:
    paths = [Path(inspect.getsourcefile(type(frontend)) or __file__)]
    h = hashlib.sha256()
    for path in sorted(set(paths), key=str):
        h.update(str(path.name).encode("utf-8"))
        h.update(path.read_bytes())
    return h.hexdigest()


def _source_tree_identity(root: Path) -> str:
    """Content identity for a Python source tree, independent of its location."""
    h = hashlib.sha256()
    paths = sorted(
        (path for path in root.rglob("*.py") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    if not paths:
        raise ValueError(f"runtime source tree contains no Python files: {root}")
    for path in paths:
        name = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        h.update(len(name).to_bytes(8, "big"))
        h.update(name)
        h.update(len(content).to_bytes(8, "big"))
        h.update(content)
    return h.hexdigest()


@functools.lru_cache(maxsize=1)
def _runtime_identity() -> str:
    # Replay validity depends on the complete framework implementation, not
    # only the five most obvious runtime modules. Interrupt delivery, optional
    # devices, snapshot adapters and lifted-call mechanics can all affect
    # deterministic continuation.
    return _source_tree_identity(Path(__file__).parent)


class GameFrontend:
    """Per-game adapter for the canonical player. Subclass and override.

    The defaults implement the SIMPLE DETERMINISTIC model: fixed
    ``--steps-per-frame`` instruction budget + ``--timer-irqs-per-frame`` INT 08h
    ticks per frame, no wall-clock time source — the frame index IS the replay clock.
    Start every new port on this model; replace ``advance_frame`` only when the
    game's own timing demands it (and then also extend ``replay_metadata`` /
    ``apply_replay_metadata`` so replays restore your knobs).
    """

    #: used in window titles, replay metadata and artifact filename prefixes
    name = "game"

    # --- CLI defaults (surface them as class attrs so subclasses just assign) ---
    default_exe: str | None = None            # None -> --exe is required
    default_game_root: str | None = None
    default_dos_args = ""
    default_steps_per_frame = 40_000
    default_timer_irqs_per_frame = 0
    default_present_hz = 60
    default_scale = 3
    #: "adlib" turns on the observer-only OPL3 + PC-speaker sink in the viewer
    default_audio = "off"
    #: Ordered implementation providers. Import order never selects execution.
    default_provider_preference: tuple[str, ...] = ("interpreted-original",)

    def __init__(self, root: Path | str) -> None:
        #: the PORT repo root; artifacts (snapshots/replays/screenshots) live under it
        self.root = Path(root)
        self.artifacts_dir = self.root / "artifacts"

    # --- CLI ------------------------------------------------------------------

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Add game-specific flags.  Never rename or repurpose the standard set."""

    # --- execution planning ------------------------------------------------------

    def program_identity(self, args: argparse.Namespace) -> str:
        """Stable plan identity; ports override with their recovery-IR identity."""
        return f"frontend:{self.name}"

    def requested_capabilities(
        self, args: argparse.Namespace,
    ) -> frozenset[str]:
        """Capabilities implied by standard player options.

        Project frontends that replace :meth:`execution_configuration` must
        pass this set into their own configuration factory.  This keeps replay
        and snapshot policy attached to the canonical execution plan instead
        of re-deriving it in individual launch paths.
        """
        requested: set[str] = set()
        if args.record_replay or args.play_replay:
            requested.update({
                DependencyCapability.REPLAY.value,
                DependencyCapability.SNAPSHOTS.value,
            })
        if args.snapshot or args.save_snapshot:
            requested.add(DependencyCapability.SNAPSHOTS.value)
        return frozenset(requested)

    def execution_configuration(self, args: argparse.Namespace) -> ExecutionConfiguration:
        exe_path = str(args.exe or "")
        bootstrap = ExeBootstrapProvider(
            provider_id="original-exe-loader",
            state_outputs=(
                "initial machine state",
                "DOS process state",
                "loaded executable image",
            ),
            artifacts=(BootstrapArtifact(
                artifact_id="original-executable",
                source_path=exe_path,
                runtime_path=Path(exe_path).name or "PROGRAM.EXE",
                generation_instruction="provide --exe with the original program path",
            ),),
            runtime_required_capabilities=frozenset({
                DependencyCapability.ORIGINAL_CODE.value,
                DependencyCapability.INTERPRETER.value,
                DependencyCapability.CPU_MODEL.value,
                DependencyCapability.DOS_MEMORY.value,
                DependencyCapability.DOS_SERVICES.value,
                DependencyCapability.DOS_RE_RUNTIME.value,
            }),
            initialized_capabilities=frozenset({
                DependencyCapability.CPU_MODEL.value,
                DependencyCapability.DOS_MEMORY.value,
                DependencyCapability.DOS_SERVICES.value,
            }),
            valid_profiles=frozenset({"development", "verification"}),
            provider_digest="dos-re-original-exe-loader-v1",
        )
        return profile_configuration(
            args.profile,
            program_identity=self.program_identity(args),
            product_profile="default",
            provider_preference=self.default_provider_preference,
            requested_capabilities=self.requested_capabilities(args),
            bootstrap_provider=bootstrap,
        )

    def execution_coverage(self, args: argparse.Namespace) -> ProgramCoverage:
        """Coverage adapter for the current monolithic interpreted runtime."""
        program = self.program_identity(args)
        root = f"{program}:program"
        return ProgramCoverage(
            roots=(root,),
            reachable=frozenset({root}),
            evidence_identity=f"interpreted-program:{program}",
        )

    def execution_implementations(
        self, args: argparse.Namespace,
    ) -> ImplementationCatalog:
        root = f"{self.program_identity(args)}:program"
        return ImplementationCatalog((ImplementationEntry(ImplementationDescriptor(
            implementation_id="interpreted-original",
            targets=frozenset({root}),
            origin=ImplementationOrigin.INTERPRETED,
            properties=frozenset({"cpu-backed", "dos-memory-backed"}),
            required_capabilities=frozenset({
                DependencyCapability.ORIGINAL_EXE.value,
                DependencyCapability.ORIGINAL_CODE.value,
                DependencyCapability.INTERPRETER.value,
                DependencyCapability.CPU_MODEL.value,
                DependencyCapability.DOS_MEMORY.value,
                DependencyCapability.DOS_SERVICES.value,
                DependencyCapability.DOS_RE_RUNTIME.value,
            }),
            implementation_digest=_runtime_identity(),
        )),))

    def execution_services(
        self, args: argparse.Namespace,
    ) -> RuntimeServiceCatalog:
        return RuntimeServiceCatalog()

    def resolve_execution_plan(self, args: argparse.Namespace) -> ExecutionPlan:
        """Resolve before boot so strict profiles cannot touch forbidden runtime."""
        return plan_execution(
            self.execution_configuration(args),
            self.execution_coverage(args),
            self.execution_implementations(args),
            self.execution_services(args),
        )

    def launch(self, args: argparse.Namespace, plan: ExecutionPlan) -> int:
        """Run the selected plan through this frontend's execution driver."""
        return _launch_real_mode(self, args)

    def verification_drivers(
        self,
        args: argparse.Namespace,
        plan: ExecutionPlan,
        artifact: ReplayArtifact,
    ) -> tuple[ReplayDriver, ReplayDriver]:
        """Create oracle and candidate drivers for differential replay.

        A game frontend owns the runtime-specific adapters, but not the
        verification command, interval semantics, or persistence format.
        """
        raise RuntimeError(
            f"{type(self).__name__} does not provide differential replay drivers"
        )

    def bind_execution_plan(self, runtime, plan: ExecutionPlan) -> None:
        """Install selected providers through their declared backend activators."""
        targets_by_implementation: dict[str, list[str]] = {}
        for binding in plan.bindings:
            targets_by_implementation.setdefault(
                binding.implementation_id, []
            ).append(binding.target)
        for descriptor in plan.implementations:
            if descriptor.category is OverrideCategory.ENHANCEMENT:
                targets_by_implementation.setdefault(
                    descriptor.implementation_id, []
                ).extend(descriptor.targets)
        descriptors = {
            item.implementation_id: item for item in plan.implementations
        }
        for implementation_id, targets in sorted(targets_by_implementation.items()):
            descriptor = descriptors[implementation_id]
            entry = next(
                item for item in plan.catalog.entries
                if item.descriptor.implementation_id == implementation_id
            )
            if entry.activate is not None:
                entry.activate(runtime, tuple(sorted(targets)))
            elif descriptor.origin is not ImplementationOrigin.INTERPRETED:
                raise RuntimeError(
                    f"selected implementation {implementation_id!r} has no "
                    "backend activator"
                )

    def default_save_dir(self, args: argparse.Namespace) -> Path:
        """Where the live product persists the game's own saved files (progress,
        options, ...).  A sibling of the shipped assets so they stay pristine;
        override per game if a platform save location is preferred."""
        return self.root / "saves"

    # --- runtime construction ---------------------------------------------------

    def create_runtime(self, args: argparse.Namespace):
        """Boot a fresh runtime.  Ports override to call their own adapter's
        ``create_<game>_runtime`` (which installs their hooks)."""
        if not args.exe:
            raise SystemExit("--exe is required (this frontend has no default_exe)")
        args.execution_plan.require_capability(
            DependencyCapability.ORIGINAL_EXE,
            consumer=f"{type(self).__name__}.create_runtime",
        )
        args.execution_plan.require_capability(
            DependencyCapability.INTERPRETER,
            consumer=f"{type(self).__name__}.create_runtime",
        )
        from dos_re.runtime import create_runtime  # lazy: keeps the loader off
        return create_runtime(args.exe, game_root=args.game_root,  # an override's graph
                              command_tail=args.dos_args)

    def load_snapshot_runtime(self, args: argparse.Namespace, snapshot_dir: str | Path):
        """Resume from a snapshot directory."""
        args.execution_plan.require_capability(
            DependencyCapability.SNAPSHOTS,
            consumer=f"{type(self).__name__}.load_snapshot_runtime",
        )
        from dos_re.snapshot import load_snapshot  # lazy (see create_runtime)
        return load_snapshot(args.exe, snapshot_dir, game_root=args.game_root)

    # --- per-frame behaviour ------------------------------------------------------

    def advance_frame(self, rt, args: argparse.Namespace, frame: int) -> None:
        """Advance the VM one displayed/simulated frame.  THE pacing extension point."""
        from dos_re.interrupts import deliver_interrupt

        for _ in range(max(0, args.timer_irqs_per_frame)):
            deliver_interrupt(rt, 0x08)
        rt.cpu.run(args.steps_per_frame)

    def replay_point_coordinate(
        self, rt, args: argparse.Namespace, *, point_ordinal: int | None = None,
    ) -> tuple[str, object]:
        """Return the exact coordinate of the current replay boundary.

        The default is the low-level guest-instruction fallback.  Games should
        override this with a semantic tick/present/input boundary and use the
        ordinal only as its sequence identity, never as a host dispatch count.
        """
        return GUEST_INSTRUCTION_COORDINATE, int(rt.cpu.instruction_count)

    def advance_replay_frame(
        self,
        rt,
        args: argparse.Namespace,
        frame: int,
        coordinate: ReplayPointCoordinate,
    ) -> None:
        """Advance to a declared point without using backend dispatch counts.

        ``CPU.run(N)`` means N calls to ``step``. A generated function may
        account thousands of guest instructions in one call, so that host
        dispatch count is never a valid cross-backend replay coordinate.
        """
        if coordinate.schema_id != GUEST_INSTRUCTION_COORDINATE:
            raise ReplayError(
                f"unsupported real-mode replay coordinate: {coordinate.schema_id!r}")
        from dos_re.interrupts import deliver_interrupt

        for _ in range(max(0, args.timer_irqs_per_frame)):
            deliver_interrupt(rt, 0x08)
        target = int(coordinate.value)
        if rt.cpu.instruction_count > target:
            raise ReplayError(
                f"replay point {coordinate.point.ordinal} is behind the runtime: "
                f"{target} < {rt.cpu.instruction_count}")
        while rt.cpu.instruction_count < target:
            rt.cpu.step()
            if rt.cpu.instruction_count > target:
                raise ReplayError(
                    f"implementation crossed replay point "
                    f"{coordinate.point.ordinal}: {rt.cpu.instruction_count} > {target}; "
                    "the selected implementation needs a resumable boundary")

    def decode_frame(self, rt):
        """Return the current screen as an HxWx3 uint8 array."""
        from dos_re.framebuffer import decode_frame_default

        return decode_frame_default(rt)

    def deliver_input(self, rt, scancode: int) -> None:
        """Deliver one XT scancode to the game (override e.g. to bound ISR steps)."""
        from dos_re.interrupts import deliver_scancode

        deliver_scancode(rt, scancode)

    # --- replay determinism ---------------------------------------------------------

    def replay_metadata(self, args: argparse.Namespace) -> dict[str, object]:
        """Reproducibility knobs a replay must match to stay deterministic."""
        return {
            "game": self.name,
            "exe": Path(args.exe).name if args.exe else "",
            "command_tail": args.dos_args,
            "steps_per_frame": int(args.steps_per_frame),
            "timer_irqs_per_frame": int(args.timer_irqs_per_frame),
        }

    def apply_replay_metadata(self, args: argparse.Namespace, meta: dict) -> None:
        """Restore the recorded pacing knobs before a replay."""
        if "steps_per_frame" in meta:
            args.steps_per_frame = int(meta["steps_per_frame"])
        if "timer_irqs_per_frame" in meta:
            args.timer_irqs_per_frame = int(meta["timer_irqs_per_frame"])

    def replay_device_identity(self, args: argparse.Namespace, rt) -> str:
        """Identify the concrete deterministic device topology.

        Runtime and implementation source digests identify device *code*; this
        identity also records which optional devices were attached and their
        immutable configuration.  A sound-disabled runtime must never reuse a
        cache captured with PIC/Sound Blaster continuation state, or vice
        versa.
        """
        dos = getattr(rt, "dos", None)
        pic = getattr(dos, "pic", None)
        sound_blaster = getattr(dos, "sound_blaster", None)

        def type_name(value) -> str:
            if value is None:
                return "absent"
            cls = type(value)
            return f"{cls.__module__}.{cls.__qualname__}"

        topology = (
            ("dos", type_name(dos)),
            ("pic", type_name(pic)),
            ("sound_blaster", type_name(sound_blaster)),
            ("sound_blaster.base", getattr(sound_blaster, "base", None)),
            ("sound_blaster.irq", getattr(sound_blaster, "irq", None)),
            ("sound_blaster.dma", getattr(sound_blaster, "dma", None)),
            (
                "sound_blaster.detection_only",
                getattr(sound_blaster, "detection_only", None),
            ),
        )
        digest = hashlib.sha256(repr(topology).encode("utf-8")).hexdigest()
        return f"dos-re-real-mode-devices-v2:{digest}"

    def replay_profile(
        self, args: argparse.Namespace, rt,
    ) -> ReplayExecutionIdentity:
        """Stable identity used to invalidate profile-local replay caches."""
        plan = args.execution_plan
        only_interpreted = bool(plan.implementations) and all(
            implementation.origin is ImplementationOrigin.INTERPRETED
            for implementation in plan.implementations
        )
        role = (
            "oracle"
            if only_interpreted and not plan.configuration.selected_overrides
            else "candidate"
        )
        mode = plan.configuration.profile
        composition_digest = execution_composition_digest(plan)
        implementation = hashlib.sha256(
            f"{_implementation_identity(self)}:{composition_digest}".encode("utf-8")
        ).hexdigest()
        image = _content_identity(args.exe)
        runtime = _runtime_identity()
        devices = self.replay_device_identity(args, rt)
        continuation_schema = "dos-re-real-mode-continuation-v1"
        projection_schema = "dos-re-complete-machine-v1"
        key = hashlib.sha256(
            repr((
                mode,
                role,
                implementation,
                image,
                runtime,
                devices,
                continuation_schema,
                projection_schema,
            )).encode("utf-8")
        ).hexdigest()[:12]
        return ReplayExecutionIdentity(
            profile_id=f"real-mode-{mode}-{key}",
            role=role,
            implementation=implementation,
            image=image,
            runtime=runtime,
            devices=devices,
            continuation_schema=continuation_schema,
            projection_schema=projection_schema,
        )

    def capture_replay_state(self, rt, *, event_cursor: int):
        return capture_runtime_continuation(rt, event_cursor=event_cursor)

    def apply_replay_state(self, rt, state) -> None:
        apply_runtime_continuation(rt, state)

    # --- presentation ------------------------------------------------------------

    def window_title(self, args: argparse.Namespace, mode: str) -> str:
        exe = Path(args.exe).name if args.exe else self.name
        return f"{exe} -- dos_re VM ({mode})"

    def create_audio_sink(self, pygame, rt, args: argparse.Namespace):
        """Viewer audio, or None.  The default honours ``--audio adlib`` with the
        observer-only OPL3 + PC-speaker sink (never affects game state; replays
        replay identically with audio on or off).  Ports with another audio
        architecture (e.g. digital SB-DMA) override this; the returned object
        just needs a ``pump()`` method called once per presented frame."""
        if args.audio != "adlib":
            return None
        from dos_re.audio_sink import AdlibSpeakerSink

        sink = AdlibSpeakerSink(pygame, rt, args.present_hz)
        return sink if sink.available else None


# --- CLI ------------------------------------------------------------------------------------------

def build_arg_parser(frontend: GameFrontend,
                     description: str | None = None) -> argparse.ArgumentParser:
    """The STANDARD play.py CLI.  Ports add flags via ``frontend.add_arguments``."""
    p = argparse.ArgumentParser(description=description,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    boot = p.add_argument_group("boot")
    boot.add_argument("--exe", default=frontend.default_exe,
                      help="path to the original MZ executable")
    boot.add_argument("--game-root", default=frontend.default_game_root,
                      help="directory containing the game's data files")
    boot.add_argument("--dos-args", default=frontend.default_dos_args,
                      help="raw DOS command tail to pass to the executable")

    mode = p.add_argument_group("run mode")
    mode.add_argument(
        "--profile",
        default="development",
        choices=("development", "verification", "detached", "release"),
        help="execution policy preset; recovery level is selected per implementation",
    )
    mode.add_argument(
        "--plan-only",
        action="store_true",
        help="resolve and print the execution/detachment plan without booting",
    )
    mode.add_argument("--headless", action="store_true",
                      help="skip the live pygame viewer (default: viewer on)")
    mode.add_argument("--frames", type=int, default=0,
                      help="exit after N frames (0 = run until closed; headless smokes)")
    mode.add_argument("--steps", type=int, default=None,
                      help="max VM instructions to execute (headless default: 1,000,000)")

    snap = p.add_argument_group("snapshots")
    snap.add_argument("--snapshot", help="continue from an existing snapshot directory")
    snap.add_argument("--save-snapshot", nargs="?", const="auto",
                      help="save a VM snapshot on exit; optional directory path")

    replay = p.add_argument_group("replays")
    replay.add_argument("--record-replay", metavar="NAME",
                      help="(viewer) start recording an input replay immediately "
                           "(F11 toggles at any time)")
    replay.add_argument("--play-replay", metavar="DIR",
                      help="play a ReplayArtifact directory (viewer unless --headless)")
    replay.add_argument("--replay-continue", action="store_true",
                      help="(with --play-replay) when the replay ends, hand the game over "
                           "to live player input instead of stopping")
    replay.add_argument("--replay-dir", default=str(frontend.artifacts_dir / "replays"),
                      help="directory to write recorded replays into")

    verify = p.add_argument_group("verification")
    verify.add_argument("--verify-start", type=int,
                        help="first stable replay point (verification profile)")
    verify.add_argument("--verify-end", type=int,
                        help="last stable replay point (verification profile)")
    verify.add_argument("--bisect", action="store_true",
                        help="locate the first divergent transition in the interval")
    verify.add_argument(
        "--verify-mode",
        choices=("checkpointed", "semantic-points", "endpoint"),
        default="checkpointed",
        help="checkpointed observes every semantic point and external effect; "
             "semantic-points is the span-1 reference; endpoint is the cheapest "
             "but can miss a divergence that reconverges before the end",
    )
    verify.add_argument(
        "--verify-checkpoint-span", type=int, default=64,
        help="full comparison interval for checkpointed verification",
    )
    verify.add_argument(
        "--verify-observables", action=argparse.BooleanOptionalAction,
        default=True,
        help="include adapter-declared I/O/device/interrupt/input effects",
    )

    pace = p.add_argument_group("pacing")
    pace.add_argument("--present-hz", type=int, default=frontend.default_present_hz,
                      help="viewer presents per second")
    pace.add_argument("--steps-per-frame", type=int,
                      default=frontend.default_steps_per_frame,
                      help="VM instructions per displayed/simulated frame")
    pace.add_argument("--timer-irqs-per-frame", type=int,
                      default=frontend.default_timer_irqs_per_frame,
                      help="INT 08h timer ticks delivered per frame (games that idle "
                           "on the PIT ISR hang forever without this)")

    view = p.add_argument_group("presentation")
    view.add_argument("--scale", type=int, default=frontend.default_scale,
                      help="initial viewer window scale")
    view.add_argument("--square-pixels", action="store_true",
                      help="par=1.0 instead of the DOS 4:3 look (par=1.2)")
    view.add_argument("--audio", default=frontend.default_audio,
                      choices=("adlib", "off"),
                      help="viewer audio: 'adlib' = observer-only OPL3 + PC-speaker "
                           "sink (never affects game state); 'off'")

    save = p.add_argument_group("persistence")
    save.add_argument("--save-dir", default=None,
                      help="directory for the game's own saved files (e.g. progress "
                           "written via INT 21h); default: <root>/saves in the live "
                           "viewer.  Reads prefer it over the shipped assets, which "
                           "stay pristine.")
    save.add_argument("--no-save", action="store_true",
                      help="never persist the game's file writes (fully deterministic); "
                           "the default for headless and replay playback regardless")
    save.add_argument("--save", action="store_true",
                      help="force-enable persistence even under replay playback "
                           "(off by default while replaying, to stay deterministic)")

    frontend.add_arguments(p)
    return p


# --- shared run-loop plumbing ---------------------------------------------------------------------

def _save_exit_snapshot(frontend: GameFrontend, rt, args, *, status: str) -> None:
    if not args.save_snapshot:
        return
    out = (_timestamp_dir(frontend.artifacts_dir, f"snapshot_{frontend.name}")
           if args.save_snapshot == "auto" else Path(args.save_snapshot))
    write_snapshot(rt, out, status=status, steps=rt.cpu.instruction_count, trace_tail=())
    print(f"snapshot: {out}")


def _save_gap_snapshot(frontend: GameFrontend, rt, *, status: str) -> None:
    """Any unhandled VM exception leaves a resumable snapshot for diagnosis."""
    try:
        out = _timestamp_dir(frontend.artifacts_dir, f"gap_snapshot_{frontend.name}")
        write_snapshot(rt, out, status=status, steps=rt.cpu.instruction_count, trace_tail=())
        print(f"gap snapshot saved: {out}")
    except Exception as save_exc:  # noqa: BLE001
        print(f"(could not save gap snapshot: {save_exc})")


def _diagnostic_lines(rt) -> list[str]:
    """Game-agnostic context printed on any halt/crash, cheap enough to
    always compute (no per-instruction tracing overhead): DOS console output
    (many DOS programs print a plain-text reason — "Not enough memory",
    "Cannot find X" — before exiting, which otherwise vanishes silently), a
    compact DOS memory-allocator summary, and open file handles (useful when
    the failure is mid asset-load). "program halted" alone hides all of this."""
    lines = []
    dos = getattr(rt, "dos", None)
    if dos is None:
        return lines
    stdout = "".join(getattr(dos, "stdout", [])).strip()
    if stdout:
        lines.append(f"dos stdout: {stdout!r}")
    allocs = getattr(dos, "allocations", None)
    if allocs:
        total = sum(allocs.values()) * 16
        lines.append(f"dos memory: {len(allocs)} live allocations, {total:,} bytes; "
                     f"next_alloc_segment={dos.next_alloc_segment:04X} "
                     f"limit={dos.allocation_limit_segment:04X}")
    files = getattr(dos, "files", None)
    if files:
        names = ", ".join(f"{h}:{fh.path.name}@{fh.pos}/{len(fh.data)}" for h, fh in sorted(files.items()))
        lines.append(f"open files: {names}")
    return lines


def _exit_report(rt, *, status: str, frames: int) -> int:
    print(f"status: {status}")
    for line in _diagnostic_lines(rt):
        print(f"  {line}")
    print(f"frames: {frames}  steps: {rt.cpu.instruction_count:,}")
    print(f"cpu: {rt.cpu.s.snapshot()}")
    return 0 if not status.startswith(("unsupported", "exception")) else 1


def _step_frame(
    frontend: GameFrontend, rt, args, frame: int,
    *, replay_coordinate: ReplayPointCoordinate | None = None,
) -> tuple[str | None, bool]:
    """One guarded frame advance.  Returns (status_or_None, keep_running).

    Every failure mode prints the cheap diagnostics (_diagnostic_lines) and
    saves a resumable gap snapshot — not just unhandled exceptions. A bare
    "program halted"/"unsupported instruction" with no further context meant
    the only way to diagnose it was to reproduce it by hand from scratch."""
    try:
        if replay_coordinate is None:
            frontend.advance_frame(rt, args, frame)
        else:
            frontend.advance_replay_frame(
                rt, args, frame, replay_coordinate)
    except ConsoleInputWouldBlock:
        return "waiting for DOS key", True
    except HaltExecution:
        status = "program halted"
        for line in _diagnostic_lines(rt):
            print(f"  {line}")
        _save_gap_snapshot(frontend, rt, status=status)
        return status, False
    except UnsupportedInstruction as exc:
        status = f"unsupported instruction: {exc}"
        for line in _diagnostic_lines(rt):
            print(f"  {line}")
        _save_gap_snapshot(frontend, rt, status=status)
        return status, False
    except Exception as exc:  # noqa: BLE001 — keep bring-up useful
        import traceback
        traceback.print_exc()
        status = f"exception: {type(exc).__name__}: {exc}"
        for line in _diagnostic_lines(rt):
            print(f"  {line}")
        _save_gap_snapshot(frontend, rt, status=status)
        return status, False
    return None, True


# --- headless -----------------------------------------------------------------------------------

def run_replay_headless(frontend: GameFrontend, rt, args,
                        playback: _RealReplayPlayback) -> int:
    """Fast deterministic replay playback: no pygame, no pacing, no presentation."""
    # Mouse detection changes startup control flow, so replay the explicit
    # recording-time answer even when the pointer never moved.
    rt.dos.mouse_present = playback.mouse_present_hint
    frame = 0
    status = "replay playback complete"
    while not playback.finished(frame):
        if args.frames and frame >= args.frames:
            status = f"frame budget reached ({args.frames})"
            break
        playback.apply_to_runtime(frame, rt, deliver=lambda r, sc: frontend.deliver_input(r, sc))
        new_status, keep_running = _step_frame(
            frontend, rt, args, frame,
            replay_coordinate=playback.coordinate_after(frame),
        )
        if new_status:
            status = new_status
        if not keep_running:
            break
        frame += 1
    print(f"events_applied={playback.next_event_index}/{len(playback.events)}")
    _save_exit_snapshot(frontend, rt, args, status=status)
    return _exit_report(rt, status=status, frames=frame)


def run_headless(frontend: GameFrontend, rt, args) -> int:
    """Bounded headless run (no replay): the snapshot-for-study workhorse."""
    steps_budget = args.steps
    if steps_budget is None and not args.frames:
        steps_budget = 1_000_000
        print(f"(headless with no --steps/--frames: defaulting to --steps {steps_budget:,})")
    frame = 0
    status = "budget reached"
    while True:
        if args.frames and frame >= args.frames:
            break
        if steps_budget is not None and rt.cpu.instruction_count >= steps_budget:
            break
        new_status, keep_running = _step_frame(frontend, rt, args, frame)
        if new_status:
            status = new_status
        if not keep_running:
            break
        frame += 1
    _save_exit_snapshot(frontend, rt, args, status=status)
    return _exit_report(rt, status=status, frames=frame)


# --- the viewer -----------------------------------------------------------------------------------

def run_view(frontend: GameFrontend, rt, args,
             playback: _RealReplayPlayback | None = None) -> int:
    """The live pygame viewer: hybrid play, replay record/replay, F10/F11/F12.

    Every recording captures a complete continuation base in ReplayArtifact.
    """
    try:
        import numpy as np
        import pygame
    except ImportError as exc:  # the viewer extras; the headless paths need neither
        missing = exc.name or "numpy/pygame"
        hint = ("pip install numpy pygame" if "pypy" not in sys.version.lower()
                else "pypy -m pip install numpy pygame-ce  "
                     "(the community fork; upstream pygame has no PyPy wheel)")
        raise SystemExit(
            f"the live viewer needs {missing!r}, which is not installed.\n"
            f"  install it:  {hint}\n"
            f"  or run without a window:  add --headless "
            f"(snapshots, replay playback and every verifier work headless)"
        ) from exc

    from dos_re.display import Display

    replaying = playback is not None

    # Persistence policy: the live viewer saves the game's own file writes so
    # progress survives; replay playback stays deterministic (off) unless --save is
    # given; --no-save forces it off.  Reads still prefer save_dir over the
    # shipped assets, which are never mutated.
    if not getattr(args, "no_save", False) and (not replaying or getattr(args, "save", False)):
        override = getattr(args, "save_dir", None)
        rt.dos.save_dir = Path(override) if override else frontend.default_save_dir(args)

    # Mouse present when playing live; when replaying, only if the replay carries
    # mouse input (a keyboard-only replay must reproduce with the mouse absent).
    rt.dos.mouse_present = playback.mouse_present_hint if replaying else True

    pygame.init()
    first = np.asarray(frontend.decode_frame(rt), np.uint8)
    fh, fw = first.shape[:2]
    par = 1.0 if args.square_pixels else 1.2
    display = Display((fw * args.scale, int(fh * par) * args.scale),
                      title=frontend.window_title(args, "replay" if replaying else "live"))
    display.par = par
    scancodes = scancode_table(pygame)
    clock = pygame.time.Clock()

    frame_box = {"n": 0}
    recorder: dict[str, _RealReplayRecorder | None] = {"rec": None}
    last_rgb = [first]

    def start_recording(name: str) -> None:
        # Pin the mouse-presence state into the replay so replay reproduces it
        # exactly (a keyboard replay recorded with the mouse present must NOT
        # replay it absent, and vice-versa) -- independent of whether the replay
        # happens to carry any mouse motion.
        meta = dict(frontend.replay_metadata(args))
        meta["mouse_present"] = bool(getattr(rt.dos, "mouse_present", False))
        rec = _RealReplayRecorder(
            frontend, args, rt, root=Path(args.replay_dir),
            name=name, metadata=meta)
        out = rec.start(boundary=frame_box["n"])
        recorder["rec"] = rec
        mouse = "mouse" if meta["mouse_present"] else "no-mouse"
        print(f"recording replay [embedded base, {mouse}] -> {out}")

    def stop_recording() -> None:
        rec = recorder["rec"]
        if rec is not None and rec.active:
            out = rec.stop(frontend, rt, boundary=frame_box["n"])
            print(f"saved replay ({rec.event_count} events) -> {out}")
        recorder["rec"] = None

    def live_input(scancode: int) -> None:
        frontend.deliver_input(rt, scancode)
        rec = recorder["rec"]
        if rec is not None and rec.active:
            rec.record_scan(boundary=frame_box["n"], scancode=scancode)

    # Live mouse -> INT 33h driver state (real-mode DOSMachine.set_mouse_norm).
    # Only used by mouse-driven games; a no-op when the runtime has no such API.
    _set_mouse = getattr(rt.dos, "set_mouse_norm", None)
    mouse_btn = [0]  # Microsoft mask: bit0=left, bit1=right, bit2=middle
    mouse_norm = [None]  # latest host (u, v); None until the mouse first moves/clicks

    def feed_mouse(pos) -> None:
        # Map through the LETTERBOX -- the rect the frame was actually drawn
        # into -- not the window.  The two only agree while the window matches
        # the frame's aspect; whenever they differ the frame is centred inside
        # black bars, and a window-relative mapping both skews the pointer and
        # offsets it by the bar size.  That is not an exotic case: this viewer
        # sizes its window from the FIRST frame's dimensions, so any game whose
        # video mode later changes shape (VGA Lemmings: 320x200 gameplay vs
        # 640x350 menus) is letterboxed for the rest of the session -- and it
        # is every fullscreen window and every phone screen.
        # (Display owns the letterbox math, so it owns the inverse; before the
        # first frame there is no rect yet -- fall back to the window.)
        if _set_mouse is None:
            return
        uv = display.window_to_frame_norm(pos)
        if uv is None:
            w, h = display.get_size()
            uv = (pos[0] / max(1, w - 1), pos[1] / max(1, h - 1))
        u, v = uv
        mouse_norm[0] = (u, v)
        if recorder["rec"] is None:
            # Live: apply immediately.  Quantized exactly like a recording, so
            # toggling recording on/off never changes where the pointer lands.
            _set_mouse(*mouse_sample(u, v, mouse_btn[0]))
        # While recording, application is deferred to the once-per-boundary
        # sample below so the VM sees exactly the recorded state (host events
        # only reach us between frames, so nothing is delayed by this).

    def sample_mouse_for_replay() -> None:
        # Once per frame while recording: record the deduped mouse sample keyed
        # to the same boundary as scancodes, and apply THE SAMPLE (not the
        # full-precision host value) to the VM.  Applied EVERY frame, changed or
        # not, because set_mouse_norm re-maps through the game's current INT 33h
        # range — replay mirrors this (the proven PM recorder/replay design).
        rec = recorder["rec"]
        if rec is None or not rec.active or _set_mouse is None or mouse_norm[0] is None:
            return
        u, v = mouse_norm[0]
        _set_mouse(*rec.record_mouse(boundary=frame_box["n"], u=u, v=v, buttons=mouse_btn[0]))

    def screenshot() -> None:
        rgb = last_rgb[0]
        if rgb is None:
            return
        h, w = rgb.shape[0], rgb.shape[1]
        surf = pygame.image.frombuffer(np.ascontiguousarray(rgb).tobytes(), (w, h), "RGB")
        out = frontend.artifacts_dir / f"shot_{frontend.name}_{datetime.now():%Y%m%d_%H%M%S}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        pygame.image.save(surf, str(out))
        print(f"screenshot: {out}")

    dispatcher = KeyDispatcher(live_input)

    _VIEWER_HOTKEYS = {pygame.K_F10, pygame.K_F11, pygame.K_F12}

    audio = frontend.create_audio_sink(pygame, rt, args)
    running = True
    status = "replaying" if replaying else "running"

    if not replaying and args.record_replay:
        start_recording(args.record_replay)

    try:
        while running and (args.frames == 0 or frame_box["n"] < args.frames):
            if args.steps is not None and rt.cpu.instruction_count >= args.steps:
                status = f"step budget reached ({args.steps:,})"
                break
            if replaying and playback.finished(frame_box["n"]):
                if args.replay_continue:
                    replaying = False
                    status = "replay finished -- live input"
                    print(status)
                else:
                    status = "replay playback complete"
                    break

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.VIDEORESIZE:
                    display.resize(event.w, event.h)
                elif event.type == pygame.KEYDOWN and event.key == pygame.K_F12:
                    out = _timestamp_dir(frontend.artifacts_dir, f"snapshot_{frontend.name}")
                    write_snapshot(rt, out, status="manual viewer snapshot",
                                   steps=rt.cpu.instruction_count, trace_tail=())
                    print(f"snapshot: {out}")
                elif event.type == pygame.KEYDOWN and event.key == pygame.K_F10:
                    screenshot()
                elif replaying:
                    continue  # ignore host keys while a replay drives input
                elif event.type == pygame.KEYDOWN and event.key == pygame.K_F11:
                    if recorder["rec"] is None:
                        start_recording(args.record_replay or frontend.name)
                    else:
                        stop_recording()
                elif event.type in (pygame.KEYDOWN, pygame.KEYUP) and event.key in _VIEWER_HOTKEYS:
                    # F10/F11/F12 are viewer hotkeys, not game keys.  F10's make
                    # is consumed above, but its break would otherwise fall to the
                    # generic KEYUP path and leak a stray break code into the game
                    # (and the replay).  Swallow both edges. (pm_backend parity.)
                    continue
                elif event.type == pygame.KEYDOWN:
                    sc = scancodes.get(event.key)
                    if sc is not None:
                        dispatcher.post_down(sc)
                elif event.type == pygame.KEYUP:
                    sc = scancodes.get(event.key)
                    if sc is not None:
                        dispatcher.post_up(sc)
                elif event.type == pygame.MOUSEMOTION:
                    feed_mouse(event.pos)
                elif event.type in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP):
                    bit = {1: 0x01, 3: 0x02, 2: 0x04}.get(event.button)
                    if bit is not None:
                        if event.type == pygame.MOUSEBUTTONDOWN:
                            mouse_btn[0] |= bit
                        else:
                            mouse_btn[0] &= ~bit
                    feed_mouse(event.pos)
                elif event.type == pygame.MOUSEWHEEL:
                    # The game has no wheel of its own (its INT 33h use is
                    # absolute position + buttons only); DOSBox maps the wheel to
                    # the Up/Down cursor keys, which its menus navigate on.  Feed
                    # one arrow tap per notch through the dispatcher so it records
                    # as a normal scan event and replays deterministically.
                    sc = scancodes.get(pygame.K_UP if event.y > 0 else pygame.K_DOWN)
                    if sc is not None:
                        for _ in range(max(1, abs(event.y))):
                            dispatcher.post_down(sc)
                            dispatcher.post_up(sc)

            if replaying:
                playback.apply_to_runtime(frame_box["n"], rt,
                                          deliver=lambda r, sc: frontend.deliver_input(r, sc))
            else:
                dispatcher.pump()
                sample_mouse_for_replay()

            new_status, keep_running = _step_frame(
                frontend, rt, args, frame_box["n"],
                replay_coordinate=(
                    playback.coordinate_after(frame_box["n"])
                    if replaying else None
                ),
            )
            if new_status:
                status = new_status
            running = running and keep_running
            rec = recorder["rec"]
            if rec is not None and rec.active:
                rec.mark(
                    frontend, args, rt, boundary=frame_box["n"] + 1)

            if audio is not None:
                audio.pump()
            rgb = np.asarray(frontend.decode_frame(rt), np.uint8)
            last_rgb[0] = rgb
            display.draw_game(rgb)
            display.flip()
            pygame.display.set_caption(
                f"{frontend.window_title(args, 'replay' if replaying else 'live')} | {status} | "
                f"frame={frame_box['n']} steps={rt.cpu.instruction_count:,} | "
                f"CS:IP={rt.cpu.s.cs:04X}:{rt.cpu.s.ip:04X}"
                + (" | REC" if recorder["rec"] is not None else "")
            )
            frame_box["n"] += 1
            clock.tick(args.present_hz)
    finally:
        stop_recording()
        pygame.quit()

    _save_exit_snapshot(frontend, rt, args, status=status)
    return _exit_report(rt, status=status, frames=frame_box["n"])


def _launch_real_mode(frontend: GameFrontend, args: argparse.Namespace) -> int:
    if args.play_replay:
        playback = _RealReplayPlayback.open(args.play_replay)
        frontend.apply_replay_metadata(args, playback.artifact.metadata)
        rt = frontend.create_runtime(args)
        base = playback.artifact.restore(
            playback.profile, ReplayPoint(0, playback.artifact.timeline_id))
        frontend.apply_replay_state(rt, base)
        frontend.bind_execution_plan(rt, args.execution_plan)
        playback.adapter.seek(base.event_cursor)
        current = frontend.replay_profile(args, rt)
        for field in ("image", "runtime", "devices", "continuation_schema"):
            if getattr(current, field) != getattr(playback.profile, field):
                raise ValueError(
                    f"replay {field} identity differs from the recorded base")
        registered = {profile.profile_id for profile, _ in playback.artifact.profiles()}
        if current.profile_id not in registered:
            playback.artifact.register_profile(
                current,
                base_point=ReplayPoint(0, playback.artifact.timeline_id),
                base_state=base,
            )
        else:
            playback.artifact.require_profile(current)
        rt.dos.console_input_fallback = None
        if args.headless:
            return run_replay_headless(frontend, rt, args, playback)
        return run_view(frontend, rt, args, playback=playback)

    if args.snapshot:
        rt = frontend.load_snapshot_runtime(args, args.snapshot)
    else:
        rt = frontend.create_runtime(args)
    frontend.bind_execution_plan(rt, args.execution_plan)
    rt.dos.console_input_fallback = None
    if args.headless:
        return run_headless(frontend, rt, args)
    return run_view(frontend, rt, args)


def _run_differential_verification(
    frontend: GameFrontend,
    args: argparse.Namespace,
    plan: ExecutionPlan,
) -> int:
    if not args.play_replay:
        raise SystemExit("--profile verification requires --play-replay")
    if args.verify_start is None or args.verify_end is None:
        raise SystemExit(
            "--profile verification requires --verify-start and --verify-end"
        )
    artifact = ReplayArtifact.open(args.play_replay)
    frontend.apply_replay_metadata(args, artifact.metadata)
    start = ReplayPoint(args.verify_start, artifact.timeline_id)
    end = ReplayPoint(args.verify_end, artifact.timeline_id)
    oracle, candidate = frontend.verification_drivers(args, plan, artifact)
    if args.bisect:
        points = tuple(
            ReplayPoint(ordinal, artifact.timeline_id)
            for ordinal in range(args.verify_start, args.verify_end + 1)
        )
        found = bisect_divergence(artifact, oracle, candidate, points)
        if found is None:
            print(f"EQUIVALENT {start.ordinal}..{end.ordinal}")
            return 0
        before, after, result = found
        print(f"DIVERGENT transition {before.ordinal}->{after.ordinal}")
    elif getattr(args, "verify_mode", "endpoint") != "endpoint":
        span = (
            1 if getattr(args, "verify_mode", "endpoint") == "semantic-points"
            else getattr(args, "verify_checkpoint_span", 64))
        checked = verify_checkpointed(
            artifact, oracle, candidate, start, end,
            checkpoint_span=span,
            observable_effects=getattr(args, "verify_observables", True),
        )
        result = checked.result
        guarantee = (
            "semantic+observable" if checked.observable_effects
            else "semantic-boundaries")
        if result.equivalent:
            print(
                f"EQUIVALENT {start.ordinal}..{end.ordinal} "
                f"{result.comparison.oracle_digest} "
                f"mode={guarantee} points={checked.points_observed} "
                f"checkpoints={checked.checkpoints_compared} span={span} "
                f"effects={checked.observable_event_count}"
            )
            return 0
        assert checked.failed_interval is not None
        before, after = checked.failed_interval
        print(
            f"DIVERGENT transition {before.ordinal}->{after.ordinal} "
            f"mode={guarantee}"
        )
    else:
        result = verify_interval(artifact, oracle, candidate, start, end)
        if result.equivalent:
            print(
                f"EQUIVALENT {start.ordinal}..{end.ordinal} "
                f"{result.comparison.oracle_digest}"
            )
            return 0
        print(f"DIVERGENT {start.ordinal}..{end.ordinal}")
    for difference in result.comparison.differences:
        print("  " + difference)
    return 1


def main(frontend: GameFrontend, argv: list[str] | None = None,
         description: str | None = None) -> int:
    """Resolve one canonical execution plan, then dispatch its frontend driver."""
    args = build_arg_parser(frontend, description).parse_args(argv)
    try:
        args.execution_plan = frontend.resolve_execution_plan(args)
    except (ExecutionPlanError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.plan_only:
        print(format_execution_plan(args.execution_plan))
        return 0
    if args.execution_plan.configuration.verification_policy.mode == "differential":
        return _run_differential_verification(
            frontend, args, args.execution_plan
        )
    return frontend.launch(args, args.execution_plan)
