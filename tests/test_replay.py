from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from dos_re.replay import (
    CanonicalState,
    ConcurrentReplayWriterError,
    ContinuationState,
    ReplayExecutionIdentity,
    FunctionVisitIndex,
    ReplayArtifact,
    ReplayEvidenceRecorder,
    ReplayError,
    ReplayEvent,
    ReplayPoint,
    ReplayPointCoordinate,
    ReplayRecording,
    StaleReplayError,
    bisect_divergence,
    machine_projection,
    verify_checkpointed,
    verify_interval,
)
from dos_re.observable import RollingEffectDigest


TIMELINE = "hook-verification-instruction-boundaries-v1"
VALUES = [3, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43]


def point(n: int) -> ReplayPoint:
    return ReplayPoint(n, TIMELINE)


def profile(profile_id: str, role: str, implementation: str,
            continuation_schema: str, *,
            runtime: str = "runtime-a") -> ReplayExecutionIdentity:
    return ReplayExecutionIdentity(
        profile_id=profile_id,
        role=role,
        implementation=implementation,
        image="image-a",
        runtime=runtime,
        devices="devices-a",
        continuation_schema=continuation_schema,
        projection_schema="game-state-v1",
    )


ORACLE = profile("oracle", "oracle", "interpreter", "machine-v1")
NATIVE = profile("native", "candidate", "detached-native", "native-v1")


def test_timeline_coordinates_are_hashed_immutable_stop_contract(tmp_path):
    recording = ReplayRecording(
        tmp_path / "coordinate-replay",
        timeline_id=TIMELINE,
        profile=NATIVE,
        base_state=CounterDriver(NATIVE).capture(),
        metadata={"purpose": "coordinate-contract"},
    )
    recording.mark(0, schema_id="guest-instructions-v1", value=100)
    recording.add(0, "input", {"value": 3})
    recording.mark(1, schema_id="guest-instructions-v1", value=137)
    artifact = recording.finish(1)

    assert artifact.timeline_coordinate(point(0)).value == 100
    assert artifact.timeline_coordinate(point(1)).value == 137
    assert len(artifact.timeline_coordinates_sha256) == 64
    with pytest.raises(ReplayError, match="immutable"):
        artifact.set_timeline_coordinates(
            [
                ReplayPointCoordinate(point(0), "guest-instructions-v1", 99),
                ReplayPointCoordinate(point(1), "guest-instructions-v1", 137),
            ],
            provenance={"kind": "conflicting-rewrite"},
        )


def test_coordinate_less_artifact_can_be_materialized_once(tmp_path):
    artifact = make_artifact(tmp_path)
    coordinates = [
        ReplayPointCoordinate(point(i), "semantic-tick-v1", i * 2)
        for i in range(len(VALUES) + 1)
    ]

    assert artifact.set_timeline_coordinates(
        coordinates, provenance={"kind": "one-shot-test"})
    assert not artifact.set_timeline_coordinates(
        coordinates, provenance={"kind": "one-shot-test"})
    reopened = ReplayArtifact.open(artifact.directory)
    assert reopened.timeline_coordinate(point(7)).value == 14


def test_replay_execution_identity_rejects_obsolete_or_unknown_fields():
    with pytest.raises(ValueError, match="current schema"):
        ReplayExecutionIdentity.from_json({
            **ORACLE.to_json(),
            "overrides": [],
        })


