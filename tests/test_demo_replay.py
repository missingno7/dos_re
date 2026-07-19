from __future__ import annotations

import json
from copy import deepcopy

import pytest

from dos_re.demo_replay import (
    CacheIdentity,
    DemoPoint,
    FunctionVisitIndex,
    MachineImage,
    ReplayArtifact,
    ReplayArtifactError,
    StaleReplayCacheError,
    machine_image_sha256,
    replay_interval,
)


TIMELINE = "test-instruction-boundaries-v1"
EVENTS = [3, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43]


def point(ordinal: int) -> DemoPoint:
    return DemoPoint(TIMELINE, ordinal)


class DeterministicDriver:
    """Tiny machine with CPU/device/scheduler state and two memory regions."""

    def __init__(self) -> None:
        self._point = point(0)
        self.state = {
            "cpu": {"acc": 0, "flags": 0},
            "timer": {"phase": 5},
            "interrupts": {"pending": []},
            "device": {"latch": 0},
            "scheduler": {"slot": 0},
        }
        self.ram = bytearray(48)
        self.vram = bytearray(10)
        self.cursor = 0
        self.replay_calls: list[tuple[int, int]] = []

    @property
    def current_point(self) -> DemoPoint:
        return self._point

    def capture(self) -> MachineImage:
        return MachineImage(
            state=deepcopy(self.state),
            memory_regions={"ram": bytes(self.ram), "vram": bytes(self.vram)},
            event_cursor=self.cursor,
        )

    def restore(self, image: MachineImage, restored_point: DemoPoint) -> None:
        self.state = deepcopy(dict(image.state))
        self.ram[:] = image.memory_regions["ram"]
        self.vram[:] = image.memory_regions["vram"]
        self.cursor = image.event_cursor
        self._point = restored_point

    def replay_to(self, target: DemoPoint) -> None:
        assert target.timeline_id == TIMELINE
        if target.ordinal < self._point.ordinal:
            raise ValueError("test driver cannot replay backwards")
        self.replay_calls.append((self._point.ordinal, target.ordinal))
        while self._point.ordinal < target.ordinal:
            event = EVENTS[self.cursor]
            ordinal = self._point.ordinal
            self.state["cpu"]["acc"] = (self.state["cpu"]["acc"] + event) & 0xFFFF
            self.state["cpu"]["flags"] ^= event & 1
            self.state["timer"]["phase"] = (self.state["timer"]["phase"] + 3) % 17
            self.state["device"]["latch"] = event
            self.state["scheduler"]["slot"] = (ordinal + 1) % 4
            if event % 11 == 0:
                self.state["interrupts"]["pending"].append(ordinal + 1)
            self.ram[(ordinal * 9) % len(self.ram)] ^= event
            self.vram[ordinal % len(self.vram)] = (event * 2) & 0xFF
            self.cursor += 1
            self._point = point(ordinal + 1)


def make_artifact(tmp_path):
    demo = tmp_path / "demo"
    demo.mkdir()
    (demo / "input_demo.json").write_text(
        json.dumps({"version": 2, "snapshot": "snapshot", "metadata": {},
                    "events": [{"boundary": i, "value": value}
                               for i, value in enumerate(EVENTS)]}),
        encoding="utf-8",
    )
    driver = DeterministicDriver()
    base = driver.capture()
    identity = CacheIdentity.build(
        event_stream=EVENTS,
        base_image=base,
        executable_image="sha256:test-executable",
        runtime_implementation="sha256:test-runtime",
        device_model="sha256:test-devices",
        snapshot_format="test-complete-state-v1",
    )
    artifact = ReplayArtifact.create(
        demo,
        base_point=point(0),
        base_image=base,
        identity=identity,
        page_size=16,
    )
    return demo, artifact, identity


def test_arbitrary_interval_lazily_caches_x_as_base_relative_pages(tmp_path):
    demo, artifact, _ = make_artifact(tmp_path)
    driver = DeterministicDriver()

    result = replay_interval(artifact, driver, point(7), point(10))

    assert result.restored_from == point(0)
    assert result.start_was_cached is False
    assert driver.replay_calls == [(0, 7), (7, 10)]
    assert artifact.has_boundary(point(7))
    assert not artifact.has_boundary(point(10))

    # The persisted X reconstructs exactly, including non-memory state and the
    # event cursor, and every stored page differs from the ORIGINAL base.
    cached_x = artifact.load_boundary(point(7))
    oracle = DeterministicDriver()
    oracle.replay_to(point(7))
    assert cached_x == oracle.capture().normalized()

    manifest_path = demo / "replay" / "boundaries" / point(7).key / "state.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["event_cursor"] == 7
    assert manifest["state"]["device"] == {"latch": EVENTS[6]}
    assert manifest["changed_pages"]
    base = artifact.load_boundary(point(0))
    for changed in manifest["changed_pages"]:
        region = cached_x.memory_regions[changed["region"]]
        start = changed["page_index"] * artifact.page_size
        end = start + changed["size"]
        assert region[start:end] != base.memory_regions[changed["region"]][start:end]

    # The endpoint exposes complete continuation state, not a RAM-only digest.
    oracle.replay_to(point(10))
    assert machine_image_sha256(result.endpoint) == machine_image_sha256(oracle.capture())


