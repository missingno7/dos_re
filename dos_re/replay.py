"""dos_re 3.0 deterministic oracle/candidate replay infrastructure.

One :class:`ReplayArtifact` owns the deterministic event stream, stable
timeline, function visits, annotations, and independent continuation caches
for every execution profile.  A profile may be the original interpreter, a
VM-backed hook set, a CPUless/DOS-memory-backed override set, or a detached
native implementation.

Continuation state is deliberately distinct from comparison state:

* :class:`ContinuationState` is private to an execution profile and contains
  everything required to resume it deterministically.  It is cached as full
  metadata plus base-relative changed pages.
* :class:`CanonicalState` is the authoritative projection compared between
  oracle and candidate.  Machine-backed profiles may project raw machine
  state; detached native profiles project the same semantic schema from their
  own representation.

No legacy replay, suffix, snapshot, or repro format is read here.  Version 1 is
the first dos_re 3.0 format and intentionally has no migration path.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import shutil
import tempfile
import uuid
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from .observable import (
    ObservableIntervalDigest,
    RollingEffectDigest,
    SEMANTIC_BOUNDARY,
)

FORMAT_VERSION = 1
DEFAULT_PAGE_SIZE = 4096
MANIFEST = "replay.json"
GUEST_INSTRUCTION_COORDINATE = "dos-re:guest-instruction-count:v1"


class ReplayError(RuntimeError):
    """Invalid, corrupt, stale, or non-deterministic replay state."""


class StaleReplayError(ReplayError):
    """The artifact/cache identity does not match the requested execution."""


class ConcurrentReplayWriterError(ReplayError):
    """Another process is currently mutating this replay artifact."""


@dataclass(frozen=True)
class ReplayPoint:
    """A stable position on one artifact's canonical total-order timeline."""

    ordinal: int
    timeline_id: str

    def __post_init__(self) -> None:
        if not self.timeline_id:
            raise ValueError("timeline_id must not be empty")
        if int(self.ordinal) < 0:
            raise ValueError("point ordinal must be non-negative")
        object.__setattr__(self, "ordinal", int(self.ordinal))

    @property
    def key(self) -> str:
        timeline = _sha256(self.timeline_id.encode("utf-8"))[:16]
        return f"{timeline}-{self.ordinal:016x}"

    def to_json(self) -> dict[str, Any]:
        return {"timeline_id": self.timeline_id, "ordinal": self.ordinal, "key": self.key}

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> "ReplayPoint":
        point = cls(int(raw["ordinal"]), str(raw["timeline_id"]))
        if raw.get("key", point.key) != point.key:
            raise ReplayError("replay point key does not match its timeline and ordinal")
        return point


