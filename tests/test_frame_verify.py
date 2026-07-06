from __future__ import annotations

import tempfile
from pathlib import Path

from dos_re.frame_verify import (
    FrameSample,
    FrameVerifyConfig,
    compose_compare_rgb,
    diff_rgb_frame,
    dump_divergence,
)
# Frame geometry is game-adapter knowledge; the classic 320x200 mode is used here.
WIDTH, HEIGHT = 320, 200


def _rgb_frame(fill: bytes) -> bytes:
    return fill * (WIDTH * HEIGHT)


def _sample(*, side: str, frame_no: int, rgb: bytes) -> FrameSample:
    return FrameSample(
        side=side,
        frame_no=frame_no,
        kind="present",
        hook=(0x1010, 0x447B),
        cs=0x1010,
        ip=0x447B,
        steps_since_start=123,
        boundary_steps=45,
        display_start=0,
        raw_crc=0x12345678,
        rgb_crc=0x9ABCDEF0,
        raw=b"\x00" * 0x4000,
        rgb=rgb,
        recent_hooks=("1010:447B frame_verify_candidate_present enter=1010:447B",),
        width=WIDTH,
        height=HEIGHT,
        context="tandy",
    )


def test_compose_compare_rgb_keeps_all_three_panels():
    ref = _rgb_frame(b"\x10\x20\x30")
    cand = _rgb_frame(b"\x40\x50\x60")
    diff = diff_rgb_frame(ref, cand)

    compare = compose_compare_rgb(ref, cand, diff, width=WIDTH, height=HEIGHT)
    row_bytes = (WIDTH * 3 + 8) * 3

    assert len(compare) == row_bytes * HEIGHT
    assert compare[:9] == b"\x10\x20\x30" * 3
    assert compare[WIDTH * 3 : WIDTH * 3 + 12] == b"\x20" * 12
    assert compare[WIDTH * 3 + 12 : WIDTH * 3 + 21] == b"\x40\x50\x60" * 3


def test_dump_divergence_writes_compare_png():
    ref_rgb = _rgb_frame(b"\x01\x02\x03")
    cand_rgb = _rgb_frame(b"\x04\x05\x06")
    ref = _sample(side="reference", frame_no=7, rgb=ref_rgb)
    cand = _sample(side="candidate", frame_no=7, rgb=cand_rgb)
    report = "FRAME VERIFY DIVERGENCE\nframe: 7"
    with tempfile.TemporaryDirectory() as tmp:
        dump_dir = Path(tmp)
        config = FrameVerifyConfig(dump_dir=dump_dir, preview_on_diff=False)

        dump_divergence(ref, cand, report, config)

        stem = dump_dir / "frame_00007_tandy"
        assert stem.with_name("frame_00007_tandy_ref.png").exists()
        assert stem.with_name("frame_00007_tandy_hook.png").exists()
        assert stem.with_name("frame_00007_tandy_diff.png").exists()
        assert stem.with_name("frame_00007_tandy_compare.png").exists()
        assert stem.with_name("frame_00007_tandy_compare.png").stat().st_size > 0


def test_run_frame_verifier_pumps_input_only_between_runtime_pairs():
    """Live input must not be delivered after the oracle already advanced.

    ``--verify-frame-preview`` runs the reference runtime first, then the hooked
    candidate runtime.  Pumping SDL input between those two passes can give the
    candidate a key event one frame earlier than the oracle, causing false frame
    divergences while playing.  The verifier should sample input only at pair
    boundaries, before both runtimes advance.
    """
    from types import SimpleNamespace

    from dos_re.frame_verify import make_frame_sample, run_frame_verifier

    boundary_key = (0x1010, 0x3354)

    class DummyCPU:
        def __init__(self) -> None:
            self.replacement_hooks = {boundary_key: lambda cpu: None}
            self.hook_names = {boundary_key: "dummy_present"}
            self.trace_enabled = False
            self.hook_verifier = None
            self.instruction_count = 0
            self.s = SimpleNamespace(cs=boundary_key[0], ip=boundary_key[1])

        def addr(self):
            return self.s.cs, self.s.ip

        def step(self) -> None:
            self.instruction_count += 1
            handler = self.replacement_hooks[boundary_key]
            name = self.hook_names[boundary_key]
            if self.hook_verifier is not None:
                self.hook_verifier(self, boundary_key, handler, name)
            else:
                handler(self)

    class DummyRuntime:
        def __init__(self) -> None:
            self.cpu = DummyCPU()
            self.input_ticks: list[int] = []

    ref = DummyRuntime()
    cand = DummyRuntime()
    pump_calls: list[int] = []

    def pump_inputs(ref_rt, cand_rt) -> None:
        tick = len(pump_calls) + 1
        pump_calls.append(tick)
        ref_rt.input_ticks.append(tick)
        cand_rt.input_ticks.append(tick)

    def sample(rt, side, frame_no, kind, hook, boundary_steps, start, recent, recent_sample_changes=()):
        # Both sides should see the same input ticks for the same verified pair.
        payload = bytes(rt.input_ticks)
        return make_frame_sample(
            rt=rt,
            side=side,
            frame_no=frame_no,
            kind=kind,
            hook=hook,
            boundary_steps=boundary_steps,
            start_count=start,
            recent_hooks=recent,
            raw=payload,
            rgb=payload,
            width=1,
            height=max(1, len(payload)),
            context="dummy",
        )

    result = run_frame_verifier(
        reference=ref,
        candidate=cand,
        config=FrameVerifyConfig(max_frames=3, frame_budget=8, source="both", log_every=0),
        boundary_hooks=((boundary_key, "present"),),
        sample_builder=sample,
        reference_env_hooks={boundary_key},
        pump_inputs=pump_inputs,
    )

    assert result == 0
    assert pump_calls == [1, 2, 3]
    assert ref.input_ticks == cand.input_ticks == [1, 2, 3]
