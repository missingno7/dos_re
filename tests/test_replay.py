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
    ExecutionProfile,
    FunctionVisitIndex,
    ReplayArtifact,
    ReplayError,
    ReplayEvent,
    ReplayPoint,
    StaleReplayError,
    bisect_divergence,
    machine_projection,
    verify_interval,
)


TIMELINE = "hook-verification-instruction-boundaries-v1"
VALUES = [3, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43]


def point(n: int) -> ReplayPoint:
    return ReplayPoint(n, TIMELINE)


def profile(profile_id: str, role: str, implementation: str,
            continuation_schema: str, *, runtime: str = "runtime-a") -> ExecutionProfile:
    return ExecutionProfile(
        profile_id=profile_id,
        role=role,
        implementation=implementation,
        image="image-a",
        runtime=runtime,
        devices="devices-a",
        continuation_schema=continuation_schema,
        projection_schema="game-state-v1",
        overrides=() if role == "oracle" else ("func:a", "func:b"),
    )


ORACLE = profile("oracle", "oracle", "interpreter", "machine-v1")
NATIVE = profile("native", "candidate", "detached-native", "native-v1")


class CounterDriver:
    """Same logical program with deliberately different continuation layouts."""

    def __init__(self, execution_profile: ExecutionProfile, *, bug_at: int | None = None):
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
    def profile(self) -> ExecutionProfile:
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


def make_artifact(tmp_path, *, candidate=NATIVE):
    events = [ReplayEvent(point(i), i, "input", {"value": value})
              for i, value in enumerate(VALUES)]
    artifact = ReplayArtifact.create(
        tmp_path / "demo", timeline_id=TIMELINE, events=events,
        metadata={"purpose": "hook-verification"}, page_size=8,
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

    manifest = json.loads((tmp_path / "demo" / "replay.json").read_text())
    assert set(manifest["profiles"]) == {"oracle", "native"}
    assert manifest["points"][point(9).key]["annotations"][0]["kind"] == "verified-endpoint"


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
    manifest = json.loads((tmp_path / "demo" / "replay.json").read_text())
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
    manifest = json.loads((tmp_path / "demo" / "replay.json").read_text())
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
    manifest_path = tmp_path / "demo" / "replay.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["profiles"]["native"]["base_state_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest))

    reopened = ReplayArtifact.open(tmp_path / "demo")
    with pytest.raises(StaleReplayError, match="base snapshot identity"):
        reopened.restore(NATIVE, point(2))


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