class CounterDriver:
    """Same logical program with deliberately different continuation layouts."""

    def __init__(
        self, execution_profile: ReplayExecutionIdentity, *,
        bug_at: int | None = None,
    ):
        self._profile = execution_profile
        self._point = point(0)
        self.cursor = 0
        self.total = 0
        self.phase = 5
        self.pending: list[int] = []
        self.bug_at = bug_at
        self.machine_ram = bytearray(32)
        self.native_slots = bytearray(9)
        self.calls: list[tuple[int, int]] = []

    @property
    def profile(self) -> ReplayExecutionIdentity:
        return self._profile

    @property
    def current_point(self) -> ReplayPoint:
        return self._point

    def capture(self) -> ContinuationState:
        if self.profile.continuation_schema == "machine-v1":
            return ContinuationState(
                "machine-v1",
                {"cpu": {"acc": self.total}, "timer": self.phase,
                 "pending_interrupts": list(self.pending)},
                {"ram": bytes(self.machine_ram)},
                self.cursor,
            )
        return ContinuationState(
            "native-v1",
            {"world": {"score": self.total}, "clock_phase": self.phase,
             "scheduled": list(self.pending)},
            {"native-slots": bytes(self.native_slots)},
            self.cursor,
        )

    def restore(self, state: ContinuationState, restored_point: ReplayPoint) -> None:
        if state.schema_id == "machine-v1":
            self.total = state.metadata["cpu"]["acc"]
            self.phase = state.metadata["timer"]
            self.pending = list(state.metadata["pending_interrupts"])
            self.machine_ram[:] = state.regions["ram"]
        else:
            self.total = state.metadata["world"]["score"]
            self.phase = state.metadata["clock_phase"]
            self.pending = list(state.metadata["scheduled"])
            self.native_slots[:] = state.regions["native-slots"]
        self.cursor = state.event_cursor
        self._point = restored_point

    def replay_to(self, artifact: ReplayArtifact, target: ReplayPoint) -> None:
        assert artifact.event_stream_sha256
        self.calls.append((self.current_point.ordinal, target.ordinal))
        while self.current_point.ordinal < target.ordinal:
            ordinal = self.current_point.ordinal
            value = VALUES[self.cursor]
            self.total += value + (1 if self.bug_at == ordinal else 0)
            self.phase = (self.phase + 3) % 17
            if value % 11 == 0:
                self.pending.append(ordinal + 1)
            self.machine_ram[(ordinal * 5) % len(self.machine_ram)] ^= value
            self.native_slots[ordinal % len(self.native_slots)] = value
            self.cursor += 1
            self._point = point(ordinal + 1)

    def project(self) -> CanonicalState:
        # The native representation is unrelated to the VM's byte layout, but
        # both adapters publish the same authoritative semantic state.
        return CanonicalState(
            "game-state-v1",
            self.cursor,
            fields={"total": self.total, "timer_phase": self.phase,
                    "pending_interrupts": list(self.pending),
                    },
        )


