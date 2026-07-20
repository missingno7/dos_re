"""Virtual time is MACHINE STATE and must survive a snapshot round-trip.

The PIT channel-0 down-counter is derived from ``cpu.instruction_count``
(``dos._pit_channel0_live_value``), so the phase a program can MEASURE (latch
port 43h, read port 40h) is part of the machine exactly like the DAC or the
BIOS tick count.  ``write_snapshot`` records it as ``steps``; the loaders must
restore it, or every restored runtime restarts at t=0 while an interpreted
oracle arrives carrying the loader's count.

Regression: VGA Lemmings' High Performance PC timer calibration
(1010:15AD/1602) measures the ABSOLUTE PIT phase and stores it as the game's
tick reload.  The EXE-free boot image recorded steps=408558 and the headless
loader dropped it, so the calibrated reload diverged from the oracle by
exactly (steps * 3) mod 0x10000 ticks (2026-07-17).
"""
from __future__ import annotations

import json

import pytest

from dos_re.runtime_core import enable_sound_blaster
from dos_re.snapshot_runtime import load_snapshot_headless
from dos_re.snapshot import capture_runtime_continuation, apply_runtime_continuation


def _write_min_snapshot(tmp_path, steps: int):
    # a full real-mode image: 1MB + the 4 EGA shadow planes (runtime_core
    # validates the size)
    (tmp_path / "memory_1mb.bin").write_bytes(bytes(0x140000))
    (tmp_path / "state.json").write_text(json.dumps({
        "cpu": {"ax": 0, "bx": 0, "cx": 0, "dx": 0, "sp": 0xFFFE, "bp": 0,
                "si": 0, "di": 0, "cs": 0x1010, "ds": 0x1010, "es": 0x1010,
                "ss": 0x1010, "ip": 0, "flags": 0x0202},
        "dos": {},
        "program": {"psp_segment": 0},
        "steps": steps,
    }), encoding="utf-8")


def test_headless_load_restores_virtual_time(tmp_path):
    _write_min_snapshot(tmp_path, steps=408558)
    rt = load_snapshot_headless(tmp_path, game_root=tmp_path)
    assert rt.cpu.instruction_count == 408558


def test_headless_load_missing_steps_is_zero(tmp_path):
    _write_min_snapshot(tmp_path, steps=0)
    meta = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    del meta["steps"]
    (tmp_path / "state.json").write_text(json.dumps(meta), encoding="utf-8")
    rt = load_snapshot_headless(tmp_path, game_root=tmp_path)
    assert rt.cpu.instruction_count == 0


def test_pit_phase_survives_the_round_trip(tmp_path):
    """The observable contract: the PIT counter a program would read is the
    same before and after the snapshot boundary."""
    _write_min_snapshot(tmp_path, steps=123456)
    rt = load_snapshot_headless(tmp_path, game_root=tmp_path)
    v = rt.dos._pit_channel0_live_value(rt.cpu)
    reload = rt.dos.pit_channel0_reload or 0x10000
    expected = (reload - (int(123456 * 3.0) % reload)) % reload
    assert v == expected


def test_pit_count_write_anchors_the_phase(tmp_path):
    """The 8254 (re)starts the countdown at a completed count write: a read
    right after loading 0xFFFF must return ~0xFFFF regardless of how much
    absolute time has already elapsed.  VGA Lemmings' High Performance PC
    calibration (load 0xFFFF, wait N hblanks, read back) depends on this;
    deriving the counter from absolute time made its measurement a function
    of the total instruction count since power-on, which interpreter and
    lifted graph never agree on exactly."""
    _write_min_snapshot(tmp_path, steps=999_999)
    rt = load_snapshot_headless(tmp_path, game_root=tmp_path)
    dos, cpu = rt.dos, rt.cpu
    # program ch0: mode 0, lobyte/hibyte, then load 0xFFFF
    dos.port_write(cpu, 0x43, 0x30, 8)
    dos.port_write(cpu, 0x40, 0xFF, 8)
    dos.port_write(cpu, 0x40, 0xFF, 8)
    v0 = dos._pit_channel0_live_value(cpu)
    assert v0 in (0xFFFF, 0), "counter must restart at the write"
    # advance virtual time; the counter must reflect the DELTA, not absolutes
    cpu.instruction_count += 100
    v1 = dos._pit_channel0_live_value(cpu)
    elapsed = int(100 * dos.PIT_TICKS_PER_INSTRUCTION_ESTIMATE)
    assert (v0 - v1) % (dos.pit_channel0_reload or 0x10000) == elapsed


def test_real_mode_replay_continuation_restores_device_and_cursor_state(tmp_path):
    _write_min_snapshot(tmp_path, steps=123)
    rt = load_snapshot_headless(tmp_path, game_root=tmp_path)
    rt.cpu.s.ax = 0xBEEF
    rt.dos.mouse_present = True
    rt.dos.mouse_range = [4, 44, 8, 88]
    rt.dos.kbd_shift = True
    rt.program.memory.ega_pel_pan = 6
    rt.dos.next_handle = 12
    rt.dos._pit_channel0_read_latch = [0x34, 0x12]
    rt.dos.file_overlay["SAVE.DAT"] = bytearray(b"saved")
    state = capture_runtime_continuation(rt, event_cursor=17)

    rt.cpu.s.ax = 0
    rt.dos.mouse_present = False
    rt.dos.mouse_range = [0, 1, 0, 1]
    rt.dos.kbd_shift = False
    rt.program.memory.ega_pel_pan = 0
    rt.dos.next_handle = 5
    rt.dos._pit_channel0_read_latch = []
    rt.dos.file_overlay.clear()
    apply_runtime_continuation(rt, state)

    assert state.event_cursor == 17
    assert rt.cpu.s.ax == 0xBEEF
    assert rt.dos.mouse_present is True
    assert rt.dos.mouse_range == [4, 44, 8, 88]
    assert rt.dos.kbd_shift is True
    assert rt.program.memory.ega_pel_pan == 6
    assert rt.dos.next_handle == 12
    assert rt.dos._pit_channel0_read_latch == [0x34, 0x12]
    assert rt.dos.file_overlay == {"SAVE.DAT": bytearray(b"saved")}


def test_real_mode_replay_continuation_rejects_wall_clock(tmp_path):
    _write_min_snapshot(tmp_path, steps=0)
    rt = load_snapshot_headless(tmp_path, game_root=tmp_path)
    rt.dos.time_source = lambda: 1.0
    with pytest.raises(ValueError, match="wall-clock"):
        capture_runtime_continuation(rt, event_cursor=0)


def test_real_mode_continuation_rejects_incompatible_device_topology(tmp_path):
    _write_min_snapshot(tmp_path, steps=0)
    with_devices = load_snapshot_headless(tmp_path, game_root=tmp_path)
    enable_sound_blaster(with_devices, detection_only=True)
    device_state = capture_runtime_continuation(with_devices, event_cursor=0)

    without_devices = load_snapshot_headless(tmp_path, game_root=tmp_path)
    with pytest.raises(ValueError, match="device topology mismatch for pic"):
        apply_runtime_continuation(without_devices, device_state)

    plain_state = capture_runtime_continuation(without_devices, event_cursor=0)
    with pytest.raises(ValueError, match="device topology mismatch for pic"):
        apply_runtime_continuation(with_devices, plain_state)
