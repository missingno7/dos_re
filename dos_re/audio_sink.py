"""Observer-only live viewer audio: AdLib via Nuked-OPL3 + PC-speaker square wave.

The VM exposes both sources as callbacks (``dos.set_adlib_callback`` /
``dos.set_speaker_callback``); this sink renders them into one pygame mixer
channel with a small jitter lead.  It never writes game state, so demos replay
identically with audio on or off — it is safe to wire into any port's viewer.

Part of the FRONTEND RING (see tools/lint.py): needs numpy + pygame.  The
OPL3 backend comes from ``load_opl3`` — opl3_fast by default (opt-in external pynuked_opl3),
else the numpy approximate ``dos_re.opl3_fast`` (the everyday default; the
bit-exact core is dormant in graveyard/, never selected at runtime).

``dos_re.player`` constructs this automatically for ``--audio adlib``; ports
with a different audio architecture (e.g. digital Sound Blaster DMA games)
override ``GameFrontend.create_audio_sink`` instead.

Origin: promoted verbatim from ancient_port's scripts/play.py AudioSink once
the play-runner unification made it the second consumer.
"""
from __future__ import annotations


def load_opl3():
    """Return ``(OPL3_class, backend_label)`` — the runtime OPL3 backend.

    The DEFAULT (and only bundled) backend is ``opl3-fast`` —
    ``dos_re.opl3_fast``, the numpy APPROXIMATE synth (~50x real-time on
    CPython; perceptually indistinguishable from the exact chip on real game
    music in blind A/B — calibration + evidence in its module docstring /
    tests).  It is build-free and dependency-free beyond numpy.

    Projects that want a bit-exact OPL3 can OPT IN to the external
    ``pynuked_opl3`` package (https://github.com/missingno7/pynuked_opl3 — a
    cffi build of Nuked-OPL3; build once with ``python -m
    pynuked_opl3._ffi_build``) and select it with
    ``DOSRE_OPL3_BACKEND=nuked``.  It is NOT a dos_re submodule and dos_re
    never requires it; when requested but not importable/built, this falls
    back to ``opl3-fast``.

    The bit-exact pure-Python core was retired from the runtime (too slow at
    ~1x real-time) and now lives in ``graveyard/opl3_exact.py`` as the
    calibration/golden reference only — it is never selected here.
    """
    import os
    pref = os.environ.get("DOSRE_OPL3_BACKEND", "").strip().lower()
    if pref in ("c", "nuked", "nuked-opl3-c"):
        try:
            from pynuked_opl3 import OPL3 as _COPL3

            _COPL3()  # probe: the package imports even when its extension is unbuilt
            return _COPL3, "nuked-opl3-c"
        except Exception:  # noqa: BLE001 — optional accuracy package absent/unbuilt
            pass
    from dos_re.opl3_fast import OPL3Fast

    return OPL3Fast, "opl3-fast"


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
        # TWO different sizes, deliberately.  They used to be one value
        # (rate // present_hz), which silently coupled the audio BUFFER DEPTH
        # to the present rate: a port presenting at 73 Hz got a 13.7ms chunk
        # and, since pygame's Channel holds only playing + ONE queued sound,
        # a 27ms live buffer -- any frame that overran drained it, the channel
        # went idle, and the sink re-accumulated its lead: an audible stutter
        # plus a re-buffer.  Raising a port's present_hz must not shrink its
        # audio safety margin.
        #
        # _gen   how much audio ONE pump() must produce to stay real-time.
        #        This alone is what present_hz determines.
        # _chunk the queue granularity = the live buffer depth (2 chunks are
        #        in the channel: one playing, one queued).  Fixed in TIME.
        self._gen = self._rate / float(max(1, present_hz))
        self._gen_frac = 0.0
        self._chunk = max(256, int(self._rate * 0.040))   # 40ms -> ~80ms live
        self._lead = int(self._rate * 0.10)
        self._buf = np.zeros((0, self._channels), dtype=np.int16)
        self._started = False
        if pygame.mixer.get_num_channels() < 2:
            pygame.mixer.set_num_channels(2)
        self._channel = pygame.mixer.Channel(1)

        # OPL3 backend: opl3_fast by default; external pynuked_opl3 by opt-in (see load_opl3).
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
        # ONE frame's worth, carrying the fraction so a present_hz that does
        # not divide the sample rate (73 -> 604.1 samples) cannot drift.
        want = self._gen + self._gen_frac
        n = int(want)
        self._gen_frac = want - n
        if n <= 0:
            return
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
        # Bound latency: if the VM ran faster than real time (a heavy-then-fast
        # burst, or a pacing mismatch) the producer can outrun the mixer and the
        # backlog grows to seconds of delay.  Cap the queued audio at ~4 chunks
        # (~160ms: the lead plus slack) by dropping the OLDEST samples — audio
        # stays live at the cost of a tiny, one-off skip instead of an
        # ever-growing delay.  Never trims below one chunk (avoids underrun).
        _max_backlog = max(self._chunk * 4, self._lead + self._chunk)
        if len(self._buf) > _max_backlog:
            self._buf = self._buf[-_max_backlog:]
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