class ObservableCounterDriver(CounterDriver):
    def __init__(self, *args, effect_bug_at=None, heal_at=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.effect_bug_at = effect_bug_at
        self.heal_at = heal_at
        self._observer = None

    def begin_observable_interval(self):
        assert self._observer is None
        self._observer = RollingEffectDigest()
        return self._observer

    def end_observable_interval(self, token):
        assert token is self._observer
        self._observer = None
        return token.finish()

    def replay_to(self, artifact: ReplayArtifact, target: ReplayPoint) -> None:
        while self.current_point.ordinal < target.ordinal:
            ordinal = self.current_point.ordinal
            super().replay_to(artifact, point(ordinal + 1))
            if self.heal_at == ordinal:
                self.total -= 1
            if self._observer is not None:
                value = VALUES[ordinal] + (
                    1 if self.effect_bug_at == ordinal else 0)
                self._observer.record(77, ordinal, value)


def make_artifact(tmp_path, *, candidate=NATIVE):
    events = [ReplayEvent(point(i), i, "input", {"value": value})
              for i, value in enumerate(VALUES)]
    artifact = ReplayArtifact.create(
        tmp_path / "replay", timeline_id=TIMELINE, events=events,
        metadata={
            "purpose": "hook-verification",
            "recording_profile_id": candidate.profile_id,
            "end_point": point(len(VALUES)).to_json(),
        },
        page_size=8,
    )
    oracle = CounterDriver(ORACLE)
    native = CounterDriver(candidate)
    artifact.register_profile(ORACLE, base_point=point(0), base_state=oracle.capture())
    artifact.register_profile(candidate, base_point=point(0), base_state=native.capture())
    return artifact


def test_semantic_projection_verifies_native_candidate_and_caches_each_profile(tmp_path):
    artifact = make_artifact(tmp_path)
    oracle, native = CounterDriver(ORACLE), CounterDriver(NATIVE)

    result = verify_interval(artifact, oracle, native, point(4), point(9))

    assert result.equivalent
    assert oracle.calls == [(0, 4), (4, 9)]
    assert native.calls == [(0, 4), (4, 9)]
    assert artifact.cached_points(ORACLE) == (point(0), point(4), point(9))
    assert artifact.cached_points(NATIVE) == (point(0), point(4), point(9))
    assert artifact.restore(ORACLE, point(4)).schema_id == "machine-v1"
    assert artifact.restore(NATIVE, point(4)).schema_id == "native-v1"

    manifest = json.loads((tmp_path / "replay" / "replay.json").read_text())
    assert set(manifest["profiles"]) == {"oracle", "native"}
    assert manifest["points"][point(9).key]["annotations"][0]["kind"] == "verified-endpoint"
    assert len(artifact.validations()) == 1
    assert not artifact.trusted


def test_checkpointed_verifier_catches_state_divergence_that_reconverges(tmp_path):
    candidate = profile(
        "healing-native", "candidate", "detached-native", "native-v1")
    artifact = make_artifact(tmp_path, candidate=candidate)
    oracle = ObservableCounterDriver(ORACLE)
    healing = ObservableCounterDriver(
        candidate, bug_at=2, heal_at=3)

    endpoint = verify_interval(
        artifact, ObservableCounterDriver(ORACLE),
        ObservableCounterDriver(candidate, bug_at=2, heal_at=3),
        point(0), point(8),
    )
    assert endpoint.equivalent  # documents the endpoint-only false negative

    checked = verify_checkpointed(
        artifact, oracle, healing, point(0), point(8),
        checkpoint_span=8, observable_effects=True,
    )
    assert not checked.equivalent
    assert checked.failed_interval == (point(2), point(3))
    assert "semantic-point state digest differs" in checked.comparison.differences


def test_checkpointed_verifier_catches_healed_external_effect(tmp_path):
    candidate = profile(
        "effect-native", "candidate", "detached-native", "native-v1")
    artifact = make_artifact(tmp_path, candidate=candidate)

    checked = verify_checkpointed(
        artifact,
        ObservableCounterDriver(ORACLE),
        ObservableCounterDriver(candidate, effect_bug_at=5),
        point(0), point(10), checkpoint_span=10,
        observable_effects=True,
    )

    assert not checked.equivalent
    assert checked.failed_interval == (point(5), point(6))
    assert "observable interval effect digest differs" in checked.comparison.differences


def test_candidate_capture_becomes_trusted_only_after_full_validation(tmp_path):
    artifact = make_artifact(tmp_path)
    assert artifact.capture_profile() == NATIVE
    assert not artifact.trusted

    result = verify_interval(
        artifact,
        CounterDriver(ORACLE),
        CounterDriver(NATIVE),
        point(0),
        point(len(VALUES)),
    )

    assert result.equivalent
    assert artifact.trusted
    assert len(artifact.validations()) == 1
    revision = json.loads(artifact.path.read_text())["revision"]
    assert not artifact.record_validation(result)
    assert json.loads(artifact.path.read_text())["revision"] == revision


def test_corrected_candidate_can_validate_a_provisional_capture(tmp_path):
    artifact = make_artifact(tmp_path)
    corrected = profile(
        "native-corrected", "candidate", "detached-native-v2", "native-v1",
        runtime="runtime-b",
    )
    artifact.register_profile(
        corrected,
        base_point=point(0),
        base_state=CounterDriver(corrected).capture(),
    )

    result = verify_interval(
        artifact,
        CounterDriver(ORACLE),
        CounterDriver(corrected),
        point(0),
        point(len(VALUES)),
    )

    assert result.equivalent
    assert not artifact.capture_profile().same_execution_as(corrected)
    assert artifact.trusted


def test_execution_enrichment_is_idempotent_and_detects_nondeterminism(tmp_path):
    artifact = make_artifact(tmp_path)
    recorder = ReplayEvidenceRecorder()
    recorder.enter("function:a", point(1))
    recorder.observe_transfer("function:a", "function:b", "call", point(2))
    recorder.exit("function:a", point(3))
    evidence = recorder.evidence(
        ORACLE,
        provenance={
            "kind": "post-hoc-oracle-replay",
            "observer_digest": "observer-a",
            "event_stream_sha256": artifact.event_stream_sha256,
        },
    )

    assert artifact.set_execution_evidence(
        ORACLE, evidence, visits=recorder.visits)
    revision = json.loads(artifact.path.read_text())["revision"]
    assert not artifact.set_execution_evidence(
        ORACLE, evidence, visits=recorder.visits)
    assert json.loads(artifact.path.read_text())["revision"] == revision

    changed = ReplayEvidenceRecorder()
    changed.enter("function:a", point(1))
    changed.observe_transfer("function:a", "function:b", "call", point(2))
    changed.observe_transfer("function:a", "function:b", "call", point(3))
    changed.exit("function:a", point(4))
    with pytest.raises(ReplayError, match="different observations"):
        artifact.set_execution_evidence(
            ORACLE,
            changed.evidence(ORACLE, provenance=evidence.provenance),
            visits=changed.visits,
        )


def test_repeated_interval_uses_nearest_profile_specific_boundary(tmp_path):
    artifact = make_artifact(tmp_path)
    verify_interval(artifact, CounterDriver(ORACLE), CounterDriver(NATIVE), point(4), point(8))
    oracle, native = CounterDriver(ORACLE), CounterDriver(NATIVE)

    result = verify_interval(artifact, oracle, native, point(6), point(7))

    assert result.equivalent
    assert oracle.calls == [(4, 6), (6, 7)]
    assert native.calls == [(4, 6), (6, 7)]


def test_interval_rejects_diverged_start_without_caching_candidate(tmp_path):
    buggy = profile("buggy-start", "candidate", "detached-native", "native-v1")
    artifact = make_artifact(tmp_path, candidate=buggy)

    with pytest.raises(ReplayError, match="non-equivalent"):
        verify_interval(
            artifact,
            CounterDriver(ORACLE),
            CounterDriver(buggy, bug_at=2),
            point(4),
            point(6),
        )

    assert not artifact.has_cached(buggy, point(4))
    manifest = json.loads((tmp_path / "replay" / "replay.json").read_text())
    kinds = [entry["kind"] for entry in manifest["points"][point(4).key]["annotations"]]
    assert "invalid-interval-start" in kinds


def test_bisection_persists_latest_valid_point_not_diverged_candidate(tmp_path):
    buggy = profile("buggy-native", "candidate", "detached-native", "native-v1")
    artifact = make_artifact(tmp_path, candidate=buggy)

    found = bisect_divergence(
        artifact, CounterDriver(ORACLE), CounterDriver(buggy, bug_at=6),
        [point(i) for i in range(11)],
    )

    assert found is not None
    before, after, result = found
    assert (before, after) == (point(6), point(7))
    assert not result.equivalent
    manifest = json.loads((tmp_path / "replay" / "replay.json").read_text())
    kinds = [entry["kind"] for entry in manifest["points"][point(6).key]["annotations"]]
    assert "latest-valid-before-divergence" in kinds
    assert "bisected-pre-divergence" in kinds
    assert not artifact.has_cached(buggy, point(7))


def test_profile_identity_change_rejects_cache(tmp_path):
    artifact = make_artifact(tmp_path)
    changed = profile("native", "candidate", "detached-native", "native-v1",
                      runtime="runtime-b")
    with pytest.raises(StaleReplayError, match="identity changed"):
        artifact.cached_points(changed)


def test_changed_base_snapshot_identity_rejects_cached_boundary(tmp_path):
    artifact = make_artifact(tmp_path)
    verify_interval(artifact, CounterDriver(ORACLE), CounterDriver(NATIVE), point(2), point(4))
    manifest_path = tmp_path / "replay" / "replay.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["profiles"]["native"]["base_state_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest))

    reopened = ReplayArtifact.open(tmp_path / "replay")
    with pytest.raises(StaleReplayError, match="base snapshot identity"):
        reopened.restore(NATIVE, point(2))


