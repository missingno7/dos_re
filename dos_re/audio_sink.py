"""Observer-only live viewer audio: AdLib via Nuked-OPL3 + PC-speaker square wave.

The VM exposes both sources as callbacks (``dos.set_adlib_callback`` /
``dos.set_speaker_callback``); this sink renders them into one pygame mixer
channel with a small jitter lead.  It never writes game state, so demos replay
identically with audio on or off — it is safe to wire into any port's viewer.

Part of the FRONTEND RING (see tools/lint.py): needs numpy + pygame.  The
OPL3 backend is the canonical pure-Python core ``dos_re.opl3`` (always
present, no build step; byte-exact against the retired upstream C reference
— tests/test_opl3.py).

``dos_re.player`` constructs this automatically for ``--audio adlib``; ports
with a different audio architecture (e.g. digital Sound Blaster DMA games)
override ``GameFrontend.create_audio_sink`` instead.

Origin: promoted verbatim from ancient_port's scripts/play.py AudioSink once
the play-runner unification made it the second consumer.
"""
from __future__ import annotations

import sys


def load_opl3():
    """Return ``(OPL3_class, backend_label)`` — the per-interpreter OPL3 choice.

    - ``nuked-opl3-c``  — the vendored pynuked_opl3 cffi build when compiled
      (bit-exact, native speed); build once: python -m pynuked_opl3._ffi_build
    - ``opl3-fast``     — on CPython otherwise: dos_re.opl3_fast, the numpy
      APPROXIMATE synth (perceptually matched, ~50x real-time on game music;
      calibration + A/B evidence in its module docstring / tests)
    - ``nuked-opl3-py`` — on PyPy otherwise: dos_re.opl3, the bit-exact
      pure-Python core (30-60x real-time there; numpy is slow under PyPy)

    The exact cores remain the reference implementations; opl3_fast is the
    everyday CPython playback backend.
    """
    try:
        from pynuked_opl3 import OPL3 as _COPL3

        _COPL3()  # probe: the package imports even when its extension is unbuilt
        return _COPL3, "nuked-opl3-c"
    except Exception:  # noqa: BLE001 — no compiled backend
        pass
    if sys.implementation.name == "cpython":
        from dos_re.opl3_fast import OPL3Fast

        return OPL3Fast, "opl3-fast"
    from dos_re.opl3 import OPL3 as _PyOPL3

    return _PyOPL3, "nuked-opl3-py"


class AdlibSpeakerSink:
    """Render the VM's AdLib register stream + PC-speaker state to the host."""

    def __init__(self, pygame, rt, present_hz: int) -> None:
        import numpy as np

        self._np = np
        self._pygame = pygame
        self.available = False
        self.opl_label = "off"
        if not pygame.mixer.get_init():
            try:
                pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
            except Exception as exc:  # noqa: BLE001 — headless/dummy audio hosts
                print(f"[audio] mixer unavailable ({exc}); audio off")
                return
        rate, _size, channels = pygame.mixer.get_init()
        self._rate, self._channels = int(rate), int(channels)
        self._chunk = max(256, self._rate // max(1, present_hz))
        self._lead = int(self._rate * 0.10)
        self._buf = np.zeros((0, self._channels), dtype=np.int16)
        self._started = False
        if pygame.mixer.get_num_channels() < 2:
            pygame.mixer.set_num_channels(2)
        self._channel = pygame.mixer.Channel(1)

        # Backend per interpreter — see load_opl3 (compiled exact when built,
        # opl3_fast on CPython, exact pure-Python on PyPy).
        opl_cls, self.opl_label = load_opl3()
        self._opl = opl_cls(sample_rate=self._rate)
        # PC speaker square-wave state (phase-continuous across chunks).
        self._spk_on = False
        self._spk_freq = 0.0
        self._spk_phase = 0.0
        rt.dos.set_adlib_callback(self._on_adlib, emit_current=True)
        rt.dos.set_speaker_callback(self._on_speaker, emit_current=True)
        self.available = True

    def _on_adlib(self, reg: int, value: int) -> None:
        if self._opl is not None:
            self._opl.write(reg, value)

    def _on_speaker(self, on: bool, freq: float) -> None:
        self._spk_on, self._spk_freq = bool(on), float(freq or 0.0)

    def _speaker_chunk(self, n: int):
        np = self._np
        if not (self._spk_on and self._spk_freq > 0):
            return None
        step = self._spk_freq / self._rate
        phases = self._spk_phase + np.arange(n) * step
        self._spk_phase = float(phases[-1] + step) % 1.0
        return np.where((phases % 1.0) < 0.5, 5000, -5000).astype(np.int16)

    def pump(self) -> None:
        """Feed one presented frame's worth of audio; call once per viewer frame."""
        if not self.available:
            return
        np = self._np
        n = self._chunk
        if self._opl is not None:
            pcm = np.frombuffer(self._opl.generate_stereo(n), dtype="<i2").reshape(-1, 2)
            out = pcm.astype(np.int32)
        else:
            out = np.zeros((n, 2), dtype=np.int32)
        spk = self._speaker_chunk(n)
        if spk is not None:
            out += spk[:, None]
        out = np.clip(out, -32768, 32767).astype(np.int16)
        if self._channels == 1:
            out = out[:, :1]
        self._buf = np.concatenate([self._buf, out])
        if not self._started:
            if len(self._buf) >= self._lead:
                self._channel.play(self._next_sound())
                self._started = True
            return
        if not self._channel.get_busy():
            self._started = False
            return
        if self._channel.get_queue() is None and len(self._buf) >= self._chunk:
            self._channel.queue(self._next_sound())

    def _next_sound(self):
        chunk, self._buf = self._buf[:self._chunk], self._buf[self._chunk:]
        arr = chunk if self._channels > 1 else chunk.reshape(-1)
        return self._pygame.sndarray.make_sound(self._np.ascontiguousarray(arr))