@dataclass(frozen=True)
class ReplayPointCoordinate:
    """Backend-neutral declaration of where a timeline point actually stops.

    An ordinal orders points; it does not by itself make the stop reproducible
    across implementations with different dispatch granularity.  The frontend
    chooses a coordinate schema (guest instruction count, simulation tick,
    presentation fence, native transaction id, ...), and every replay driver
    for that timeline must stop at the declared coordinate exactly.
    """

    point: ReplayPoint
    schema_id: str
    value: Any

    def __post_init__(self) -> None:
        if not self.schema_id:
            raise ValueError("timeline coordinate schema_id must not be empty")
        object.__setattr__(
            self, "value", _json_value(self.value, "timeline coordinate value"))

    def to_json(self) -> dict[str, Any]:
        return {
            "point": self.point.to_json(),
            "schema_id": self.schema_id,
            "value": self.value,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> "ReplayPointCoordinate":
        return cls(
            ReplayPoint.from_json(raw["point"]),
            str(raw["schema_id"]),
            raw.get("value"),
        )


@dataclass(frozen=True)
class ReplayEvent:
    """One deterministic external event applied at a stable point."""

    point: ReplayPoint
    sequence: int
    channel: str
    payload: Any

    def __post_init__(self) -> None:
        if int(self.sequence) < 0:
            raise ValueError("event sequence must be non-negative")
        if not self.channel:
            raise ValueError("event channel must not be empty")
        object.__setattr__(self, "sequence", int(self.sequence))
        object.__setattr__(self, "payload", _json_value(self.payload, "event payload"))

    def to_json(self) -> dict[str, Any]:
        return {
            "point": self.point.to_json(), "sequence": self.sequence,
            "channel": self.channel, "payload": self.payload,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> "ReplayEvent":
        return cls(ReplayPoint.from_json(raw["point"]), int(raw["sequence"]),
                   str(raw["channel"]), raw.get("payload"))


@dataclass(frozen=True)
class ContinuationState:
    """Complete, profile-private state required for deterministic resumption."""

    schema_id: str
    metadata: Mapping[str, Any]
    regions: Mapping[str, bytes]
    event_cursor: int
    _is_normalized: bool = field(default=False, repr=False, compare=False)

    def normalized(self) -> "ContinuationState":
        if self._is_normalized:
            return self
        if not self.schema_id:
            raise ValueError("continuation schema_id must not be empty")
        cursor = int(self.event_cursor)
        if cursor < 0:
            raise ValueError("event cursor must be non-negative")
        regions: dict[str, bytes] = {}
        for name, data in self.regions.items():
            name = str(name)
            if not name or name in regions:
                raise ValueError(f"invalid continuation region name: {name!r}")
            regions[name] = bytes(data)
        return ContinuationState(
            self.schema_id, _json_value(self.metadata, "continuation metadata"),
            regions, cursor, True)

    @property
    def digest(self) -> str:
        state = self.normalized()
        h = hashlib.sha256(_canonical_json({
            "schema_id": state.schema_id,
            "metadata": state.metadata,
            "event_cursor": state.event_cursor,
        }))
        _hash_regions(h, state.regions)
        return h.hexdigest()


@dataclass(frozen=True)
class CanonicalState:
    """Representation-independent state used for oracle equivalence."""

    schema_id: str
    event_cursor: int
    fields: Mapping[str, Any] = field(default_factory=dict)
    regions: Mapping[str, bytes] = field(default_factory=dict)
    _is_normalized: bool = field(default=False, repr=False, compare=False)

    def normalized(self) -> "CanonicalState":
        if self._is_normalized:
            return self
        if not self.schema_id:
            raise ValueError("canonical schema_id must not be empty")
        cursor = int(self.event_cursor)
        if cursor < 0:
            raise ValueError("canonical event cursor must be non-negative")
        regions: dict[str, bytes] = {}
        for name, data in self.regions.items():
            name = str(name)
            if not name or name in regions:
                raise ValueError(f"invalid canonical region name: {name!r}")
            regions[name] = bytes(data)
        return CanonicalState(
            self.schema_id, cursor,
            _json_value(self.fields, "canonical fields"), regions, True)

    @property
    def digest(self) -> str:
        state = self.normalized()
        return canonical_state_digest(
            state.schema_id, state.event_cursor, state.fields, state.regions)

    def compare(self, other: "CanonicalState") -> "StateComparison":
        left, right = self.normalized(), other.normalized()
        differences: list[str] = []
        if left.schema_id != right.schema_id:
            differences.append(
                f"schema: oracle {left.schema_id!r} != candidate {right.schema_id!r}")
        if left.event_cursor != right.event_cursor:
            differences.append(
                f"event_cursor: oracle {left.event_cursor} != candidate {right.event_cursor}")
        if left.fields != right.fields:
            differences.extend(_diff_json(left.fields, right.fields, "fields"))
        names = sorted(set(left.regions) | set(right.regions))
        for name in names:
            if name not in left.regions:
                differences.append(f"region {name!r}: missing from oracle")
            elif name not in right.regions:
                differences.append(f"region {name!r}: missing from candidate")
            elif left.regions[name] != right.regions[name]:
                a, b = left.regions[name], right.regions[name]
                first = next((i for i, pair in enumerate(zip(a, b)) if pair[0] != pair[1]),
                             min(len(a), len(b)))
                differences.append(
                    f"region {name!r}: first mismatch at {first:#x}; "
                    f"sizes {len(a)} != {len(b)}" if len(a) != len(b) else
                    f"region {name!r}: first mismatch at {first:#x}")
        return StateComparison(not differences, tuple(differences), left.digest, right.digest)


def machine_projection(state: ContinuationState, *, schema_id: str) -> CanonicalState:
    """Project a complete VM/override continuation state without losing bytes.

    Interpreted, VMless, CPUless, and DOS-memory-backed profiles use this when
    their authoritative representations are intentionally identical.  Detached
    native profiles instead implement ``ReplayDriver.project`` by constructing
    the same canonical semantic schema from native fields/regions.
    """
    state = state.normalized()
    return CanonicalState(
        schema_id=schema_id,
        event_cursor=state.event_cursor,
        fields={
            "continuation_schema": state.schema_id,
            "metadata": state.metadata,
        },
        regions=state.regions,
        _is_normalized=True,
    )


def canonical_state_digest(
    schema_id: str,
    event_cursor: int,
    fields: Mapping[str, Any],
    regions: Mapping[str, bytes | bytearray | memoryview],
) -> str:
    """Stream the canonical projection digest without materializing regions.

    Machine adapters use this for point fingerprints over live bytearrays.  It
    is exactly the digest produced by :class:`CanonicalState`, not a weaker
    summary; tests require the fast and materialized paths to agree.
    """
    h = hashlib.sha256(_canonical_json({
        "schema_id": str(schema_id),
        "event_cursor": int(event_cursor),
        "fields": fields,
    }))
    _hash_regions(h, regions)
    return h.hexdigest()


@dataclass(frozen=True)
class StateComparison:
    equivalent: bool
    differences: tuple[str, ...]
    oracle_digest: str
    candidate_digest: str


@dataclass(frozen=True)
class ReplayExecutionIdentity:
    """Stable identity of one oracle or candidate execution configuration."""

    profile_id: str
    role: str
    implementation: str
    image: str
    runtime: str
    devices: str
    continuation_schema: str
    projection_schema: str

    def __post_init__(self) -> None:
        if self.role not in ("oracle", "candidate"):
            raise ValueError("replay execution role must be 'oracle' or 'candidate'")
        for name in ("profile_id", "implementation", "image", "runtime", "devices",
                     "continuation_schema", "projection_schema"):
            if not getattr(self, name):
                raise ValueError(f"replay execution identity {name} must not be empty")

    @property
    def identity_digest(self) -> str:
        return _sha256(_canonical_json(self.to_json()))

    @property
    def storage_key(self) -> str:
        return f"{_safe_name(self.profile_id)}-{self.identity_digest[:16]}"

    def same_execution_as(self, other: "ReplayExecutionIdentity") -> bool:
        """Return whether two profiles select the same deterministic runtime.

        Profile names and roles describe how an execution is being used.  They
        do not change the selected implementation, image, runtime, devices, or
        state schemas that determine replay compatibility.
        """
        return (
            self.implementation,
            self.image,
            self.runtime,
            self.devices,
            self.continuation_schema,
            self.projection_schema,
        ) == (
            other.implementation,
            other.image,
            other.runtime,
            other.devices,
            other.continuation_schema,
            other.projection_schema,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id, "role": self.role,
            "implementation": self.implementation, "image": self.image,
            "runtime": self.runtime, "devices": self.devices,
            "continuation_schema": self.continuation_schema,
            "projection_schema": self.projection_schema,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> "ReplayExecutionIdentity":
        expected = {
            "profile_id", "role", "implementation", "image", "runtime",
            "devices", "continuation_schema", "projection_schema",
        }
        if set(raw) != expected:
            raise ValueError(
                "replay execution identity fields do not match the current "
                f"schema: expected {sorted(expected)}, got {sorted(raw)}"
            )
        return cls(
            str(raw["profile_id"]), str(raw["role"]), str(raw["implementation"]),
            str(raw["image"]), str(raw["runtime"]), str(raw["devices"]),
            str(raw["continuation_schema"]), str(raw["projection_schema"]),
        )


@dataclass
class FunctionVisit:
    function_id: str
    invocation_count: int = 0
    first_entry: ReplayPoint | None = None
    last_exit: ReplayPoint | None = None
    incomplete: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "function_id": self.function_id,
            "invocation_count": self.invocation_count,
            "first_entry": None if self.first_entry is None else self.first_entry.to_json(),
            "last_exit": None if self.last_exit is None else self.last_exit.to_json(),
            "incomplete": self.incomplete,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> "FunctionVisit":
        first, last = raw.get("first_entry"), raw.get("last_exit")
        return cls(
            str(raw["function_id"]), int(raw["invocation_count"]),
            None if first is None else ReplayPoint.from_json(first),
            None if last is None else ReplayPoint.from_json(last),
            bool(raw.get("incomplete", False)),
        )


class FunctionVisitIndex:
    """Streaming nested/recursive call recorder for the artifact atlas index."""

    def __init__(self) -> None:
        self._visits: dict[str, FunctionVisit] = {}
        self._depth: dict[str, int] = {}

    def enter(self, function_id: str, point_before: ReplayPoint) -> None:
        function_id = str(function_id)
        if not function_id:
            raise ValueError("function identity must not be empty")
        visit = self._visits.setdefault(function_id, FunctionVisit(function_id))
        depth = self._depth.get(function_id, 0)
        if visit.first_entry is None:
            visit.first_entry = point_before
        else:
            _same_timeline(visit.first_entry, point_before)
        visit.invocation_count += 1
        self._depth[function_id] = depth + 1
        visit.incomplete = True

    def exit(self, function_id: str, point_after: ReplayPoint) -> None:
        function_id = str(function_id)
        depth = self._depth.get(function_id, 0)
        if depth <= 0:
            raise ValueError(f"function exit without entry: {function_id!r}")
        visit = self._visits[function_id]
        assert visit.first_entry is not None
        _same_timeline(visit.first_entry, point_after)
        if point_after.ordinal < visit.first_entry.ordinal:
            raise ValueError("function exit precedes first entry")
        depth -= 1
        self._depth[function_id] = depth
        if depth == 0:
            visit.last_exit = point_after
            visit.incomplete = False

    def records(self) -> tuple[FunctionVisit, ...]:
        return tuple(self._visits[key] for key in sorted(self._visits))

    def to_json(self) -> list[dict[str, Any]]:
        return [record.to_json() for record in self.records()]


@dataclass(frozen=True)
class ObservedTransfer:
    """Aggregated, directly observed control transfer on one replay timeline."""

    source_id: str
    target_id: str
    kind: str
    count: int
    first_observed: ReplayPoint
    last_observed: ReplayPoint

    def __post_init__(self) -> None:
        if not self.source_id or not self.target_id or not self.kind:
            raise ValueError("observed transfer identities and kind must not be empty")
        if int(self.count) <= 0:
            raise ValueError("observed transfer count must be positive")
        _same_timeline(self.first_observed, self.last_observed)
        if self.last_observed.ordinal < self.first_observed.ordinal:
            raise ValueError("observed transfer range is reversed")
        object.__setattr__(self, "count", int(self.count))

    def to_json(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "kind": self.kind,
            "count": self.count,
            "first_observed": self.first_observed.to_json(),
            "last_observed": self.last_observed.to_json(),
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> "ObservedTransfer":
        return cls(
            str(raw["source_id"]), str(raw["target_id"]), str(raw["kind"]),
            int(raw["count"]), ReplayPoint.from_json(raw["first_observed"]),
            ReplayPoint.from_json(raw["last_observed"]),
        )


@dataclass(frozen=True)
class ReplayExecutionEvidence:
    """Versioned oracle evidence produced from actual runtime observations.

    This section remains compact and immutable with respect to the input
    stream.  The Atlas imports it; it does not infer transfers from adjacent
    function visits.
    """

    profile_identity_digest: str
    transfers: tuple[ObservedTransfer, ...] = ()
    runtime_variants: tuple[str, ...] = ()
    incomplete_functions: tuple[str, ...] = ()
    provenance: Mapping[str, Any] = field(default_factory=dict)
    version: int = 1

    def __post_init__(self) -> None:
        if self.version != 1:
            raise ValueError("unsupported replay execution-evidence version")
        if not self.profile_identity_digest:
            raise ValueError("execution evidence requires a profile identity")
        object.__setattr__(
            self, "transfers",
            tuple(sorted(self.transfers, key=lambda item: (
                item.source_id, item.target_id, item.kind,
                item.first_observed.ordinal, item.last_observed.ordinal,
            ))),
        )
        object.__setattr__(self, "runtime_variants", tuple(sorted(set(self.runtime_variants))))
        object.__setattr__(
            self, "incomplete_functions", tuple(sorted(set(self.incomplete_functions))))
        object.__setattr__(
            self, "provenance",
            _json_value(self.provenance, "execution evidence provenance"),
        )

    @property
    def evidence_identity_digest(self) -> str:
        """Identity of one reproducible observation recipe.

        Observed counts are deliberately excluded. Re-running the same plan
        and observer over the same artifact must reproduce the same content or
        be rejected as non-deterministic evidence.
        """
        return _sha256(_canonical_json({
            "version": self.version,
            "profile_identity_digest": self.profile_identity_digest,
            "provenance": self.provenance,
        }))

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "profile_identity_digest": self.profile_identity_digest,
            "transfers": [item.to_json() for item in self.transfers],
            "runtime_variants": list(self.runtime_variants),
            "incomplete_functions": list(self.incomplete_functions),
            "provenance": self.provenance,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> "ReplayExecutionEvidence":
        return cls(
            str(raw["profile_identity_digest"]),
            tuple(ObservedTransfer.from_json(item) for item in raw.get("transfers", ())),
            tuple(map(str, raw.get("runtime_variants", ()))),
            tuple(map(str, raw.get("incomplete_functions", ()))),
            raw.get("provenance", {}),
            int(raw.get("version", 0)),
        )


class ReplayEvidenceRecorder:
    """Streaming collector fed by a backend's real control-transfer observer."""

    def __init__(self) -> None:
        self.visits = FunctionVisitIndex()
        self._transfers: dict[tuple[str, str, str], ObservedTransfer] = {}
        self._runtime_variants: set[str] = set()

    def enter(self, function_id: str, point_before: ReplayPoint) -> None:
        self.visits.enter(function_id, point_before)

    def exit(self, function_id: str, point_after: ReplayPoint) -> None:
        self.visits.exit(function_id, point_after)

    def observe_transfer(
        self, source_id: str, target_id: str, kind: str, point: ReplayPoint,
    ) -> None:
        key = (str(source_id), str(target_id), str(kind))
        current = self._transfers.get(key)
        if current is None:
            self._transfers[key] = ObservedTransfer(*key, 1, point, point)
            return
        _same_timeline(current.first_observed, point)
        self._transfers[key] = ObservedTransfer(
            *key, current.count + 1, current.first_observed, point)

    def observe_runtime_variant(self, variant_id: str) -> None:
        if not variant_id:
            raise ValueError("runtime variant identity must not be empty")
        self._runtime_variants.add(str(variant_id))

    def evidence(
        self,
        profile: ReplayExecutionIdentity,
        *,
        provenance: Mapping[str, Any] | None = None,
    ) -> ReplayExecutionEvidence:
        incomplete = tuple(
            record.function_id for record in self.visits.records()
            if record.invocation_count and record.incomplete
        )
        return ReplayExecutionEvidence(
            profile.identity_digest, tuple(self._transfers.values()),
            tuple(self._runtime_variants), incomplete, provenance or {})


class ReplayDriver(Protocol):
    """Adapter implemented by each interpreter, override, or native profile."""

    @property
    def profile(self) -> ReplayExecutionIdentity: ...
    @property
    def current_point(self) -> ReplayPoint: ...
    def capture(self) -> ContinuationState: ...
    def restore(self, state: ContinuationState, point: ReplayPoint) -> None: ...
    def replay_to(self, artifact: "ReplayArtifact", point: ReplayPoint) -> None: ...
    def project(self) -> CanonicalState: ...


@dataclass(frozen=True)
class IntervalRun:
    profile: ReplayExecutionIdentity
    restored_from: ReplayPoint
    start: ReplayPoint
    end: ReplayPoint
    projection: CanonicalState


@dataclass(frozen=True)
class VerificationResult:
    start: ReplayPoint
    end: ReplayPoint
    oracle: IntervalRun
    candidate: IntervalRun
    comparison: StateComparison

    @property
    def equivalent(self) -> bool:
        return self.comparison.equivalent


@dataclass(frozen=True)
class CheckpointVerificationResult:
    """Single-pass semantic-point verification with localized failure data.

    ``checkpoint_span`` controls expensive detailed comparisons and reporting,
    not observation coverage.  Every semantic point contributes its complete
    canonical-state fingerprint to an order-sensitive rolling digest.  In
    observable mode, backend adapters additionally digest effects that escape
    between those points.
    """

    result: VerificationResult
    checkpoint_span: int
    points_observed: int
    checkpoints_compared: int
    observable_effects: bool
    failed_interval: tuple[ReplayPoint, ReplayPoint] | None = None
    observable_event_count: int = 0

    @property
    def equivalent(self) -> bool:
        return self.result.equivalent

    @property
    def comparison(self) -> StateComparison:
        return self.result.comparison


@dataclass(frozen=True)
class ReplayValidation:
    """One persisted oracle/candidate interval comparison."""

    oracle_profile_identity_digest: str
    candidate_profile_identity_digest: str
    start: ReplayPoint
    end: ReplayPoint
    equivalent: bool
    oracle_digest: str
    candidate_digest: str
    version: int = 1

    def __post_init__(self) -> None:
        if self.version != 1:
            raise ValueError("unsupported replay-validation version")
        if (
            not self.oracle_profile_identity_digest
            or not self.candidate_profile_identity_digest
            or not self.oracle_digest
            or not self.candidate_digest
        ):
            raise ValueError("replay validation identities must not be empty")
        _ordered_interval(self.start, self.end)

    @property
    def identity_digest(self) -> str:
        return _sha256(_canonical_json(self.to_json()))

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "oracle_profile_identity_digest": self.oracle_profile_identity_digest,
            "candidate_profile_identity_digest": self.candidate_profile_identity_digest,
            "start": self.start.to_json(),
            "end": self.end.to_json(),
            "equivalent": bool(self.equivalent),
            "oracle_digest": self.oracle_digest,
            "candidate_digest": self.candidate_digest,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> "ReplayValidation":
        return cls(
            str(raw["oracle_profile_identity_digest"]),
            str(raw["candidate_profile_identity_digest"]),
            ReplayPoint.from_json(raw["start"]),
            ReplayPoint.from_json(raw["end"]),
            bool(raw["equivalent"]),
            str(raw["oracle_digest"]),
            str(raw["candidate_digest"]),
            int(raw.get("version", 0)),
        )


class ReplayRecording:
    """In-memory event capture finalized as one immutable ReplayArtifact.

    Frontends own input sampling and stable-boundary detection; this class owns
    the sole persistent representation.  No partial or frontend-specific
    manifest is written while recording.
    """

    def __init__(
        self, directory: str | Path, *, timeline_id: str,
        profile: ReplayExecutionIdentity, base_state: ContinuationState,
        metadata: Mapping[str, Any] | None = None,
    ):
        self.directory = Path(directory)
        self.timeline_id = str(timeline_id)
        self.profile = profile
        self.base_state = base_state.normalized()
        self.metadata = _json_value(metadata or {}, "recording metadata")
        self._events: list[ReplayEvent] = []
        self._coordinates: dict[int, ReplayPointCoordinate] = {}
        self._sequence = 0
        self._finished = False

    @property
    def event_count(self) -> int:
        return len(self._events)

    @property
    def active(self) -> bool:
        return not self._finished

    def add(self, ordinal: int, channel: str, payload: Any) -> ReplayEvent:
        if self._finished:
            raise RuntimeError("replay recording is already complete")
        event = ReplayEvent(
            ReplayPoint(int(ordinal), self.timeline_id),
            self._sequence, channel, payload)
        self._events.append(event)
        self._sequence += 1
        return event

    def mark(
        self, ordinal: int, *, schema_id: str, value: Any,
    ) -> ReplayPointCoordinate:
        """Declare the exact cross-backend stop coordinate for one point."""
        if self._finished:
            raise RuntimeError("replay recording is already complete")
        coordinate = ReplayPointCoordinate(
            ReplayPoint(int(ordinal), self.timeline_id), schema_id, value)
        existing = self._coordinates.get(coordinate.point.ordinal)
        if existing is not None and existing != coordinate:
            raise ValueError(
                f"timeline point {coordinate.point.ordinal} was marked twice")
        self._coordinates[coordinate.point.ordinal] = coordinate
        return coordinate

    def finish(
        self, end_ordinal: int, *, end_state: ContinuationState | None = None,
    ) -> "ReplayArtifact":
        if self._finished:
            raise RuntimeError("replay recording is already complete")
        end = ReplayPoint(int(end_ordinal), self.timeline_id)
        if self.base_state.event_cursor != 0:
            raise ValueError("a new replay recording base must use event cursor 0")
        if any(event.point.ordinal > end.ordinal for event in self._events):
            raise ValueError("recording end precedes an event")
        if end_state is not None and end_state.normalized().event_cursor != len(self._events):
            raise ValueError("recording endpoint cursor does not cover every event")
        if self._coordinates:
            expected = set(range(end.ordinal + 1))
            actual = set(self._coordinates)
            if actual != expected:
                missing = sorted(expected - actual)
                extra = sorted(actual - expected)
                raise ValueError(
                    "recording timeline coordinates must cover every ordinal; "
                    f"missing={missing[:8]} extra={extra[:8]}")
        metadata = dict(self.metadata)
        metadata.update({
            "recording_profile_id": self.profile.profile_id,
            "end_point": end.to_json(),
        })
        artifact = ReplayArtifact.create(
            self.directory, timeline_id=self.timeline_id,
            events=self._events,
            coordinates=self._coordinates.values(),
            metadata=metadata)
        base = ReplayPoint(0, self.timeline_id)
        artifact.register_profile(
            self.profile, base_point=base, base_state=self.base_state)
        if end_state is not None and end != base:
            artifact.cache(
                self.profile, end, end_state,
                metadata={"kind": "recording-end"})
        self._finished = True
        return artifact


class ReplayArtifact:
    """One deterministic replay corpus item and all profile-local caches."""

    def __init__(self, directory: Path, manifest: dict[str, Any]):
        self.directory = Path(directory)
        self.path = self.directory / MANIFEST
        self._manifest = manifest

    @classmethod
    def create(
        cls,
        directory: str | Path,
        *,
        timeline_id: str,
        events: Iterable[ReplayEvent],
        coordinates: Iterable[ReplayPointCoordinate] = (),
        metadata: Mapping[str, Any] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> "ReplayArtifact":
        directory = Path(directory)
        path = directory / MANIFEST
        if path.exists():
            raise FileExistsError(path)
        if not timeline_id or page_size <= 0:
            raise ValueError("timeline_id and positive page_size are required")
        ordered = sorted(events, key=lambda e: (e.point.ordinal, e.sequence))
        seen: set[tuple[int, int]] = set()
        for event in ordered:
            if event.point.timeline_id != timeline_id:
                raise ValueError("event uses a different timeline")
            key = (event.point.ordinal, event.sequence)
            if key in seen:
                raise ValueError(f"duplicate event position/sequence: {key}")
            seen.add(key)
        ordered_coordinates = sorted(
            coordinates, key=lambda coordinate: coordinate.point.ordinal)
        coordinate_ordinals: set[int] = set()
        coordinate_schemas = {item.schema_id for item in ordered_coordinates}
        if len(coordinate_schemas) > 1:
            raise ValueError("one replay timeline cannot mix coordinate schemas")
        for coordinate in ordered_coordinates:
            if coordinate.point.timeline_id != timeline_id:
                raise ValueError("timeline coordinate uses a different timeline")
            if coordinate.point.ordinal in coordinate_ordinals:
                raise ValueError(
                    f"duplicate timeline coordinate: {coordinate.point.ordinal}")
            coordinate_ordinals.add(coordinate.point.ordinal)
        encoded_coordinates = [item.to_json() for item in ordered_coordinates]
        manifest = {
            "format_version": FORMAT_VERSION,
            "revision": 0,
            "timeline_id": timeline_id,
            "page_size": int(page_size),
            "events": [event.to_json() for event in ordered],
            "event_stream_sha256": _sha256(_canonical_json([e.to_json() for e in ordered])),
            "timeline_coordinates": encoded_coordinates,
            "timeline_coordinates_sha256": _sha256(
                _canonical_json(encoded_coordinates)),
            "timeline_coordinate_provenance": None,
            "metadata": _json_value(metadata or {}, "artifact metadata"),
            "profiles": {},
            "function_visits": [],
            "execution_evidence": None,
            "validations": [],
            "points": {},
        }
        artifact = cls(directory, manifest)
        with artifact._locked_mutation(reload=False):
            if path.exists():
                raise FileExistsError(path)
            artifact._write()
        return artifact

    @classmethod
    def open(cls, directory: str | Path) -> "ReplayArtifact":
        directory = Path(directory)
        manifest = _read_json(directory / MANIFEST)
        if int(manifest.get("format_version", 0)) != FORMAT_VERSION:
            raise ReplayError("unsupported replay artifact version")
        artifact = cls(directory, manifest)
        with artifact._locked_mutation(reload=False):
            artifact._recover_incomplete_publications()
        expected = _sha256(_canonical_json([event.to_json() for event in artifact.events]))
        if expected != manifest.get("event_stream_sha256"):
            raise ReplayError("event stream hash mismatch")
        encoded_coordinates = [
            coordinate.to_json()
            for coordinate in artifact.timeline_coordinates
        ]
        coordinate_digest = _sha256(_canonical_json(encoded_coordinates))
        if coordinate_digest != manifest.get(
            "timeline_coordinates_sha256", coordinate_digest
        ):
            raise ReplayError("timeline coordinate hash mismatch")
        return artifact

    @property
    def timeline_id(self) -> str:
        return str(self._manifest["timeline_id"])

    @property
    def page_size(self) -> int:
        return int(self._manifest["page_size"])

    @property
    def events(self) -> tuple[ReplayEvent, ...]:
        return tuple(ReplayEvent.from_json(raw) for raw in self._manifest["events"])

    @property
    def event_stream_sha256(self) -> str:
        return str(self._manifest["event_stream_sha256"])

    @property
    def timeline_coordinates(self) -> tuple[ReplayPointCoordinate, ...]:
        return tuple(
            ReplayPointCoordinate.from_json(raw)
            for raw in self._manifest.get("timeline_coordinates", ())
        )

    @property
    def timeline_coordinates_sha256(self) -> str:
        encoded = [item.to_json() for item in self.timeline_coordinates]
        return str(self._manifest.get(
            "timeline_coordinates_sha256",
            _sha256(_canonical_json(encoded)),
        ))

    def timeline_coordinate(self, point: ReplayPoint) -> ReplayPointCoordinate:
        self._point(point)
        matches = [
            coordinate for coordinate in self.timeline_coordinates
            if coordinate.point == point
        ]
        if len(matches) != 1:
            raise ReplayError(
                f"timeline point {point.ordinal} has no exact stop coordinate")
        return matches[0]

    def set_timeline_coordinates(
        self,
        coordinates: Iterable[ReplayPointCoordinate],
        *,
        provenance: Mapping[str, Any],
    ) -> bool:
        """Materialize a previously unindexed timeline exactly once.

        This is intentionally not a compatibility playback path. A project may
        run an explicit one-shot materializer over a valuable recording, after
        which normal replay requires and consumes the resulting coordinates.
        """
        ordered = tuple(sorted(
            coordinates, key=lambda coordinate: coordinate.point.ordinal))
        if not ordered:
            raise ValueError("timeline coordinate materialization is empty")
        schemas = {item.schema_id for item in ordered}
        if len(schemas) != 1:
            raise ValueError("one replay timeline cannot mix coordinate schemas")
        ordinals: set[int] = set()
        for coordinate in ordered:
            self._point(coordinate.point)
            if coordinate.point.ordinal in ordinals:
                raise ValueError(
                    f"duplicate timeline coordinate: {coordinate.point.ordinal}")
            ordinals.add(coordinate.point.ordinal)
        end_raw = self.metadata.get("end_point")
        if isinstance(end_raw, Mapping):
            end = ReplayPoint.from_json(end_raw)
            expected = set(range(end.ordinal + 1))
            if ordinals != expected:
                missing = sorted(expected - ordinals)
                extra = sorted(ordinals - expected)
                raise ValueError(
                    "materialized timeline coordinates must cover every ordinal; "
                    f"missing={missing[:8]} extra={extra[:8]}")
        encoded = [item.to_json() for item in ordered]
        digest = _sha256(_canonical_json(encoded))
        encoded_provenance = _json_value(
            provenance, "timeline coordinate provenance")
        with self._locked_mutation():
            current = self._manifest.get("timeline_coordinates", [])
            if current:
                if (
                    current == encoded
                    and self._manifest.get("timeline_coordinate_provenance")
                    == encoded_provenance
                ):
                    return False
                raise ReplayError("timeline coordinates are immutable once materialized")
            self._manifest["timeline_coordinates"] = encoded
            self._manifest["timeline_coordinates_sha256"] = digest
            self._manifest["timeline_coordinate_provenance"] = encoded_provenance
            self._write()
        return True

    @property
    def metadata(self) -> dict[str, Any]:
        """Return a detached copy of artifact-level canonical JSON metadata."""
        return _json_value(self._manifest["metadata"], "artifact metadata")

    @property
    def identity_digest(self) -> str:
        """Stable identity of the immutable timeline and its capture base."""
        recording_profile_id = str(self._manifest.get("metadata", {}).get(
            "recording_profile_id", ""))
        profile = self._manifest.get("profiles", {}).get(recording_profile_id)
        record = None if profile is None else {
            "identity_digest": profile.get("identity_digest"),
            "base_state_sha256": profile.get("base_state_sha256"),
            "base_point": profile.get("base_point"),
        }
        return _sha256(_canonical_json({
            "format_version": int(self._manifest["format_version"]),
            "timeline_id": self.timeline_id,
            "event_stream_sha256": self.event_stream_sha256,
            "timeline_coordinates_sha256": self.timeline_coordinates_sha256,
            "recording_profile_id": recording_profile_id,
            "recording_profile": record,
        }))

    def capture_profile(self) -> ReplayExecutionIdentity:
        """Return the execution profile that captured the immutable inputs."""
        profile_id = str(self.metadata.get("recording_profile_id", ""))
        profiles = {
            profile.profile_id: profile for profile, _ in self.profiles()
        }
        try:
            return profiles[profile_id]
        except KeyError as exc:
            raise ReplayError(
                f"capture profile is absent from artifact: {profile_id!r}"
            ) from exc

    def profile_by_digest(
        self, identity_digest: str,
    ) -> ReplayExecutionIdentity:
        matches = [
            profile for profile, _ in self.profiles()
            if profile.identity_digest == str(identity_digest)
        ]
        if len(matches) != 1:
            raise ReplayError(
                "replay profile identity is absent or ambiguous: "
                f"{identity_digest}"
            )
        return matches[0]

    def validations(self) -> tuple[ReplayValidation, ...]:
        return tuple(
            ReplayValidation.from_json(raw)
            for raw in self._manifest.get("validations", ())
        )

    def record_validation(self, result: VerificationResult) -> bool:
        """Persist a comparison once; identical reruns are idempotent."""
        validation = ReplayValidation(
            result.oracle.profile.identity_digest,
            result.candidate.profile.identity_digest,
            result.start,
            result.end,
            result.equivalent,
            result.comparison.oracle_digest,
            result.comparison.candidate_digest,
        )
        with self._locked_mutation():
            self.require_profile(result.oracle.profile)
            self.require_profile(result.candidate.profile)
            self._point(result.start)
            self._point(result.end)
            records = self._manifest.setdefault("validations", [])
            encoded = validation.to_json()
            if encoded in records:
                return False
            records.append(encoded)
            records.sort(key=lambda raw: ReplayValidation.from_json(
                raw).identity_digest)
            self._write()
        return True

    @property
    def trusted(self) -> bool:
        """Whether this finite captured timeline is oracle-backed evidence.

        This says nothing about unobserved inputs to functions visited by the
        replay. Function correctness remains a set of scoped verification
        claims, never a consequence of artifact trust.
        """
        capture = self.capture_profile()
        if capture.role == "oracle":
            return True
        end_raw = self.metadata.get("end_point")
        if not isinstance(end_raw, Mapping):
            return False
        end = ReplayPoint.from_json(end_raw)
        start = ReplayPoint(0, self.timeline_id)
        for validation in self.validations():
            if (
                validation.equivalent
                and validation.start == start
                and validation.end == end
            ):
                oracle = self.profile_by_digest(
                    validation.oracle_profile_identity_digest)
                candidate = self.profile_by_digest(
                    validation.candidate_profile_identity_digest)
                # Capture may use a provisional or subsequently corrected
                # implementation. Trust is an oracle-backed claim about the
                # finite immutable event timeline, not a certification of the
                # runtime that happened to collect those inputs.
                if oracle.role == "oracle" and candidate.role == "candidate":
                    return True
        return False

    def register_profile(
        self, profile: ReplayExecutionIdentity, *, base_point: ReplayPoint,
        base_state: ContinuationState,
    ) -> None:
        with self._locked_mutation():
            self._point(base_point)
            state = base_state.normalized()
            if state.schema_id != profile.continuation_schema:
                raise ValueError("base continuation schema does not match execution profile")
            existing = self._manifest["profiles"].get(profile.profile_id)
            if existing is not None:
                stored = ReplayExecutionIdentity.from_json(existing["identity"])
                if stored != profile:
                    raise StaleReplayError(
                        f"profile {profile.profile_id!r} is already registered with another identity")
                raise ValueError(f"profile already registered: {profile.profile_id!r}")
            root = Path("profiles") / profile.storage_key
            base_manifest = self._write_full_state(root / "base", base_point, state, profile)
            self._manifest["profiles"][profile.profile_id] = {
                "identity": profile.to_json(),
                "identity_digest": profile.identity_digest,
                "base_state_sha256": state.digest,
                "base_point": base_point.to_json(),
                "base": base_manifest.as_posix(),
                "boundaries": {},
                "pending_boundaries": {},
            }
            self._write()

    def require_profile(self, profile: ReplayExecutionIdentity) -> dict[str, Any]:
        record = self._manifest["profiles"].get(profile.profile_id)
        if record is None:
            raise StaleReplayError(f"unregistered execution profile: {profile.profile_id!r}")
        stored = ReplayExecutionIdentity.from_json(record["identity"])
        if stored != profile or record.get("identity_digest") != profile.identity_digest:
            raise StaleReplayError(f"execution profile identity changed: {profile.profile_id!r}")
        return record

    def cached_points(self, profile: ReplayExecutionIdentity) -> tuple[ReplayPoint, ...]:
        record = self.require_profile(profile)
        points = [ReplayPoint.from_json(record["base_point"])]
        points.extend(ReplayPoint.from_json(item["point"])
                      for item in record["boundaries"].values())
        return tuple(sorted(points, key=lambda point: point.ordinal))

    def profiles(self) -> tuple[tuple[ReplayExecutionIdentity, int], ...]:
        """Return registered profile identities and persistent boundary counts."""
        return tuple(
            (ReplayExecutionIdentity.from_json(record["identity"]),
             len(record["boundaries"]))
            for _, record in sorted(self._manifest["profiles"].items())
        )

    def nearest_cached(
        self, profile: ReplayExecutionIdentity, point: ReplayPoint,
    ) -> ReplayPoint:
        self._point(point)
        eligible = [item for item in self.cached_points(profile) if item.ordinal <= point.ordinal]
        if not eligible:
            raise ReplayError(f"profile {profile.profile_id!r} has no state before point {point.ordinal}")
        return max(eligible, key=lambda item: item.ordinal)

    def has_cached(self, profile: ReplayExecutionIdentity, point: ReplayPoint) -> bool:
        return point in self.cached_points(profile)

    def restore(
        self, profile: ReplayExecutionIdentity, point: ReplayPoint,
    ) -> ContinuationState:
        self._point(point)
        record = self.require_profile(profile)
        base_point = ReplayPoint.from_json(record["base_point"])
        base = self._read_full_state(record["base"], base_point, profile)
        if base.digest != record.get("base_state_sha256"):
            raise StaleReplayError("profile base snapshot identity changed")
        if point == base_point:
            return base
        boundary = record["boundaries"].get(point.key)
        if boundary is None:
            raise KeyError(f"uncached point {point.key} for profile {profile.profile_id!r}")
        manifest = _read_json(self._resolve(boundary["manifest"]))
        if manifest.get("profile_identity_digest") != profile.identity_digest:
            raise StaleReplayError("cached boundary belongs to another execution profile")
        if manifest.get("base_state_sha256") != base.digest:
            raise StaleReplayError("cached boundary belongs to another base snapshot")
        if ReplayPoint.from_json(manifest["point"]) != point:
            raise ReplayError("cached boundary point mismatch")
        layout = manifest.get("regions")
        if layout is None:
            # Boundaries written before variable-region support always had the
            # exact base layout.
            regions = {
                name: bytearray(data) for name, data in base.regions.items()
            }
        else:
            regions = {}
            for item in layout:
                name, size = str(item["name"]), int(item["size"])
                if not name or name in regions or size < 0:
                    raise ReplayError("invalid cached boundary region layout")
                original = base.regions.get(name, b"")
                data = bytearray(original[:size])
                if len(data) < size:
                    data.extend(b"\x00" * (size - len(data)))
                regions[name] = data
        for page in manifest["changed_pages"]:
            name, index = str(page["region"]), int(page["index"])
            if name not in regions or index < 0:
                raise ReplayError("invalid changed-page address")
            start = index * self.page_size
            size = min(self.page_size, len(regions[name]) - start)
            if size <= 0 or int(page["size"]) != size:
                raise ReplayError("changed page lies outside its base region")
            payload = _read_zlib(self._resolve(page["file"]))
            if len(payload) != size or _sha256(payload) != page["sha256"]:
                raise ReplayError("changed-page content mismatch")
            regions[name][start:start + size] = payload
        state = ContinuationState(
            str(manifest["schema_id"]), manifest["metadata"],
            {name: bytes(data) for name, data in regions.items()},
            int(manifest["event_cursor"]),
        ).normalized()
        if state.digest != manifest.get("state_sha256"):
            raise ReplayError("reconstructed continuation-state hash mismatch")
        return state

    def cache(
        self, profile: ReplayExecutionIdentity, point: ReplayPoint,
        state: ContinuationState,
        *, metadata: Mapping[str, Any] | None = None,
    ) -> bool:
        with self._locked_mutation():
            self._point(point)
            record = self.require_profile(profile)
            if self.has_cached(profile, point):
                return False
            state = state.normalized()
            if state.schema_id != profile.continuation_schema:
                raise ValueError("continuation schema does not match execution profile")
            base_point = ReplayPoint.from_json(record["base_point"])
            base = self._read_full_state(record["base"], base_point, profile)
            if base.digest != record.get("base_state_sha256"):
                raise StaleReplayError("profile base snapshot identity changed")
            root = Path("profiles") / profile.storage_key / "boundaries" / point.key
            final = self.directory / root
            final.parent.mkdir(parents=True, exist_ok=True)
            temp = final.parent / f".{point.key}-{uuid.uuid4().hex}.tmp"
            temp.mkdir()
            pending_committed = False
            pages: list[dict[str, Any]] = []
            try:
                for region_no, name in enumerate(sorted(state.regions)):
                    current = state.regions[name]
                    original = base.regions.get(name, b"")
                    for index, start in enumerate(range(0, len(current), self.page_size)):
                        payload = current[start:start + self.page_size]
                        original_page = original[start:start + len(payload)]
                        if (
                            len(original_page) == len(payload)
                            and payload == original_page
                        ):
                            continue
                        rel = Path("pages") / f"{region_no:04d}" / f"{index:08x}.zlib"
                        path = temp / rel
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(zlib.compress(payload, 6))
                        pages.append({
                            "region": name, "index": index, "size": len(payload),
                            "sha256": _sha256(payload), "file": (root / rel).as_posix(),
                        })
                _write_json(temp / "state.json", {
                    "format_version": FORMAT_VERSION, "point": point.to_json(),
                    "profile_identity_digest": profile.identity_digest,
                    "base_state_sha256": base.digest,
                    "schema_id": state.schema_id, "metadata": state.metadata,
                    "event_cursor": state.event_cursor, "state_sha256": state.digest,
                    "boundary_metadata": _json_value(metadata or {}, "boundary metadata"),
                    "regions": [
                        {"name": name, "size": len(state.regions[name])}
                        for name in sorted(state.regions)
                    ],
                    "changed_pages": pages,
                })
                self._publication_stage("prepared")
                pending = record.setdefault("pending_boundaries", {})
                pending[point.key] = {
                    "point": point.to_json(),
                    "manifest": (root / "state.json").as_posix(),
                    "temp": temp.relative_to(self.directory).as_posix(),
                }
                self._write()
                pending_committed = True
                self._publication_stage("pending-indexed")
                if final.exists():
                    raise ReplayError(f"boundary directory already exists: {final}")
                os.replace(temp, final)
                temp = None
                self._publication_stage("directory-published")
                record["boundaries"][point.key] = {
                    "point": point.to_json(), "manifest": (root / "state.json").as_posix(),
                }
                pending.pop(point.key, None)
                self._write()
                self._publication_stage("manifest-indexed")
            finally:
                if not pending_committed and temp is not None and temp.exists():
                    shutil.rmtree(temp)
            return True

    def annotate(self, point: ReplayPoint, *, kind: str, metadata: Mapping[str, Any]) -> None:
        with self._locked_mutation():
            self._point(point)
            if not kind:
                raise ValueError("point annotation kind must not be empty")
            entries = self._manifest["points"].setdefault(point.key, {
                "point": point.to_json(), "annotations": [],
            })
            entries["annotations"].append({
                "kind": kind, "metadata": _json_value(metadata, "point annotation"),
            })
            self._write()

    def point_annotations(self) -> tuple[dict[str, Any], ...]:
        """Return detached annotations ordered by point and insertion order."""
        result: list[dict[str, Any]] = []
        for record in sorted(
            self._manifest.get("points", {}).values(),
            key=lambda item: ReplayPoint.from_json(item["point"]).ordinal,
        ):
            point = ReplayPoint.from_json(record["point"])
            for annotation in record.get("annotations", ()):
                result.append({
                    "point": point.to_json(),
                    "kind": str(annotation["kind"]),
                    "metadata": _json_value(
                        annotation.get("metadata", {}), "point annotation"),
                })
        return tuple(result)

    def set_function_visits(self, index: FunctionVisitIndex) -> bool:
        with self._locked_mutation():
            for visit in index.records():
                if visit.first_entry is not None:
                    self._point(visit.first_entry)
                if visit.last_exit is not None:
                    self._point(visit.last_exit)
            encoded = index.to_json()
            if self._manifest["function_visits"] == encoded:
                return False
            self._manifest["function_visits"] = encoded
            self._write()
        return True

    def set_execution_evidence(
        self,
        profile: ReplayExecutionIdentity,
        evidence: ReplayExecutionEvidence,
        *,
        visits: FunctionVisitIndex | None = None,
    ) -> bool:
        """Persist post-hoc oracle observations with idempotence guarantees.

        The oracle that enriches a replay need not be the profile that captured
        its input stream. A candidate-captured artifact becomes Atlas-eligible
        only after full validation, while its function/edge evidence remains
        owned by this explicitly identified oracle observation run.
        """
        with self._locked_mutation():
            record = self.require_profile(profile)
            if profile.role != "oracle":
                raise ValueError("execution evidence must be recorded by an oracle profile")
            if evidence.profile_identity_digest != profile.identity_digest:
                raise StaleReplayError("execution evidence profile identity changed")
            for transfer in evidence.transfers:
                self._point(transfer.first_observed)
                self._point(transfer.last_observed)
            if record.get("identity_digest") != evidence.profile_identity_digest:
                raise StaleReplayError("registered oracle identity changed")
            encoded_evidence = evidence.to_json()
            encoded_visits = (
                self._manifest["function_visits"]
                if visits is None else visits.to_json()
            )
            for visit in encoded_visits:
                first, last = visit.get("first_entry"), visit.get("last_exit")
                if first is not None:
                    self._point(ReplayPoint.from_json(first))
                if last is not None:
                    self._point(ReplayPoint.from_json(last))
            existing_raw = self._manifest.get("execution_evidence")
            if existing_raw is not None:
                existing = ReplayExecutionEvidence.from_json(existing_raw)
                if (
                    existing.evidence_identity_digest
                    == evidence.evidence_identity_digest
                    and (
                        existing_raw != encoded_evidence
                        or self._manifest["function_visits"] != encoded_visits
                    )
                ):
                    raise ReplayError(
                        "the same execution plan and evidence provenance "
                        "produced different observations"
                    )
            if (
                existing_raw == encoded_evidence
                and self._manifest["function_visits"] == encoded_visits
            ):
                return False
            self._manifest["function_visits"] = encoded_visits
            self._manifest["execution_evidence"] = encoded_evidence
            self._write()
        return True

    def execution_evidence(self) -> ReplayExecutionEvidence | None:
        raw = self._manifest.get("execution_evidence")
        return None if raw is None else ReplayExecutionEvidence.from_json(raw)

    def function_visits(self) -> tuple[FunctionVisit, ...]:
        return tuple(FunctionVisit.from_json(raw)
                     for raw in self._manifest["function_visits"])

    def function_interval(self, function_id: str) -> tuple[ReplayPoint, ReplayPoint]:
        """Return the exact first-entry/final-completed-exit verification interval."""
        matches = [visit for visit in self.function_visits()
                   if visit.function_id == function_id]
        if not matches:
            raise KeyError(f"function was not visited by this replay: {function_id!r}")
        visit = matches[0]
        if visit.first_entry is None or visit.last_exit is None or visit.incomplete:
            raise ReplayError(f"function has no completed replay interval: {function_id!r}")
        return visit.first_entry, visit.last_exit

    def _write_full_state(
        self, root: Path, point: ReplayPoint, state: ContinuationState,
        profile: ReplayExecutionIdentity,
    ) -> Path:
        directory = self.directory / root
        directory.mkdir(parents=True, exist_ok=False)
        regions = []
        for index, name in enumerate(sorted(state.regions)):
            payload = state.regions[name]
            rel = Path("regions") / f"{index:04d}.zlib"
            path = directory / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(zlib.compress(payload, 6))
            regions.append({
                "name": name, "size": len(payload), "sha256": _sha256(payload),
                "file": (root / rel).as_posix(),
            })
        manifest = root / "state.json"
        _write_json(self.directory / manifest, {
            "format_version": FORMAT_VERSION, "point": point.to_json(),
            "profile_identity_digest": profile.identity_digest,
            "schema_id": state.schema_id, "metadata": state.metadata,
            "event_cursor": state.event_cursor, "state_sha256": state.digest,
            "regions": regions,
        })
        return manifest

    def _read_full_state(
        self, relative: str | Path, point: ReplayPoint,
        profile: ReplayExecutionIdentity,
    ) -> ContinuationState:
        manifest = _read_json(self._resolve(relative))
        if manifest.get("profile_identity_digest") != profile.identity_digest:
            raise StaleReplayError("profile base identity mismatch")
        if ReplayPoint.from_json(manifest["point"]) != point:
            raise ReplayError("profile base point mismatch")
        regions: dict[str, bytes] = {}
        for region in manifest["regions"]:
            name = str(region["name"])
            payload = _read_zlib(self._resolve(region["file"]))
            if name in regions or len(payload) != int(region["size"]) or \
                    _sha256(payload) != region["sha256"]:
                raise ReplayError(f"profile base region mismatch: {name!r}")
            regions[name] = payload
        state = ContinuationState(
            str(manifest["schema_id"]), manifest["metadata"], regions,
            int(manifest["event_cursor"]),
        ).normalized()
        if state.digest != manifest.get("state_sha256"):
            raise ReplayError("profile base state hash mismatch")
        return state

    def _point(self, point: ReplayPoint) -> None:
        if point.timeline_id != self.timeline_id:
            raise ValueError("point belongs to another replay timeline")

    def _resolve(self, relative: str | Path) -> Path:
        root = self.directory.resolve()
        path = (self.directory / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ReplayError(f"artifact path escapes its directory: {relative!r}") from exc
        return path

    @property
    def _lock_path(self) -> Path:
        return self.directory / ".replay-writer.lock"

    @contextmanager
    def _locked_mutation(self, *, reload: bool = True):
        """Serialize mutations and reject a live competing writer.

        The lock is intentionally artifact-local and non-waiting: tooling gets
        a precise failure instead of silently interleaving two manifests.  A
        lock left by a dead process on this host is reclaimed automatically.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        owner = {
            "pid": os.getpid(), "host": socket.gethostname(), "token": token,
        }
        payload = (_canonical_json(owner) + b"\n")
        for attempt in range(2):
            try:
                fd = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if attempt == 0 and self._reclaim_stale_lock():
                    continue
                raise ConcurrentReplayWriterError(
                    f"another writer owns replay artifact {self.directory}")
            else:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                break
        try:
            if reload and self.path.exists():
                current = _read_json(self.path)
                if int(current.get("format_version", 0)) != FORMAT_VERSION:
                    raise ReplayError("unsupported replay artifact version")
                self._manifest = current
                self._recover_incomplete_publications()
            yield
        finally:
            try:
                current_owner = _read_json(self._lock_path)
            except ReplayError:
                current_owner = {}
            if current_owner.get("token") == token:
                self._lock_path.unlink(missing_ok=True)

    def _reclaim_stale_lock(self) -> bool:
        try:
            owner = _read_json(self._lock_path)
        except ReplayError:
            return False
        if owner.get("host") != socket.gethostname():
            return False
        try:
            pid = int(owner["pid"])
        except (KeyError, TypeError, ValueError):
            return False
        if _process_is_alive(pid):
            return False
        try:
            self._lock_path.unlink()
        except FileNotFoundError:
            pass
        return True

    def _recover_incomplete_publications(self) -> None:
        """Finish or discard cache publications interrupted at any stage."""
        changed = False
        for record in self._manifest.get("profiles", {}).values():
            pending = record.setdefault("pending_boundaries", {})
            boundaries = record.setdefault("boundaries", {})
            for key, entry in list(pending.items()):
                final_manifest = self._resolve(entry["manifest"])
                final = final_manifest.parent
                temp = self._resolve(entry["temp"])
                if not final_manifest.exists() and (temp / "state.json").exists():
                    final.parent.mkdir(parents=True, exist_ok=True)
                    if final.exists():
                        shutil.rmtree(temp)
                    else:
                        os.replace(temp, final)
                if final_manifest.exists():
                    self._validate_boundary_manifest(record, key, final_manifest)
                    boundaries[key] = {
                        "point": entry["point"], "manifest": entry["manifest"],
                    }
                pending.pop(key, None)
                changed = True

            identity = ReplayExecutionIdentity.from_json(record["identity"])
            boundary_root = (
                self.directory / "profiles" / identity.storage_key / "boundaries")
            if boundary_root.exists():
                for child in boundary_root.iterdir():
                    if child.name.startswith(".") and child.name.endswith(".tmp"):
                        shutil.rmtree(child)
                        changed = True
                        continue
                    if not child.is_dir() or child.name in boundaries:
                        continue
                    # Boundaries are derived caches. A current publication is
                    # always indexed as pending before its final directory is
                    # published, so an unindexed directory has no authority.
                    shutil.rmtree(child)
                    changed = True
        if changed:
            self._write()

    def _validate_boundary_manifest(
        self, record: Mapping[str, Any], key: str, path: Path,
    ) -> None:
        raw = _read_json(path)
        point = ReplayPoint.from_json(raw["point"])
        identity = ReplayExecutionIdentity.from_json(record["identity"])
        if point.key != key:
            raise ReplayError(f"boundary directory key mismatch: {path.parent}")
        if raw.get("profile_identity_digest") != identity.identity_digest:
            raise ReplayError(f"cached boundary profile mismatch: {path.parent}")
        if raw.get("base_state_sha256") != record.get("base_state_sha256"):
            raise ReplayError(f"cached boundary base mismatch: {path.parent}")

    def _publication_stage(self, stage: str) -> None:
        """Test seam called after each durable cache-publication stage."""

    def _write(self) -> None:
        self._manifest["revision"] = int(self._manifest.get("revision", 0)) + 1
        _write_json(self.path, self._manifest)


def run_interval(
    artifact: ReplayArtifact, driver: ReplayDriver,
    start: ReplayPoint, end: ReplayPoint,
) -> IntervalRun:
    """Restore the nearest profile cache and execute exactly ``start`` → ``end``."""
    _ordered_interval(start, end)
    restored, _ = _position_and_project(artifact, driver, start)
    if not artifact.has_cached(driver.profile, start):
        artifact.cache(driver.profile, start, driver.capture(), metadata={"kind": "interval-start"})
    driver.replay_to(artifact, end)
    _driver_at(driver, end, "end")
    projection = _project(driver)
    return IntervalRun(driver.profile, restored, start, end, projection)


def verify_interval(
    artifact: ReplayArtifact, oracle: ReplayDriver, candidate: ReplayDriver,
    start: ReplayPoint, end: ReplayPoint, *, cache_verified_end: bool = True,
) -> VerificationResult:
    """Replay one interval on both sides and compare canonical continuation state."""
    if oracle.profile.role != "oracle" or candidate.profile.role != "candidate":
        raise ValueError("verify_interval requires oracle and candidate profiles")
    if oracle.profile.projection_schema != candidate.profile.projection_schema:
        raise ValueError("oracle and candidate must declare the same projection schema")
    _ordered_interval(start, end)
    oracle_restored, oracle_start = _position_and_project(artifact, oracle, start)
    candidate_restored, candidate_start = _position_and_project(artifact, candidate, start)
    start_comparison = oracle_start.compare(candidate_start)
    if not start_comparison.equivalent:
        artifact.annotate(start, kind="invalid-interval-start", metadata={
            "oracle_profile": oracle.profile.profile_id,
            "candidate_profile": candidate.profile.profile_id,
            "differences": list(start_comparison.differences[:16]),
        })
        raise ReplayError(
            "verification interval starts from non-equivalent oracle/candidate state: "
            + "; ".join(start_comparison.differences[:3]))
    if not artifact.has_cached(oracle.profile, start):
        artifact.cache(oracle.profile, start, oracle.capture(), metadata={"kind": "verified-start"})
    if not artifact.has_cached(candidate.profile, start):
        artifact.cache(candidate.profile, start, candidate.capture(), metadata={"kind": "verified-start"})

    oracle.replay_to(artifact, end)
    candidate.replay_to(artifact, end)
    _driver_at(oracle, end, "oracle end")
    _driver_at(candidate, end, "candidate end")
    oracle_run = IntervalRun(
        oracle.profile, oracle_restored, start, end, _project(oracle))
    candidate_run = IntervalRun(
        candidate.profile, candidate_restored, start, end, _project(candidate))
    comparison = oracle_run.projection.compare(candidate_run.projection)
    result = VerificationResult(start, end, oracle_run, candidate_run, comparison)
    artifact.record_validation(result)
    if comparison.equivalent:
        artifact.annotate(end, kind="verified-endpoint", metadata={
            "oracle_profile": oracle.profile.profile_id,
            "candidate_profile": candidate.profile.profile_id,
            "projection_schema": oracle.profile.projection_schema,
            "digest": comparison.oracle_digest,
        })
        if cache_verified_end:
            if not artifact.has_cached(oracle.profile, end):
                artifact.cache(oracle.profile, end, oracle.capture(), metadata={"kind": "verified-end"})
            if not artifact.has_cached(candidate.profile, end):
                artifact.cache(candidate.profile, end, candidate.capture(), metadata={"kind": "verified-end"})
    else:
        # X is the latest point known equivalent for this interval.  Preserve
        # it, never the already-diverged candidate endpoint.
        artifact.annotate(start, kind="latest-valid-before-divergence", metadata={
            "oracle_profile": oracle.profile.profile_id,
            "candidate_profile": candidate.profile.profile_id,
            "first_observed_mismatch_at": end.to_json(),
            "differences": list(comparison.differences[:16]),
        })
    return result


@dataclass(frozen=True)
class _ObservedSegment:
    start: ReplayPoint
    end: ReplayPoint
    oracle_projection: CanonicalState | None
    candidate_projection: CanonicalState | None
    comparison: StateComparison
    boundary_equivalent: bool
    effects_equivalent: bool
    oracle_boundary_digest: ObservableIntervalDigest
    candidate_boundary_digest: ObservableIntervalDigest
    oracle_effect_digest: ObservableIntervalDigest | None
    candidate_effect_digest: ObservableIntervalDigest | None

    @property
    def equivalent(self) -> bool:
        return (
            self.comparison.equivalent
            and self.boundary_equivalent
            and self.effects_equivalent
        )


def verify_checkpointed(
    artifact: ReplayArtifact,
    oracle: ReplayDriver,
    candidate: ReplayDriver,
    start: ReplayPoint,
    end: ReplayPoint,
    *,
    checkpoint_span: int = 64,
    observable_effects: bool = True,
    cache_verified_end: bool = True,
) -> CheckpointVerificationResult:
    """Verify every semantic point in one pass, comparing detail coarsely.

    The verifier hashes each complete canonical point state, so a divergence at
    a point cannot disappear merely because both sides reconverge before the
    next coarse checkpoint.  With ``observable_effects=True`` each driver must
    also implement ``begin_observable_interval`` and
    ``end_observable_interval``; this catches ordered I/O/device/interrupt/input
    effects that can occur and disappear between semantic points.

    On a failed coarse interval only that interval is restored and replayed one
    point at a time.  The returned ``failed_interval`` is therefore the first
    divergent semantic transition, ready for a backend-specific detailed trace.
    This is not instruction-trace equivalence: internal instruction order and
    guest coordinates remain irrelevant unless an adapter emits a timing/yield
    effect at the semantic boundary.
    """
    if checkpoint_span <= 0:
        raise ValueError("checkpoint_span must be positive")
    if oracle.profile.role != "oracle" or candidate.profile.role != "candidate":
        raise ValueError("verify_checkpointed requires oracle and candidate profiles")
    if oracle.profile.projection_schema != candidate.profile.projection_schema:
        raise ValueError("oracle and candidate must declare the same projection schema")
    _ordered_interval(start, end)
    if observable_effects:
        _require_observable_driver(oracle)
        _require_observable_driver(candidate)

    oracle_restored, oracle_start = _position_and_project(artifact, oracle, start)
    candidate_restored, candidate_start = _position_and_project(
        artifact, candidate, start)
    start_comparison = oracle_start.compare(candidate_start)
    if not start_comparison.equivalent:
        raise ReplayError(
            "verification interval starts from non-equivalent oracle/candidate state: "
            + "; ".join(start_comparison.differences[:3]))
    if not artifact.has_cached(oracle.profile, start):
        artifact.cache(
            oracle.profile, start, oracle.capture(),
            metadata={"kind": "verified-start"})
    if not artifact.has_cached(candidate.profile, start):
        artifact.cache(
            candidate.profile, start, candidate.capture(),
            metadata={"kind": "verified-start"})

    cursor = start
    points_observed = 0
    checkpoints = 0
    observable_event_count = 0
    final_segment: _ObservedSegment | None = None
    failed_interval: tuple[ReplayPoint, ReplayPoint] | None = None
    while cursor.ordinal < end.ordinal:
        checkpoint = ReplayPoint(
            min(end.ordinal, cursor.ordinal + checkpoint_span),
            artifact.timeline_id,
        )
        segment = _observe_segment(
            artifact, oracle, candidate, cursor, checkpoint,
            observable_effects=observable_effects,
        )
        points_observed += checkpoint.ordinal - cursor.ordinal
        checkpoints += 1
        final_segment = segment
        if segment.oracle_effect_digest is not None:
            observable_event_count += segment.oracle_effect_digest.event_count
        if not segment.equivalent:
            failed_interval, segment, refined_points = _refine_failed_segment(
                artifact, oracle, candidate, cursor, checkpoint,
                observable_effects=observable_effects,
            )
            points_observed += refined_points
            final_segment = segment
            break
        cursor = checkpoint

    if final_segment is None:
        # Empty interval: the already-compared start is also the endpoint.
        digest = _empty_interval_digest("dos-re:semantic-point-states:v1")
        final_segment = _ObservedSegment(
            start, end, oracle_start, candidate_start, start_comparison,
            True, True, digest, digest, None, None)

    if failed_interval is None:
        # The rolling point digest already compared the complete canonical
        # endpoint at every coarse checkpoint. Materialize rich projections
        # only once at the requested final endpoint for retained diagnostics.
        oracle_projection = _project(oracle)
        candidate_projection = _project(candidate)
        comparison = oracle_projection.compare(candidate_projection)
    else:
        oracle_projection = final_segment.oracle_projection
        candidate_projection = final_segment.candidate_projection
        assert oracle_projection is not None
        assert candidate_projection is not None
        differences = list(final_segment.comparison.differences)
        if not final_segment.boundary_equivalent:
            differences.insert(0, "semantic-point state digest differs")
        if not final_segment.effects_equivalent:
            differences.insert(0, "observable interval effect digest differs")
        comparison = StateComparison(
            False,
            tuple(differences or ("observed interval differs",)),
            oracle_projection.digest,
            candidate_projection.digest,
        )

    oracle_run = IntervalRun(
        oracle.profile, oracle_restored, start, end, oracle_projection)
    candidate_run = IntervalRun(
        candidate.profile, candidate_restored, start, end,
        candidate_projection)
    result = VerificationResult(
        start, end, oracle_run, candidate_run, comparison)
    artifact.record_validation(result)
    if result.equivalent:
        artifact.annotate(end, kind="verified-semantic-interval", metadata={
            "oracle_profile": oracle.profile.profile_id,
            "candidate_profile": candidate.profile.profile_id,
            "checkpoint_span": checkpoint_span,
            "points_observed": points_observed,
            "observable_effects": observable_effects,
            "digest": comparison.oracle_digest,
        })
        if cache_verified_end:
            if not artifact.has_cached(oracle.profile, end):
                artifact.cache(
                    oracle.profile, end, oracle.capture(),
                    metadata={"kind": "verified-end"})
            if not artifact.has_cached(candidate.profile, end):
                artifact.cache(
                    candidate.profile, end, candidate.capture(),
                    metadata={"kind": "verified-end"})
    else:
        assert failed_interval is not None
        before, after = failed_interval
        artifact.annotate(before, kind="latest-valid-before-divergence", metadata={
            "oracle_profile": oracle.profile.profile_id,
            "candidate_profile": candidate.profile.profile_id,
            "first_observed_mismatch_at": after.to_json(),
            "verification_mode": (
                "semantic+observable" if observable_effects else "semantic"),
            "differences": list(comparison.differences[:16]),
        })
    return CheckpointVerificationResult(
        result, checkpoint_span, points_observed, checkpoints,
        observable_effects, failed_interval, observable_event_count)


def _observe_segment(
    artifact: ReplayArtifact,
    oracle: ReplayDriver,
    candidate: ReplayDriver,
    start: ReplayPoint,
    end: ReplayPoint,
    *,
    observable_effects: bool,
) -> _ObservedSegment:
    _driver_at(oracle, start, "observed segment start")
    _driver_at(candidate, start, "observed segment start")
    (
        oracle_endpoint_digest,
        oracle_boundary_digest,
        oracle_effect_digest,
    ) = _observe_driver_segment(
        artifact, oracle, start, end,
        observable_effects=observable_effects)
    (
        candidate_endpoint_digest,
        candidate_boundary_digest,
        candidate_effect_digest,
    ) = _observe_driver_segment(
        artifact, candidate, start, end,
        observable_effects=observable_effects)
    boundary_equivalent = oracle_boundary_digest == candidate_boundary_digest
    effects_equivalent = (
        not observable_effects
        or oracle_effect_digest == candidate_effect_digest
    )
    oracle_projection = candidate_projection = None
    if not boundary_equivalent or not effects_equivalent:
        oracle_projection = _project(oracle)
        candidate_projection = _project(candidate)
        comparison = oracle_projection.compare(candidate_projection)
    else:
        comparison = StateComparison(
            True, (), oracle_endpoint_digest, candidate_endpoint_digest)
    return _ObservedSegment(
        start, end, oracle_projection, candidate_projection, comparison,
        boundary_equivalent, effects_equivalent,
        oracle_boundary_digest, candidate_boundary_digest,
        oracle_effect_digest, candidate_effect_digest,
    )


def _observe_driver_segment(
    artifact: ReplayArtifact,
    driver: ReplayDriver,
    start: ReplayPoint,
    end: ReplayPoint,
    *,
    observable_effects: bool,
) -> tuple[
    str,
    ObservableIntervalDigest,
    ObservableIntervalDigest | None,
]:
    """Run one side contiguously so tracing does not defeat backend/JIT locality."""
    effects = _begin_observable(driver) if observable_effects else None
    boundaries = RollingEffectDigest("dos-re:semantic-point-states:v1")
    endpoint_digest = _point_digest(driver)
    try:
        for ordinal in range(start.ordinal + 1, end.ordinal + 1):
            point = ReplayPoint(ordinal, artifact.timeline_id)
            driver.replay_to(artifact, point)
            _driver_at(driver, point, "semantic observation")
            digest = _point_digest(driver)
            endpoint_digest = digest
            boundaries.record_bytes(
                SEMANTIC_BOUNDARY, bytes.fromhex(digest), identity=ordinal)
    finally:
        effect_digest = (
            _end_observable(driver, effects)
            if observable_effects else None)
    return (
        endpoint_digest,
        boundaries.finish("dos-re:semantic-point-states:v1"),
        effect_digest,
    )


def _refine_failed_segment(
    artifact: ReplayArtifact,
    oracle: ReplayDriver,
    candidate: ReplayDriver,
    start: ReplayPoint,
    end: ReplayPoint,
    *,
    observable_effects: bool,
) -> tuple[tuple[ReplayPoint, ReplayPoint], _ObservedSegment, int]:
    _position_and_project(artifact, oracle, start)
    _position_and_project(artifact, candidate, start)
    previous = start
    observed = 0
    for ordinal in range(start.ordinal + 1, end.ordinal + 1):
        point = ReplayPoint(ordinal, artifact.timeline_id)
        segment = _observe_segment(
            artifact, oracle, candidate, previous, point,
            observable_effects=observable_effects,
        )
        observed += 1
        if not segment.equivalent:
            return (previous, point), segment, observed
        previous = point
    raise ReplayError(
        "coarse interval digest differed but point refinement was equivalent; "
        "an observable-effect adapter is non-deterministic")


def _require_observable_driver(driver: ReplayDriver) -> None:
    if not callable(getattr(driver, "begin_observable_interval", None)) or not callable(
        getattr(driver, "end_observable_interval", None)
    ):
        raise ValueError(
            f"replay driver {driver.profile.profile_id!r} does not provide "
            "observable interval effects")


def _begin_observable(driver: ReplayDriver):
    return driver.begin_observable_interval()  # type: ignore[attr-defined]


def _end_observable(
    driver: ReplayDriver, token,
) -> ObservableIntervalDigest:
    digest = driver.end_observable_interval(token)  # type: ignore[attr-defined]
    if not isinstance(digest, ObservableIntervalDigest):
        raise ReplayError("observable replay driver returned an invalid digest")
    return digest


def _empty_interval_digest(schema_id: str) -> ObservableIntervalDigest:
    return RollingEffectDigest(schema_id).finish(schema_id)


def bisect_divergence(
    artifact: ReplayArtifact, oracle: ReplayDriver, candidate: ReplayDriver,
    points: Sequence[ReplayPoint],
) -> tuple[ReplayPoint, ReplayPoint, VerificationResult] | None:
    """Find and persist the smallest supplied interval whose endpoint diverges.

    ``points`` must be stable ordered stop points and its first point must be a
    known-equivalent start.  Cached midpoint endpoints make repeated calls
    progressively cheaper; no suffix artifact is created.
    """
    if len(points) < 2:
        raise ValueError("bisection requires at least two points")
    for a, b in zip(points, points[1:]):
        _ordered_interval(a, b)
    whole = verify_interval(artifact, oracle, candidate, points[0], points[-1])
    if whole.equivalent:
        return None
    lo, hi = 0, len(points) - 1
    last = whole
    while hi - lo > 1:
        mid = (lo + hi) // 2
        probe = verify_interval(artifact, oracle, candidate, points[lo], points[mid])
        if probe.equivalent:
            lo = mid
        else:
            hi = mid
            last = probe
    last = verify_interval(artifact, oracle, candidate, points[lo], points[hi])
    if last.equivalent:
        raise ReplayError("bisection invariant failed: final interval no longer diverges")
    artifact.annotate(points[lo], kind="bisected-pre-divergence", metadata={
        "divergent_successor": points[hi].to_json(),
        "oracle_profile": oracle.profile.profile_id,
        "candidate_profile": candidate.profile.profile_id,
    })
    return points[lo], points[hi], last


def _ordered_interval(start: ReplayPoint, end: ReplayPoint) -> None:
    _same_timeline(start, end)
    if end.ordinal < start.ordinal:
        raise ValueError("interval end precedes start")


def _same_timeline(a: ReplayPoint, b: ReplayPoint) -> None:
    if a.timeline_id != b.timeline_id:
        raise ValueError("points belong to different replay timelines")


def _driver_at(driver: ReplayDriver, point: ReplayPoint, operation: str) -> None:
    if driver.current_point != point:
        raise ReplayError(
            f"driver failed exact stop after {operation}: {driver.current_point!r} != {point!r}")


def _position_and_project(
    artifact: ReplayArtifact, driver: ReplayDriver, point: ReplayPoint,
) -> tuple[ReplayPoint, CanonicalState]:
    artifact.require_profile(driver.profile)
    restored = artifact.nearest_cached(driver.profile, point)
    driver.restore(artifact.restore(driver.profile, restored), restored)
    _driver_at(driver, restored, "restore")
    driver.replay_to(artifact, point)
    _driver_at(driver, point, "position")
    return restored, _project(driver)


def _project(driver: ReplayDriver) -> CanonicalState:
    projection = driver.project().normalized()
    if projection.schema_id != driver.profile.projection_schema:
        raise ReplayError("driver projection schema does not match execution profile")
    return projection


def _point_digest(driver: ReplayDriver) -> str:
    fast = getattr(driver, "point_digest", None)
    if callable(fast):
        digest = str(fast())
        if len(digest) != 64:
            raise ReplayError("replay driver returned an invalid point digest")
        return digest
    return _project(driver).digest


def _diff_json(a: Any, b: Any, path: str) -> list[str]:
    if type(a) is not type(b):
        return [f"{path}: oracle type {type(a).__name__} != candidate {type(b).__name__}"]
    if isinstance(a, dict):
        out: list[str] = []
        for key in sorted(set(a) | set(b)):
            if key not in a:
                out.append(f"{path}.{key}: missing from oracle")
            elif key not in b:
                out.append(f"{path}.{key}: missing from candidate")
            else:
                out.extend(_diff_json(a[key], b[key], f"{path}.{key}"))
            if len(out) >= 16:
                break
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            return [f"{path}: lengths {len(a)} != {len(b)}"]
        for index, (left, right) in enumerate(zip(a, b)):
            out = _diff_json(left, right, f"{path}[{index}]")
            if out:
                return out
        return []
    return [] if a == b else [f"{path}: oracle {a!r} != candidate {b!r}"]


def _hash_regions(
    h, regions: Mapping[str, bytes | bytearray | memoryview],
) -> None:
    for name in sorted(regions):
        encoded, payload = name.encode("utf-8"), regions[name]
        h.update(len(encoded).to_bytes(4, "little")); h.update(encoded)
        h.update(len(payload).to_bytes(8, "little")); h.update(payload)


def _safe_name(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in text)
    return cleaned.strip("_") or "profile"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _json_value(value: Any, what: str) -> Any:
    try:
        return json.loads(_canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{what} must be JSON-serializable: {exc}") from exc


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # Windows os.kill(pid, 0) is not the harmless POSIX existence probe:
        # it can signal/terminate the target process. Query the process handle
        # instead so checking a live artifact lock cannot kill its owner.
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, pid)
        if not handle:
            # Access denied means the process exists but is not queryable.
            return ctypes.get_last_error() == 5
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(
                handle, ctypes.byref(exit_code)
            ):
                return True
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReplayError(f"cannot read replay JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReplayError(f"replay JSON must contain an object: {path}")
    return value


def _read_zlib(path: Path) -> bytes:
    try:
        return zlib.decompress(path.read_bytes())
    except (OSError, zlib.error) as exc:
        raise ReplayError(f"cannot read compressed replay state {path}: {exc}") from exc


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload); handle.flush(); os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()