def test_boundary_delta_supports_added_removed_and_resized_regions(tmp_path):
    artifact = ReplayArtifact.create(
        tmp_path / "variable-regions",
        timeline_id=TIMELINE,
        events=(),
        metadata={"recording_profile_id": ORACLE.profile_id},
        page_size=4,
    )
    base = ContinuationState(
        "machine-v1",
        {"phase": "base"},
        {
            "grown": b"abcdefgh",
            "removed": b"gone",
            "shrunk": b"12345678",
        },
        0,
    )
    target = ContinuationState(
        "machine-v1",
        {"phase": "target"},
        {
            "added": b"new-region",
            "grown": b"abXXefgh-more",
            "shrunk": b"1234",
        },
        0,
    ).normalized()
    artifact.register_profile(
        ORACLE, base_point=point(0), base_state=base)

    assert artifact.cache(ORACLE, point(1), target)

    reopened = ReplayArtifact.open(artifact.directory)
    assert reopened.restore(ORACLE, point(1)) == target
    record = reopened.require_profile(ORACLE)
    boundary = json.loads(
        reopened._resolve(
            record["boundaries"][point(1).key]["manifest"]
        ).read_text(encoding="utf-8")
    )
    assert boundary["regions"] == [
        {"name": "added", "size": 10},
        {"name": "grown", "size": 13},
        {"name": "shrunk", "size": 4},
    ]
    assert {page["region"] for page in boundary["changed_pages"]} == {
        "added", "grown",
    }


