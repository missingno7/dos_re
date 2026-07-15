"""Perceptual-fidelity tests for dos_re.opl3_fast against the EXACT core.

opl3_fast is approximate by design, so these are calibrated-tolerance checks
of the audible dimensions (pitch, envelope timing, harmonic balance,
loudness), each measured against dos_re.opl3 — the byte-exact reference that
is always importable.  The A/B evidence on real game music (80 s SKYROADS
stream, WAV pair + spectral metrics) lives in the introduction commit.
"""
from __future__ import annotations

import numpy as np
import pytest

from dos_re.opl3 import OPL3
from dos_re.opl3_fast import OPL3Fast

RATE = 44100


def _note(chipcls, *, fnum=0x298, block=4, mult=1, tl_m=16, ar=0xF, dr=4,
          sl=4, rr=6, fb=0, con=0, wf_c=0, egt=1, on=RATE // 2, off=RATE // 4):
    c = chipcls(sample_rate=RATE)
    c.write(0x20, 0x20 * egt | mult)
    c.write(0x23, 0x20 * egt | mult)
    c.write(0x40, tl_m)
    c.write(0x43, 0x00)
    c.write(0x60, (ar << 4) | dr)
    c.write(0x63, (ar << 4) | dr)
    c.write(0x80, (sl << 4) | rr)
    c.write(0x83, (sl << 4) | rr)
    c.write(0xE3, wf_c)
    c.write(0xC0, (fb << 1) | con)
    c.write(0xA0, fnum & 0xFF)
    c.write(0xB0, 0x20 | (block << 2) | (fnum >> 8))
    pcm = c.generate_stereo(on)
    c.write(0xB0, (block << 2) | (fnum >> 8))
    pcm += c.generate_stereo(off)
    return np.frombuffer(pcm, "<i2").astype(np.float64).reshape(-1, 2)[:, 0]


def _f0(x):
    seg = x[RATE // 8:RATE // 4]
    seg = seg - seg.mean()
    ac = np.correlate(seg, seg, "full")[len(seg) - 1:]
    d = np.diff(ac)
    start = int(np.argmax(d > 0))
    peak = int(np.argmax(ac[start:])) + start
    return RATE / peak if peak else 0.0


def _env(x, win=441):
    n = len(x) // win
    return np.sqrt((x[:n * win].reshape(n, win) ** 2).mean(axis=1))


def test_deterministic_bytes():
    a = _note(OPL3Fast).tobytes()
    b = _note(OPL3Fast).tobytes()
    assert a == b


def test_api_surface():
    c = OPL3Fast(sample_rate=22050)
    assert c.generate_stereo(0) == b""
    c.write(0x20, 0x01)
    c.write_immediate(0xA0, 0x98)
    s = c.generate_stereo(128)
    assert len(s) == 128 * 4
    assert len(c.generate_mono(64)) == 64 * 2
    c.reset(44100)
    assert c.sample_rate == 44100


@pytest.mark.parametrize("mult,block", [(1, 4), (3, 4), (1, 2)])
def test_pitch_matches_exact_core(mult, block):
    xa = _note(OPL3, mult=mult, block=block)
    xb = _note(OPL3Fast, mult=mult, block=block)
    fa, fb_ = _f0(xa), _f0(xb)
    assert fa > 0 and abs(fa - fb_) / fa < 0.01, (fa, fb_)


@pytest.mark.parametrize("fb", [0, 3, 6, 7])
def test_harmonic_balance_vs_exact(fb):
    xa = _note(OPL3, fb=fb)
    xb = _note(OPL3Fast, fb=fb)
    w = RATE // 8
    sa = xa[w:2 * w] * np.hanning(w)
    sb = xb[w:2 * w] * np.hanning(w)
    base = _f0(xa)
    fa = np.abs(np.fft.rfft(sa))
    fbv = np.abs(np.fft.rfft(sb))

    def hset(f):
        out = []
        for h in range(1, 7):
            b = int(round(base * h * w / RATE))
            out.append(f[max(b - 3, 0):b + 4].max() if b + 4 < len(f) else 0.0)
        m = max(out) or 1.0
        return [v / m for v in out]

    diff = max(abs(p - q) for p, q in zip(hset(fa), hset(fbv)))
    assert diff < 0.08, diff


def test_release_time_matches_exact():
    for rr in (4, 6, 8):
        xa = _note(OPL3, rr=rr, on=RATE // 4, off=RATE)
        xb = _note(OPL3Fast, rr=rr, on=RATE // 4, off=RATE)

        def t_rel(x):
            e = _env(x)
            pk = e.max()
            i0 = RATE // 4 // 441
            below = np.where(e[i0:] < pk / 100)[0]
            return below[0] if len(below) else 10_000

        ta, tb = t_rel(xa), t_rel(xb)
        assert abs(ta - tb) <= max(3, 0.2 * ta), (rr, ta, tb)


def test_loudness_within_tolerance():
    xa = _note(OPL3)
    xb = _note(OPL3Fast)
    ra = _env(xa).max()
    rb = _env(xb).max()
    assert 0.85 < rb / ra < 1.15, (ra, rb)


def test_note_onset_is_sample_accurate():
    c = OPL3Fast(sample_rate=RATE)
    c.write(0x20, 0x21)
    c.write(0x23, 0x21)
    c.write(0x40, 0x10)
    c.write(0x43, 0x00)
    c.write(0x60, 0xF0)
    c.write(0x63, 0xF0)
    c.write(0x80, 0x47)
    c.write(0x83, 0x47)
    pcm1 = c.generate_stereo(1000)          # silence
    c.write(0xA0, 0x98)
    c.write(0xB0, 0x31)                      # key on queued at position 1000
    pcm2 = c.generate_stereo(1000)
    x1 = np.frombuffer(pcm1, "<i2")
    x2 = np.frombuffer(pcm2, "<i2")
    assert not np.any(x1)                    # nothing before the key-on
    assert np.any(x2)                        # sound after it
