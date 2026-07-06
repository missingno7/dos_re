"""Smoke tests for the mechanisms promoted from the source ports in the second
extraction pass: gaps/transition signals, state-view descriptors, checkpoint
stepping, the frontier manifest, and the live-code signature guards."""
from __future__ import annotations

import pytest

from dos_re.checkpoints import checkpoints_for, run_to_next_checkpoint
from dos_re.cpu import CPU8086, CPUState
from dos_re.frontier import FrontierCategory, FrontierEntry, by_addr, frontier_summary_lines
from dos_re.gaps import HookTraceStats, HookVerifyStats, HybridGap, report
from dos_re.hooks import code_matches, self_disable_if_patched, signature_matches
from dos_re.memory import Memory
from dos_re.state_view import (
    ByteBackend,
    OverlayBackend,
    S16,
    StructArray,
    StructView,
    U8,
    U16,
    coerce_backend,
)


# ---- gaps ----------------------------------------------------------------------------------------

def test_hybrid_gap_signal_subclass_is_still_a_gap():
    class RespawnTransition(HybridGap):
        pass

    with pytest.raises(HybridGap):
        raise RespawnTransition("death bounce begins")


def test_report_tallies_and_raises_on_divergence():
    stats = HookVerifyStats()
    report(stats, None, False, "tile_row", None)
    report(stats, None, False, "tile_row", "AX mismatch")
    assert stats.verified == 1
    assert stats.diverged == [("tile_row", "AX mismatch")]
    with pytest.raises(AssertionError):
        report(stats, None, True, "tile_row", "AX mismatch")


def test_hook_trace_stats_window_summary():
    stats = HookTraceStats()
    stats.bump("render")
    stats.bump("render")
    since = stats.snapshot()
    stats.bump("player")
    assert stats.total() == 3
    assert stats.window_total(since) == 1
    assert stats.summary(since=since) == "player=1"


# ---- state_view ----------------------------------------------------------------------------------

DATA_BASE = 0x1A0F << 4


class Slot(StructView):
    x = U16(0)
    y = S16(2)
    life = U8(4)


class World(StructView):
    wind = U16(0x100)
    slots = StructArray(0x200, 6, 3, Slot)

    def __init__(self, source):
        super().__init__(coerce_backend(source, DATA_BASE), 0)


def test_byte_backend_view_roundtrip_writes_the_same_bytes():
    data = bytearray(0x100000)
    w = World(data)
    w.wind = 0x1234
    w.slots[1].x = 0xBEEF
    w.slots[1].y = -2
    w.slots[1].life = 7

    assert data[DATA_BASE + 0x100] == 0x34 and data[DATA_BASE + 0x101] == 0x12
    slot1 = DATA_BASE + 0x200 + 6
    assert data[slot1] == 0xEF and data[slot1 + 1] == 0xBE
    assert w.slots[1].y == -2          # signed round-trip
    assert w.slots[-2].x == 0xBEEF     # negative index wraps
    assert len(w.slots) == 3


def test_overlay_backend_accumulates_contract_without_touching_base():
    base = bytearray(0x10000)
    base[0x50] = 0xAA
    ov = OverlayBackend(lambda off: base[off])
    slot = Slot(ov, 0x50)

    assert slot.x == 0x00AA            # read-through
    slot.life = 9                      # write accumulates
    assert base[0x54] == 0             # base untouched
    assert ov.writes == {0x54: 9}
    assert slot.life == 9              # overlay sees its own write


def test_coerce_backend_passes_backends_through():
    b = ByteBackend(bytearray(16), 0)
    assert coerce_backend(b, 0xBEEF) is b


# ---- checkpoints ---------------------------------------------------------------------------------

CHECKPOINTS = {
    (0x1010, 0x0010): "frame: top of frame",
    (0x1010, 0x0020): "render: present",
}


class _HoppingCPU:
    """Steps through a fixed address sequence."""

    def __init__(self, seq):
        self._seq = list(seq)
        self._i = 0

    def addr(self):
        return self._seq[self._i]

    def step(self):
        self._i += 1


def test_run_to_next_checkpoint_stops_at_requested_kind():
    cpu = _HoppingCPU([(0x1010, 0x0010), (0x1010, 0x0015), (0x1010, 0x0020), (0x1010, 0x0025)])
    hit = run_to_next_checkpoint(cpu, CHECKPOINTS, kinds="render", max_steps=10)
    assert hit == (0x1010, 0x0020)

    with pytest.raises(KeyError):
        checkpoints_for(CHECKPOINTS, "no-such-kind")


# ---- frontier ------------------------------------------------------------------------------------

def test_frontier_manifest_summary_and_index():
    manifest = (
        FrontierEntry((0x1010, 0xD007), "main_frame_loop", "game_state",
                      FrontierCategory.FINAL_ORCHESTRATOR, "replaced"),
        FrontierEntry((0x32FF, 0x0052), "unpack_bootstrap", "bootstrap",
                      FrontierCategory.DO_NOT_HOOK_BOOTSTRAP, "classified-do-not-hook"),
    )
    idx = by_addr(manifest)
    assert idx[(0x1010, 0xD007)].category is FrontierCategory.FINAL_ORCHESTRATOR
    lines = frontier_summary_lines(manifest)
    assert lines[0].startswith("== explicit cold-start frontier manifest")
    assert any("32FF:0052" in line and "do-not-hook-bootstrap" in line for line in lines)


# ---- signature guards ----------------------------------------------------------------------------

def test_signature_guard_accepts_matching_and_zero_bytes_and_rejects_patched():
    mem = Memory()
    cpu = CPU8086(mem, CPUState(cs=0x1000))
    sig = bytes.fromhex("40 c3")

    # all-zero window (synthetic fixture) stays enabled
    assert self_disable_if_patched(cpu, 0x0000, sig, "inc_ret") is False

    mem.load(0x1000, 0, sig)
    assert code_matches(cpu, 0x0000, sig)
    assert signature_matches(bytes.fromhex("40 c3 90"), sig)
    assert self_disable_if_patched(cpu, 0x0000, sig, "inc_ret") is False

    mem.load(0x1000, 0, bytes.fromhex("48 c3"))  # runtime-patched: DEC AX instead of INC AX
    with pytest.raises(RuntimeError, match="runtime-patched"):
        self_disable_if_patched(cpu, 0x0000, sig, "inc_ret")


# ---- fail-loud hardware gaps ----------------------------------------------------------------------

def test_unmodeled_ega_write_modes_fail_loud_instead_of_acting_like_mode_0():
    from dos_re.memory import UnsupportedEgaWriteMode

    mem = Memory()
    mem.ega_planar = True
    mem.ega_map_mask = 0x0F

    mem.ega_write_mode = 1          # modeled: latch copy, must not raise
    mem.wb(0xA000, 0x0000, 0x12)

    for mode in (2, 3):
        mem.ega_write_mode = mode
        with pytest.raises(UnsupportedEgaWriteMode):
            mem.wb(0xA000, 0x0000, 0x12)
