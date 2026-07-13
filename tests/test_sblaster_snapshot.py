"""Regression: the Sound Blaster / DMA state survives a snapshot round-trip.

A save taken mid-playback must restore the DSP/DMA programming and re-arm a block
IRQ, otherwise the resumed game (already past detection, waiting on the next
block-complete IRQ) streams nothing. Pure device-level test (no VM); the in-VM
end-to-end resume is exercised by pre2/probes/capture_sb.py + manual play.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dos_re.sblaster import DmaChannel, SoundBlaster  # noqa: E402


def test_dma_channel_round_trip():
    ch = DmaChannel(page=1, base_addr=0xB1F5, base_count=167, cur_addr=0xB1F5,
                    cur_count=20, mode=0x49, masked=False, _flipflop_high=True)
    restored = DmaChannel()
    restored.restore_state(ch.snapshot_state())
    assert restored.snapshot_state() == ch.snapshot_state()
    assert restored.physical() == ch.physical()


def _programmed_sb() -> SoundBlaster:
    """An SB in the state the driver leaves it mid-playback (8403 Hz, ch1 active)."""
    sb = SoundBlaster()
    sb.speaker_on = True
    sb.sample_rate = 8403
    sb.time_constant = 0x89
    sb.dma_active = True
    sb.auto_init = False
    sb.channels[1].restore_state(
        {"page": 1, "base_addr": 0xB1F5, "base_count": 167, "cur_addr": 0xB1F5,
         "cur_count": 167, "mode": 0x49, "masked": False, "flipflop_high": False}
    )
    return sb


def test_sound_blaster_round_trip_preserves_playback_state():
    sb = _programmed_sb()
    state = sb.snapshot_state()
    fresh = SoundBlaster()
    fresh.restore_state(state)
    assert fresh.sample_rate == 8403
    assert fresh.speaker_on is True
    assert fresh.dma_active is True
    assert fresh.channels[1].physical() == sb.channels[1].physical()
    assert fresh.snapshot_state() == state


def test_rearm_raises_block_irq_when_mid_stream():
    fired = []
    sb = _programmed_sb()
    sb.raise_irq = fired.append
    sb.rearm_after_restore()
    assert fired == [sb.irq]  # one block-complete IRQ raised to kick the refill ISR
    assert sb.irq_line is True


def test_rearm_is_noop_when_not_streaming():
    fired = []
    sb = SoundBlaster()           # dma_active stays False
    sb.raise_irq = fired.append
    sb.rearm_after_restore()
    assert fired == []


def test_rearm_resumes_a_pending_block_faithfully_without_firing():
    """A block PENDING at save must resume at the same remaining offset on the
    resuming clock — NOT fire at load.  Force-firing made a recorded demo's
    block-IRQ land at load instead of its due instant, so the replay diverged
    ~one frame in.  Regression for that fix."""
    fired = []
    sb = _programmed_sb()
    sb.clock = lambda: 10.0        # save-time clock
    sb.raise_irq = fired.append
    sb._block_pending = True
    sb._block_due = 10.05          # 0.05 ahead of now
    state = sb.snapshot_state()    # stores block_remaining == 0.05

    fresh = _programmed_sb()
    now = [3.0]                    # resume on a DIFFERENT clock origin
    fresh.clock = lambda: now[0]
    fresh.raise_irq = fired.append
    fresh.restore_state(state)
    fresh.rearm_after_restore()

    assert fired == []                                 # did NOT fire at load
    assert fresh._block_pending is True
    assert abs(fresh._block_due - 3.05) < 1e-6         # re-armed 0.05 ahead of new now
    now[0] = 3.06                                       # advance clearly past due
    fresh.service()
    assert fired == [fresh.irq]                        # fires at the due instant