def test_artifact_metadata_is_returned_as_a_detached_copy(tmp_path):
    artifact = make_artifact(tmp_path)
    metadata = artifact.metadata
    metadata["purpose"] = "mutated"
    assert artifact.metadata["purpose"] == "hook-verification"


def test_published_boundary_is_restorable_from_another_process(tmp_path):
    artifact = make_artifact(tmp_path)
    verify_interval(
        artifact, CounterDriver(ORACLE), CounterDriver(NATIVE),
        point(1), point(3))
    code = (
        "from dos_re.replay import ReplayArtifact,ReplayPoint;"
        f"a=ReplayArtifact.open(r'{artifact.directory}');"
        "p=[profile for profile,count in a.profiles() "
        "if profile.profile_id=='native'][0];"
        f"s=a.restore(p,ReplayPoint(3,{TIMELINE!r}));"
        "assert s.event_cursor==3"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        timeout=30, env=env)
    assert result.returncode == 0, result.stderr


def test_interrupted_boundary_publication_recovers(tmp_path):
    for stage, expected_cached in (
        ("prepared", False),
        ("pending-indexed", True),
        ("directory-published", True),
        ("manifest-indexed", True),
    ):
        artifact = make_artifact(tmp_path / stage)
        driver = CounterDriver(ORACLE)
        driver.replay_to(artifact, point(3))

        def interrupt(current, target=stage):
            if current == target:
                raise RuntimeError(f"interrupted after {target}")

        artifact._publication_stage = interrupt
        with pytest.raises(RuntimeError, match="interrupted"):
            artifact.cache(ORACLE, point(3), driver.capture())

        reopened = ReplayArtifact.open(artifact.directory)
        assert reopened.has_cached(ORACLE, point(3)) is expected_cached
        if not expected_cached:
            assert reopened.cache(ORACLE, point(3), driver.capture())
        assert reopened.restore(ORACLE, point(3)).digest == driver.capture().digest


