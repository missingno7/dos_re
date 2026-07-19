"""Protected-mode ReplayArtifact adapter and frame-clock tests."""
from types import SimpleNamespace

from dos_re.cpu import HaltExecution
from dos_re.cpu386 import CPU386, FlatMemory
from dos_re.input_demo import MOUSE_CHANNEL, mouse_payload
from dos_re.pm_input_demo import (
    FrameClock,
    KEY_CHANNEL,
    ProtectedModeInputAdapter,
    key_payload,
)
from dos_re.replay import (
    ContinuationState,
    ExecutionProfile,
    ReplayArtifact,
    ReplayRecording,
)


def _profile():
    return ExecutionProfile(
        "pm-candidate", "candidate", "pm-interpreter", "image", "runtime",
        "devices", "dos-re-pm-continuation-v1", "machine-v1")


def _state(cursor, value=0):
    return ContinuationState(
        "dos-re-pm-continuation-v1", {"cpu": {"eip": value}},
        {"memory": bytes([value]) * 32, "vga-planes": bytes(16)}, cursor)


def test_protected_mode_recording_uses_replay_artifact(tmp_path):
    recording = ReplayRecording(
        tmp_path / "pm", timeline_id="pm-frame-entry:119d40:v1",
        profile=_profile(), base_state=_state(0),
        metadata={"frame_tick_addr": 0x119D40, "mouse_present": True})
    recording.add(3, KEY_CHANNEL, key_payload("space", True))
    recording.add(3, KEY_CHANNEL, key_payload("space", False))
    recording.add(5, MOUSE_CHANNEL, mouse_payload(0.5, 0.9, 0))
    artifact = recording.finish(8, end_state=_state(3, 9))

    reopened = ReplayArtifact.open(artifact.directory)
    assert not (artifact.directory / "input_demo.json").exists()
    adapter = ProtectedModeInputAdapter(reopened.events)
    keys, mouse = [], []
    dos = SimpleNamespace(set_mouse_norm=lambda *sample: mouse.append(sample))
    adapter.apply(5, dos, deliver_key=lambda _dos, name, make: keys.append((name, make)))
    assert keys == [("space", True), ("space", False)]
    assert mouse[-1] == (0.5, 0.9, 0)


def test_frame_clock_counts_once_per_call():
    code, frame = 0x1000, 0x2000
    mem = FlatMemory(size=0x10000)
    import struct
    disp = frame - (0x1005 + 5)
    blob = b"\xB9\x04\x00\x00\x00" + b"\xE8" + struct.pack("<i", disp) + b"\xE2\xF9\xF4"
    mem.load(code, blob)
    mem.load(frame, b"\xC3")
    cpu = CPU386(mem, eip=code, esp=0x8000)
    frames = []
    FrameClock(cpu, frame, lambda ordinal: frames.append(ordinal))
    try:
        cpu.run(1000)
    except HaltExecution:
        pass
    assert frames == [0, 1, 2, 3]
