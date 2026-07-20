"""The low-latency Sound Blaster sink must survive a huge PCM burst.

A long stall (e.g. the game paused, so the frame clock never advanced and one
present ran a multi-million-instruction burst) hands ``pump`` far more PCM than
the fixed ring holds.  It must bound the buffered latency instead of overflowing
the ring with an unbroadcastable wraparound write (the crash a paused resume hit:
"could not broadcast input array from shape (82397,) into shape (22050,)").
"""
from __future__ import annotations

import threading

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("sounddevice")

from dos_re.pm_backend import _SoundDeviceSink  # noqa: E402


class _FakeSB:
    def __init__(self, nbytes: int, rate: int = 5128):
        self.sample_rate = rate
        self.pcm_out = bytearray(bytes(range(256)) * (nbytes // 256 + 1))


def _bare_sink(sb):
    """A sink with its ring set up but no real PortAudio stream opened."""
    s = object.__new__(_SoundDeviceSink)
    s.sb = sb
    s.ring = np.zeros(_SoundDeviceSink.RING, dtype=np.int16)
    s.w = s.r = 0
    s.primed = False
    s.pos = 0.0
    s.inbuf = bytearray()
    s._lock = threading.Lock()
    return s


def test_pump_survives_burst_larger_than_ring():
    s = _bare_sink(_FakeSB(200_000))     # resamples to far more than RING
    s.pump()                             # must not raise (previously ValueError)
    used = (s.w - s.r) % _SoundDeviceSink.RING
    assert 0 <= s.w < _SoundDeviceSink.RING
    assert used <= _SoundDeviceSink.MAX_FILL   # latency bounded, ring intact


def test_pump_normal_block_is_not_dropped():
    # A modest block (~one DMA half) fits well under the cap and is enqueued.
    s = _bare_sink(_FakeSB(640))
    s.pump()
    used = (s.w - s.r) % _SoundDeviceSink.RING
    assert 0 < used <= _SoundDeviceSink.MAX_FILL
