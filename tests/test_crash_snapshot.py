"""A crash must leave the machine behind, and that machine must be resumable.

The value is not the report -- it is that the fault stops being a destination.
Every deep failure in this project so far (a wall violation 1,100 frames into a
cold boot, an iteration guard at frame 280, a palette wrong once in 1,832
frames) was chased by writing a probe and REPLAYING FROM FRAME 0 to reach it
again. The state was sitting right there when it broke, and got thrown away.

So the load-bearing test here is not "it wrote a file": it is
test_a_crash_snapshot_resumes_at_the_fault.
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from dos_re.crash import crash_dir, save_crash, save_recovery_frontier
from dos_re.execution import (
    ImplementationCatalog,
    ImplementationDescriptor,
    ImplementationEntry,
    ImplementationOrigin,
    ProgramCoverage,
    plan_execution,
    profile_configuration,
)
from dos_re.runtime_miss import RuntimeExecutionFrontier
from dos_re.runtime import create_runtime


def _tiny_exe(tmp_path: Path) -> str:
    exe = tmp_path / "T.EXE"
    hdr = bytearray(32)
    hdr[0:2] = b"MZ"
    struct.pack_into("<H", hdr, 2, 32 + 1)
    struct.pack_into("<H", hdr, 4, 1)
    struct.pack_into("<H", hdr, 8, 2)
    exe.write_bytes(bytes(hdr) + b"\xf4")
    return str(exe)


def test_a_crash_snapshot_resumes_at_the_fault(tmp_path: Path) -> None:
    """THE POINT: reload it and you are standing where it broke, with the same
    registers and the same memory -- no replay."""
    from dos_re.snapshot import load_snapshot
    exe = _tiny_exe(tmp_path)
    rt = create_runtime(exe, game_root=str(tmp_path))
    rt.cpu.s.cs, rt.cpu.s.ip = 0x1010, 0x4866      # "the fault"
    rt.cpu.s.ax, rt.cpu.s.bx = 0xDEAD, 0xBEEF
    rt.cpu.mem.data[0x1234] = 0x5A

    out = save_crash(rt, tmp_path / "c", exc=RuntimeError("boom"), frame=280)

    rt2 = load_snapshot(exe, out, game_root=str(tmp_path))
    assert (rt2.cpu.s.cs, rt2.cpu.s.ip) == (0x1010, 0x4866)
    assert (rt2.cpu.s.ax, rt2.cpu.s.bx) == (0xDEAD, 0xBEEF)
    assert rt2.cpu.mem.data[0x1234] == 0x5A


def test_it_records_where_and_why(tmp_path: Path) -> None:
    rt = create_runtime(_tiny_exe(tmp_path), game_root=str(tmp_path))
    rt.cpu.s.cs, rt.cpu.s.ip = 0x1010, 0x5FED
    try:
        raise ValueError("the int blocked")
    except ValueError as exc:
        save_crash(rt, tmp_path / "c", exc=exc, status="wall", frame=1115,
                   parks={"434A": 74})

    info = json.loads((tmp_path / "c" / "crash.json").read_text())
    assert info["where"] == "1010:5FED"
    assert info["status"] == "wall"
    assert info["exception"]["type"] == "ValueError"
    assert "the int blocked" in info["exception"]["message"]
    assert "Traceback" in info["exception"]["traceback"]
    # the caller's context: what the machine itself cannot say
    assert info["context"]["frame"] == 1115
    assert info["context"]["parks"] == {"434A": 74}
    assert info["registers"]["ip"] == "5FED"


def test_a_failing_write_does_not_raise(tmp_path: Path) -> None:
    """It runs on a path that is ALREADY failing. A crash handler that crashes
    costs the report it was trying to save -- and replaces a real diagnosis with
    its own stack trace."""
    rt = create_runtime(_tiny_exe(tmp_path), game_root=str(tmp_path))
    clash = tmp_path / "not-a-dir"
    clash.write_text("I am a file")
    save_crash(rt, clash, exc=RuntimeError("boom"))     # must not raise


def test_crash_dir_does_not_read_the_clock(tmp_path: Path) -> None:
    """The stamp is the caller's, so a run stays reproducible and a path is
    pinnable."""
    assert crash_dir(tmp_path, "vmless", "20260717_161500").name == \
        "vmless_20260717_161500"


def test_runtime_miss_writes_deterministic_recovery_frontier(
    tmp_path: Path,
) -> None:
    rt = create_runtime(_tiny_exe(tmp_path), game_root=str(tmp_path))
    target = "game:point:1010:1234"
    plan = plan_execution(
        profile_configuration("detached", program_identity="game:test"),
        ProgramCoverage(
            roots=(target,),
            reachable=frozenset({target}),
            evidence_identity="test-coverage",
        ),
        ImplementationCatalog((ImplementationEntry(
            ImplementationDescriptor(
                "generated-test",
                frozenset({target}),
                ImplementationOrigin.GENERATED,
                implementation_digest="generated-test-v1",
            ),
        ),)),
    )
    rt.execution_plan = plan
    rt.execution_carrier_id = "generated-test-carrier"
    rt._dos_re_replay_context = {
        "artifact": "replay-smoke",
        "replay_cursor": 7,
        "semantic_timeline_position": 19,
        "last_completed_boundary": 18,
        "current_partially_executed_boundary": 19,
        "nearest_cached_boundary": 16,
    }
    rt.cpu.s.cs, rt.cpu.s.ip = 0x1010, 0x1234
    rt.cpu.interp_forbidden = True
    with pytest.raises(RuntimeExecutionFrontier) as caught:
        rt.cpu.step()

    ids = []
    for name in ("frontier-a", "frontier-b"):
        out = save_recovery_frontier(
            rt,
            tmp_path / name,
            exc=caught.value,
            source_identity="game:function:caller",
            target_identity=target,
            selected_provider="generated-test",
            candidate_containing_identity="game:function:caller",
            recent_atlas_path=("game:function:caller", target),
        )
        payload = json.loads(
            (out / "recovery_frontier.json").read_text(encoding="utf-8")
        )
        ids.append(payload["frontier_id"])
        assert payload["target_address"] == "1010:1234"
        assert payload["target_identity"] == target
        assert payload["active_execution_carrier"] == "generated-test-carrier"
        assert payload["selected_provider"] == "generated-test"
        assert payload["replay"]["nearest_cached_boundary"] == 16
        assert payload["execution_plan"]["fallback_policy"] == "forbidden"
        assert (out / "memory_1mb.bin").is_file()
        assert (out / "state.json").is_file()
    assert ids[0] == ids[1]
