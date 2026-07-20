"""Real-mode ReplayArtifact recording and input-adapter tests."""
from types import SimpleNamespace

from dos_re.input_demo import (
    DOS_KEY_CHANNEL,
    MOUSE_CHANNEL,
    SCAN_CHANNEL,
    RealModeInputAdapter,
    bios_key_value_from_scancode,
    dos_key_payload,
    mouse_payload,
    mouse_sample,
    scan_payload,
)
from dos_re.replay import (
    ContinuationState,
    ExecutionProfile,
    ReplayArtifact,
    ReplayRecording,
)


class DummyRuntime:
    def __init__(self):
        self.scans = []
        self.mouse = []
        self.dos = SimpleNamespace(
            key_queue=[],
            set_mouse_norm=lambda *sample: self.mouse.append(sample))


def _profile():
    return ExecutionProfile(
        "real-oracle", "oracle", "interpreter", "image", "runtime", "devices",
        "dos-re-real-mode-continuation-v1", "machine-v1")


def _state(cursor, value=0):
    return ContinuationState(
        "dos-re-real-mode-continuation-v1",
        {"cpu": {"ip": value}, "devices": {"timer": value}},
        {"memory": bytes([value]) * 32},
        cursor,
    )


def test_real_mode_recording_is_one_replay_artifact_and_replays(tmp_path):
    recording = ReplayRecording(
        tmp_path / "real", timeline_id="real-frames-v1",
        profile=_profile(), base_state=_state(0),
        metadata={"mouse_present": True})
    recording.add(0, SCAN_CHANNEL, scan_payload(0x4D))
    recording.add(2, DOS_KEY_CHANNEL, dos_key_payload(0x39, " ", 0x3920))
    recording.add(2, MOUSE_CHANNEL, mouse_payload(0.5, 0.75, 1))
    artifact = recording.finish(3, end_state=_state(3, 7))

    assert (artifact.directory / "replay.json").is_file()
    assert not (artifact.directory / "input_demo.json").exists()
    reopened = ReplayArtifact.open(artifact.directory)
    adapter = RealModeInputAdapter(reopened.events)
    rt = DummyRuntime()
    deliver = lambda runtime, scan: runtime.scans.append(scan)
    assert adapter.apply_to_runtime(0, rt, deliver=deliver) == 1
    assert adapter.apply_to_runtime(2, rt, deliver=deliver) == 2
    assert rt.scans == [0x4D]
    assert rt.dos.key_queue == [0x3920]
    assert rt.mouse[-1] == (0.5, 0.75, 1)


def test_adapter_can_feed_oracle_and_candidate_and_seek_cursor():
    recording_events = []
    from dos_re.replay import ReplayEvent, ReplayPoint
    timeline = "real-frames-v1"
    recording_events.append(
        ReplayEvent(ReplayPoint(1, timeline), 0, SCAN_CHANNEL, scan_payload(0x39)))
    recording_events.append(
        ReplayEvent(ReplayPoint(1, timeline), 1, MOUSE_CHANNEL, mouse_payload(0.2, 0.3, 0)))
    adapter = RealModeInputAdapter(recording_events)
    oracle, candidate = DummyRuntime(), DummyRuntime()
    adapter.apply_to_runtimes(
        1, (oracle, candidate),
        deliver=lambda runtime, scan: runtime.scans.append(scan))
    assert oracle.scans == candidate.scans == [0x39]
    assert oracle.mouse == candidate.mouse
    adapter.seek(1)
    adapter.apply_to_runtime(1, oracle, deliver=lambda runtime, scan: None)
    assert adapter.event_cursor == 2


def test_input_normalization_has_no_legacy_mouse_fields():
    assert mouse_sample(-1, 2, 9) == (0.0, 1.0, 1)
    assert mouse_payload(0.25, 0.75, 2) == {
        "u": 0.25, "v": 0.75, "buttons": 2}
    assert bios_key_value_from_scancode(0x39, "") == 0x3920
    assert bios_key_value_from_scancode(0x3B, "") is None