def test_next_interval_restores_nearest_cache_and_new_delta_is_not_chained(tmp_path):
    _, artifact, _ = make_artifact(tmp_path)
    replay_interval(artifact, DeterministicDriver(), point(7), point(10))

    driver = DeterministicDriver()
    result = replay_interval(artifact, driver, point(8), point(9), cache_y=True)

    assert result.restored_from == point(7)
    assert driver.replay_calls == [(7, 8), (8, 9)]
    assert artifact.cached_points() == (point(0), point(7), point(8), point(9))

    # Point 8 remains independently restorable from base even if point 7's
    # manifest is made unavailable: loading 8 never follows a delta chain.
    boundary_7 = artifact._index["boundaries"].pop(point(7).key)
    try:
        restored_8 = artifact.load_boundary(point(8))
    finally:
        artifact._index["boundaries"][point(7).key] = boundary_7
    oracle = DeterministicDriver()
    oracle.replay_to(point(8))
    assert restored_8 == oracle.capture().normalized()


def test_existing_x_is_reused_and_demo_manifest_links_the_index(tmp_path):
    demo, artifact, _ = make_artifact(tmp_path)
    replay_interval(artifact, DeterministicDriver(), point(4), point(6))

    driver = DeterministicDriver()
    result = replay_interval(artifact, driver, point(4), point(5))
    assert result.start_was_cached is True
    assert result.restored_from == point(4)
    assert driver.replay_calls == [(4, 4), (4, 5)]

    manifest = json.loads((demo / "input_demo.json").read_text(encoding="utf-8"))
    assert manifest["metadata"]["replay_index"]["path"] == "replay/index.json"


def test_stale_identity_is_rejected_before_restore(tmp_path):
    demo, _, identity = make_artifact(tmp_path)
    stale = CacheIdentity(
        event_stream_sha256=identity.event_stream_sha256,
        base_snapshot_sha256=identity.base_snapshot_sha256,
        executable_image=identity.executable_image,
        runtime_implementation="sha256:new-runtime",
        device_model=identity.device_model,
        snapshot_format=identity.snapshot_format,
    )
    with pytest.raises(StaleReplayCacheError, match="identity mismatch"):
        ReplayArtifact.open(demo, expected_identity=stale)


def test_corrupt_changed_page_is_rejected(tmp_path):
    demo, artifact, _ = make_artifact(tmp_path)
    replay_interval(artifact, DeterministicDriver(), point(5), point(6))
    manifest_path = demo / "replay" / "boundaries" / point(5).key / "state.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    page_path = demo / manifest["changed_pages"][0]["file"]
    page_path.write_bytes(b"not zlib")

    with pytest.raises(ReplayArtifactError, match="changed page"):
        artifact.load_boundary(point(5))


def test_function_visits_handle_recursion_and_mirror_into_demo_metadata(tmp_path):
    demo, artifact, _ = make_artifact(tmp_path)
    visits = FunctionVisitIndex()

    visits.observe_entry("lifted:image-a:func-10", point(2))
    visits.observe_entry("lifted:image-a:func-10", point(3))  # recursive
    visits.observe_exit("lifted:image-a:func-10", point(4))   # inner return
    assert visits.visits()[0].last_exit is None
    visits.observe_exit("lifted:image-a:func-10", point(5))   # outer return
    visits.observe_entry("lifted:image-a:func-10", point(8))
    visits.observe_exit("lifted:image-a:func-10", point(9))
    visits.observe_entry("lifted:image-a:unfinished", point(10))
    artifact.update_function_visits(visits)

    records = {visit.function_id: visit for visit in artifact.function_visits().visits()}
    complete = records["lifted:image-a:func-10"]
    assert complete.invocation_count == 3
    assert complete.first_entry == point(2)
    assert complete.last_exit == point(9)
    unfinished = records["lifted:image-a:unfinished"]
    assert unfinished.invocation_count == 1
    assert unfinished.first_entry == point(10)
    assert unfinished.last_exit is None

    manifest = json.loads((demo / "input_demo.json").read_text(encoding="utf-8"))
    mirrored = {item["function_id"]: item
                for item in manifest["metadata"]["function_visits"]}
    assert mirrored["lifted:image-a:func-10"]["invocation_count"] == 3
    assert mirrored["lifted:image-a:func-10"]["last_exit"]["ordinal"] == 9


def test_function_exit_without_entry_fails_loud(tmp_path):
    visits = FunctionVisitIndex()
    with pytest.raises(ValueError, match="without active"):
        visits.observe_exit("lifted:missing", point(1))