def test_unindexed_derived_boundary_is_discarded_and_cannot_poison_cache(tmp_path):
    artifact = make_artifact(tmp_path)
    directory = (
        artifact.directory / "profiles" / ORACLE.storage_key
        / "boundaries" / point(3).key
    )
    directory.mkdir(parents=True)
    (directory / "partial").write_text("not authoritative", encoding="utf-8")

    reopened = ReplayArtifact.open(artifact.directory)
    assert not directory.exists()
    driver = CounterDriver(ORACLE)
    driver.replay_to(reopened, point(3))
    assert reopened.cache(ORACLE, point(3), driver.capture())
    assert reopened.restore(ORACLE, point(3)).digest == driver.capture().digest


def test_live_concurrent_writer_is_rejected_without_manifest_damage(tmp_path):
    artifact = make_artifact(tmp_path)
    lock = artifact.directory / ".replay-writer.lock"
    lock.write_text(json.dumps({
        "pid": os.getpid(), "host": socket.gethostname(), "token": "other",
    }))
    try:
        with pytest.raises(ConcurrentReplayWriterError):
            artifact.annotate(point(1), kind="test", metadata={})
    finally:
        lock.unlink()
    reopened = ReplayArtifact.open(artifact.directory)
    assert reopened.event_stream_sha256 == artifact.event_stream_sha256


def test_separate_artifact_handles_reload_before_each_mutation(tmp_path):
    artifact = make_artifact(tmp_path)
    first = ReplayArtifact.open(artifact.directory)
    second = ReplayArtifact.open(artifact.directory)
    first.annotate(point(1), kind="first", metadata={})
    second.annotate(point(1), kind="second", metadata={})

    manifest = json.loads((artifact.directory / "replay.json").read_text())
    kinds = [
        entry["kind"]
        for entry in manifest["points"][point(1).key]["annotations"]
    ]
    assert kinds == ["first", "second"]


def test_machine_projection_covers_metadata_regions_and_event_cursor():
    state = ContinuationState(
        "machine-v1", {"cpu": {"ip": 4}, "device": {"latch": 9}},
        {"ram": b"abc"}, 7,
    )
    projection = machine_projection(state, schema_id="complete-machine-v1")
    assert projection.event_cursor == 7
    assert projection.fields["metadata"]["device"]["latch"] == 9
    assert projection.regions == {"ram": b"abc"}


def test_function_visit_interval_handles_recursive_calls(tmp_path):
    artifact = make_artifact(tmp_path)
    visits = FunctionVisitIndex()
    visits.enter("image-a:function-1", point(2))
    visits.enter("image-a:function-1", point(3))
    visits.exit("image-a:function-1", point(4))
    visits.exit("image-a:function-1", point(5))
    visits.enter("image-a:function-1", point(8))
    visits.exit("image-a:function-1", point(9))
    artifact.set_function_visits(visits)

    record = visits.records()[0]
    assert record.invocation_count == 3
    assert record.first_entry == point(2)
    assert record.last_exit == point(9)
    assert artifact.function_interval("image-a:function-1") == (point(2), point(9))


def test_final_incomplete_invocation_invalidates_a_prior_completed_interval(tmp_path):
    artifact = make_artifact(tmp_path)
    visits = FunctionVisitIndex()
    visits.enter("image-a:function-1", point(2))
    visits.exit("image-a:function-1", point(5))
    visits.enter("image-a:function-1", point(8))
    artifact.set_function_visits(visits)

    record = artifact.function_visits()[0]
    assert record.last_exit == point(5)
    assert record.incomplete is True
    with pytest.raises(ReplayError, match="no completed replay interval"):
        artifact.function_interval("image-a:function-1")
