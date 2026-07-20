"""Protected-mode backend adapter for the canonical player lifecycle.

The protected-mode counterpart of :mod:`dos_re.player`, reduced to what the
PM runtime supports today: a pygame window presenting the VGA screen
(chained 13h or unchained Mode X via :func:`~dos_re.dos4gw.render_pm_frame`),
keyboard delivered as set-1 scancodes through the emulated 8042 KBC
(extended-key E0 pairs included), mouse through the INT 33h driver state,
pacing by wall-clock vsync (``dos.time_source`` drives the program's own
3DAh retrace waits at ~70 Hz real time), F10 screenshot / F12 snapshot, F11
replay record, ``--play-replay`` deterministic replay, and ``--snapshot`` resume.

Audio: the Sound Blaster PCM plays through a low-latency callback ring buffer
(``sounddevice``/PortAudio) when available, falling back to a chunked pygame
mixer sink.  ``pip install sounddevice`` for smooth playback.

A recording is one self-contained ReplayArtifact: F11 captures a complete
continuation base plus normalized events keyed to the game's own frame counter
(an adapter-supplied
``frame_tick_addr`` hit once per frame; see ``pm_replay_input``). ``--play-replay
<dir>`` restores that base and re-injects input at the same stable boundaries.
The user names only the artifact directory.

FRONTEND RING module: pygame imports stay lazy so headless use (and
``import dos_re``) never require it.

A game port constructs :class:`PMFrontend` in its single
``scripts/play.py`` and passes it to :func:`dos_re.player.main`.

It contains no parser, profile resolver, or entrypoint of its own.
"""
from __future__ import annotations

import hashlib
import threading as _threading
import time
from pathlib import Path

from .dos4gw import DosInputExhausted, render_pm_frame
from .execution import (
    ExecutionPlan,
    ImplementationOrigin,
    execution_composition_digest,
)
from .frame_verify import write_rgb_png
from .replay_input import MOUSE_CHANNEL, mouse_payload
from .pm_snapshot import apply_pm_continuation, capture_pm_continuation
from .player import GameFrontend
from .replay import (
    ReplayArtifact,
    ReplayExecutionIdentity,
    ReplayPoint,
    ReplayRecording,
)

try:                       # numpy is a first-class dep; audio resampling needs it
    import numpy as _np
except ImportError:        # pragma: no cover
    _np = None


def _make_audio_sink(sb):
    """The low-latency sounddevice sink when available (+ numpy); else the
    pygame chunk sink."""
    if sb is not None and _np is not None:
        try:
            import sounddevice  # noqa: F401
            return _SoundDeviceSink(sb)
        except Exception:  # noqa: BLE001 — no PortAudio device / not installed
            pass
    return _PcmSink(sb)


# pygame key name -> set-1 scancode (make code; break = make | 0x80).
# Extended keys (arrows...) are (0xE0, code) tuples.
SCANCODES = {
    "escape": 0x01, "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06,
    "6": 0x07, "7": 0x08, "8": 0x09, "9": 0x0A, "0": 0x0B, "-": 0x0C,
    "=": 0x0D, "backspace": 0x0E, "tab": 0x0F,
    "q": 0x10, "w": 0x11, "e": 0x12, "r": 0x13, "t": 0x14, "y": 0x15,
    "u": 0x16, "i": 0x17, "o": 0x18, "p": 0x19, "[": 0x1A, "]": 0x1B,
    "return": 0x1C, "left ctrl": 0x1D,
    "a": 0x1E, "s": 0x1F, "d": 0x20, "f": 0x21, "g": 0x22, "h": 0x23,
    "j": 0x24, "k": 0x25, "l": 0x26, ";": 0x27, "'": 0x28, "`": 0x29,
    "left shift": 0x2A, "\\": 0x2B,
    "z": 0x2C, "x": 0x2D, "c": 0x2E, "v": 0x2F, "b": 0x30, "n": 0x31,
    "m": 0x32, ",": 0x33, ".": 0x34, "/": 0x35, "right shift": 0x36,
    "left alt": 0x38, "space": 0x39, "caps lock": 0x3A,
    "f1": 0x3B, "f2": 0x3C, "f3": 0x3D, "f4": 0x3E, "f5": 0x3F,
    "f6": 0x40, "f7": 0x41, "f8": 0x42, "f9": 0x43,
    "up": (0xE0, 0x48), "down": (0xE0, 0x50),
    "left": (0xE0, 0x4B), "right": (0xE0, 0x4D),
}


def _file_identity(path: str | Path) -> str:
    p = Path(path)
    payload = p.read_bytes() if p.is_file() else str(path).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _pm_profile(
    exe: str | Path, rt, plan: ExecutionPlan,
) -> ReplayExecutionIdentity:
    implementation = hashlib.sha256()
    implementation.update(execution_composition_digest(plan).encode("ascii"))
    runtime = hashlib.sha256()
    root = Path(__file__).parent
    for name in ("cpu386.py", "dos4gw.py", "runtime.py", "pm_snapshot.py", "replay.py"):
        runtime.update(name.encode("utf-8"))
        runtime.update((root / name).read_bytes())
    only_interpreted = bool(plan.implementations) and all(
        implementation.origin is ImplementationOrigin.INTERPRETED
        for implementation in plan.implementations
    )
    role = (
        "oracle"
        if only_interpreted and not plan.configuration.selected_overrides
        else "candidate"
    )
    implementation_digest = implementation.hexdigest()
    key = implementation_digest[:12]
    return ReplayExecutionIdentity(
        profile_id=f"protected-mode-{role}-{key}",
        role=role,
        implementation=implementation_digest,
        image=_file_identity(exe),
        runtime=runtime.hexdigest(),
        devices="dos-re-protected-mode-devices-v1",
        continuation_schema="dos-re-pm-continuation-v1",
        projection_schema="dos-re-complete-machine-v1",
    )


def send_key(dos, name: str, make: bool) -> None:
    """Deliver one pygame-named key as KBC scancodes (make or break)."""
    sc = SCANCODES.get(name)
    if sc is None:
        return
    if isinstance(sc, tuple):
        dos.press_scancode(sc[0])
        dos.press_scancode(sc[1] | (0x00 if make else 0x80))
    else:
        dos.press_scancode(sc | (0x00 if make else 0x80))


class _SoundDeviceSink:
    """Callback-based low-latency Sound Blaster playback via sounddevice.

    A PortAudio callback drains a ring buffer on its OWN audio thread at
    exactly the output rate, so playback is smooth and low-latency regardless
    of the frame loop's pacing — unlike chunked pygame Sound/queue streaming
    (which jittered/underran, the lag and pypy cut-off).  ``pump`` resamples
    the SB's 8-bit PCM into the ring, phase-continuous; the callback pulls from
    it, outputting silence on underrun and priming a small cushion at the
    start.  numpy-only (both CPython and pypy have it); ``run_viewer`` falls
    back to :class:`_PcmSink` if sounddevice is unavailable."""

    OUT_RATE = 22050
    RING = OUT_RATE                 # 1 s ring
    CUSHION = 1764                  # samples buffered before draining (~80 ms)
    MAX_FILL = 4410                 # cap buffered latency (~200 ms); drop oldest beyond

    def __init__(self, sb):
        import sounddevice as sd
        self.sb = sb
        self.ring = _np.zeros(self.RING, dtype=_np.int16)
        self.w = 0
        self.r = 0
        self.primed = False
        self.pos = 0.0              # resample phase into inbuf
        self.inbuf = bytearray()
        self._lock = _threading.Lock()
        self.stream = sd.OutputStream(samplerate=self.OUT_RATE, channels=1,
                                      dtype="int16", callback=self._callback)
        self.stream.start()

    def _callback(self, outdata, frames, time_info, status):  # PortAudio thread
        with self._lock:
            avail = (self.w - self.r) % self.RING
            if not self.primed:
                if avail < self.CUSHION:
                    outdata[:] = 0
                    return
                self.primed = True
            n = frames if frames < avail else avail
            r = self.r
            if r + n <= self.RING:
                outdata[:n, 0] = self.ring[r:r + n]
            else:
                k = self.RING - r
                outdata[:k, 0] = self.ring[r:]
                outdata[k:n, 0] = self.ring[:n - k]
            self.r = (r + n) % self.RING
            if n < avail:
                pass
            else:
                self.primed = False        # fully drained -> re-cushion
        if n < frames:
            outdata[n:] = 0

    def pump(self):
        sb = self.sb
        if sb is None or not sb.sample_rate:
            return
        if sb.pcm_out:
            self.inbuf += sb.pcm_out
            del sb.pcm_out[:]
        if len(self.inbuf) <= 1:
            return
        ratio = sb.sample_rate / self.OUT_RATE
        inb = _np.frombuffer(bytes(self.inbuf), dtype=_np.uint8).astype(_np.float32) - 128.0
        n = int((len(inb) - 1 - self.pos) / ratio)
        if n <= 0:
            return
        idx = self.pos + _np.arange(n) * ratio
        out16 = (_np.interp(idx, _np.arange(len(inb)), inb) * 256.0).clip(
            -32768, 32767).astype(_np.int16)
        new_pos = self.pos + n * ratio
        consumed = int(new_pos)
        self.pos = new_pos - consumed
        del self.inbuf[:consumed]
        with self._lock:
            m = len(out16)
            # A long stall (e.g. the game paused, so the frame clock never
            # advanced and one present ran a huge instruction burst) can
            # resample to far more than the ring holds.  Keep only the newest
            # MAX_FILL samples — the rest would be dropped by the latency bound
            # below anyway — so the wraparound write can never overflow the ring.
            if m > self.MAX_FILL:
                out16 = out16[-self.MAX_FILL:]
                m = self.MAX_FILL
            used = (self.w - self.r) % self.RING
            # Bound latency: if the ring is already full of buffered audio, drop
            # the oldest so playback stays in sync instead of lagging.
            if used + m > self.MAX_FILL:
                self.r = (self.r + (used + m - self.MAX_FILL)) % self.RING
            w = self.w
            if w + m <= self.RING:
                self.ring[w:w + m] = out16
            else:
                k = self.RING - w
                self.ring[w:] = out16[:k]
                self.ring[:m - k] = out16[k:]
            self.w = (w + m) % self.RING

    def close(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:  # noqa: BLE001
            pass


class _PcmSink:
    """Sound Blaster PCM consumer: drains ``sb.pcm_out`` (8-bit unsigned mono
    at the DSP-programmed rate), RESAMPLES to a standard 22050 Hz / 16-bit
    signed stream, and feeds a pygame mixer channel gap-free.

    Resampling to a standard rate + 16-bit is what makes it portable: the raw
    8-bit-unsigned-at-an-odd-rate (e.g. 7936 Hz) mixer format is silently
    unsupported by some SDL backends (no audio under pypy's build).  Playback
    problems this also solves: a pygame Channel holds only ONE queued sound so
    per-present submits would drop audio, and the DMA blocks arrive with
    frame-granularity jitter — so it buffers a cushion, then feeds fixed chunks
    into whatever play/queue slot is free (never dropping), decoupled from
    arrival jitter."""

    OUT_RATE = 22050               # standard rate every SDL backend supports
    MIXER_BUF = 2048               # SDL callback buffer (~93 ms) — slack against underruns
    CHUNK = 1024                   # output samples per Sound (~46 ms)
    CUSHION = 3072                 # output samples buffered before start (~140 ms)
    MAX_LAG = 9000                 # bound buffered latency (~400 ms); drop oldest beyond

    def __init__(self, sb):
        self.sb = sb
        self.channel = None
        self.inbuf = bytearray()   # raw 8-bit unsigned input
        self.out = bytearray()     # resampled 16-bit signed output
        self.pos = 0.0             # fractional read position into inbuf (phase-continuous)
        self.playing = False

    def _resample(self, in_rate):
        """Append inbuf (8-bit unsigned @ in_rate) to out (16-bit signed @
        OUT_RATE), phase-continuous.  numpy fast path, scalar fallback so it
        works where numpy is absent (e.g. a bare pypy)."""
        ratio = in_rate / self.OUT_RATE
        if _np is not None:
            inb = _np.frombuffer(bytes(self.inbuf), dtype=_np.uint8).astype(_np.float32) - 128.0
            n = int((len(inb) - 1 - self.pos) / ratio)
            if n <= 0:
                return
            idx = self.pos + _np.arange(n) * ratio
            samp = _np.interp(idx, _np.arange(len(inb)), inb)
            self.out += (samp * 256.0).clip(-32768, 32767).astype("<i2").tobytes()
            new_pos = self.pos + n * ratio
        else:
            import struct
            inb = self.inbuf
            pos = self.pos
            ob = bytearray()
            end = len(inb) - 1
            while pos < end:
                i = int(pos)
                frac = pos - i
                s = (inb[i] * (1.0 - frac) + inb[i + 1] * frac) - 128.0
                val = int(s * 256.0)
                ob += struct.pack("<h", -32768 if val < -32768 else 32767 if val > 32767 else val)
                pos += ratio
            self.out += ob
            new_pos = pos
        consumed = int(new_pos)
        self.pos = new_pos - consumed
        del self.inbuf[:consumed]

    def pump(self):
        import pygame
        sb = self.sb
        if sb is None or not sb.sample_rate:
            return
        if sb.pcm_out:
            self.inbuf += sb.pcm_out
            del sb.pcm_out[:]
        if self.channel is None:
            pygame.mixer.quit()
            pygame.mixer.init(frequency=self.OUT_RATE, size=-16, channels=1, buffer=self.MIXER_BUF)
            self.channel = pygame.mixer.Channel(0)
            self.playing = False
        if len(self.inbuf) > 1:
            self._resample(sb.sample_rate)
        # Bound latency: if the output has grown too far ahead (the game
        # produced faster than real time), drop the oldest — keeps audio in
        # sync instead of lagging further and further behind the action.
        if len(self.out) > self.MAX_LAG * 2:
            del self.out[:len(self.out) - self.MAX_LAG * 2]
        if not self.playing:
            if len(self.out) < self.CUSHION * 2:
                return             # build a cushion so jitter can't underrun
            self.playing = True
        cb = self.CHUNK * 2        # bytes per chunk (16-bit)
        while len(self.out) >= cb:
            if not self.channel.get_busy():
                self.channel.play(pygame.mixer.Sound(buffer=bytes(self.out[:cb])))
            elif self.channel.get_queue() is None:
                self.channel.queue(pygame.mixer.Sound(buffer=bytes(self.out[:cb])))
            else:
                break              # both slots full — hold the rest
            del self.out[:cb]
        if self.playing and not self.channel.get_busy() and len(self.out) < cb:
            self.playing = False   # re-cushion after an underrun


def run_viewer(rt, *, scale: int = 3, title: str = "dos_re PM",
               artifacts_dir: str | Path = "artifacts",
               frame_tick_addr: int | None = None,
               record_replay: str | None = None,
               replay_profile: ReplayExecutionIdentity | None = None) -> int:
    import pygame
    from .pm_replay_input import FrameClock, FramePaced, KEY_CHANNEL, key_payload

    pygame.init()
    # A fixed 4:3 canvas: every VGA geometry this runtime produces (320x200
    # mode 13h, Mode X 320x240 / 320x400) displays at the aspect a real
    # monitor showed — the frame is scaled to the canvas each present.
    win = pygame.display.set_mode((320 * scale, 240 * scale))
    pygame.display.set_caption(title)
    pygame.mouse.set_visible(False)      # the game draws its own cursor

    dos, cpu = rt.dos, rt.cpu
    dos.time_source = time.monotonic     # 3DAh retrace advances at 70 Hz real time
    pcm = _make_audio_sink(dos.sound_blaster)
    artifacts = Path(artifacts_dir)

    # Replay recording (F11): a FrameClock keyed to the game's per-frame entry
    # tags input with the frame index, so the replay playbacks deterministically.
    mouse_norm = [0.5, 0.5]
    mbtn = [0]                       # mouse-button mask (host copy)
    rec = {"recording": None, "start": 0, "last_mouse": None, "dir": None}
    # While recording, input is BUFFERED and applied to the VM only at the frame
    # boundary (on_frame) — never mid-frame.  On a slow interpreter one game
    # frame spans several presents, so an async key/mouse event landing between
    # them would change the VM mid-frame at a wall-clock-dependent instruction,
    # yet the replay records/replays input only at boundaries → the replay would
    # diverge.  Boundary-quantizing input makes recording match replay exactly.
    pending = {"keys": []}
    # True while the frame clock isn't advancing (the game sits in its pause
    # loop, which never hits the per-frame tick).  Keys pressed then can't wait
    # for on_frame to flush the buffer, so they're delivered immediately and
    # recorded at the paused frame — replay gets pause+unpause at one frame and
    # collapses the pause to zero, staying in sync (a pause is a state-neutral
    # spin, so nothing is lost).
    stalled = [False]

    def on_frame(frame):
        if rec["recording"] is not None:
            f = frame - rec["start"]
            # Apply + record the input buffered since the previous boundary.
            # Apply EXACTLY the value we record (the rounded sample), not the
            # full-precision mouse_norm: the game maps the normalized mouse onto
            # a pixel and is sensitive to <1e-4 (≈0.06 px) differences, so
            # applying full precision while recording a rounded value makes the
            # replay land on a different pixel and diverge (observed ~frame 22).
            sample = [round(mouse_norm[0], 4), round(mouse_norm[1], 4), mbtn[0]]
            dos.set_mouse_norm(sample[0], sample[1], sample[2])
            if sample != rec["last_mouse"]:
                rec["recording"].add(f, MOUSE_CHANNEL, mouse_payload(*sample))
                rec["last_mouse"] = sample
            for make, name in pending["keys"]:
                rec["recording"].add(f, KEY_CHANNEL, key_payload(name, make))
                send_key(dos, name, make)
            pending["keys"].clear()

    clock = FrameClock(cpu, frame_tick_addr, on_frame) if frame_tick_addr else None

    # Pacing.  With an adapter frame clock we run the game with DETERMINISTIC
    # retrace (the vsync wait exits in ~2 reads instead of spinning thousands
    # of emulated 3DAh reads per frame) and throttle to 70 logical frames/sec
    # by wall-clock — so the CPU emulates one frame's real work per frame, not
    # the busy-wait.  Without a frame clock, fall back to wall-clock retrace.
    paced = clock is not None
    if paced:
        dos.time_source = None
    period = 1 / 70.0
    MAX_CATCHUP = 2                 # frames advanced per present — bounded so a
                                    # slow game degrades smoothly (1-2 frames /
                                    # present) instead of bursting dozens at once
    PACE_CHUNK = 40_000            # instructions per inner run before re-checking
                                    # (small enough that one chunk ~ the wall cap
                                    # even on the slow CPython path, so a paused
                                    # game stays responsive; a normal frame breaks
                                    # via FramePaced inside the first chunk anyway)
    PACE_WALL_CAP = 0.05           # ... and a wall-time ceiling per present, so a
                                    # frame that never arrives can't stall the loop
    last_time = time.monotonic()

    def advance():
        """Advance the frames due since the last present (paced), capped at
        MAX_CATCHUP.  The frame clock breaks each run precisely at the next
        frame boundary (FramePaced), so the game runs at its true rate.

        A frame that never completes — the game's pause loop doesn't hit the
        per-frame tick, so FramePaced never fires — is bounded by a wall-time
        ceiling instead of running the whole multi-million-instruction budget.
        Otherwise one present would stall for seconds (the UI 'freezes') and the
        Sound Blaster would produce that many seconds of PCM in a single burst,
        overflowing the audio sink.  Chunking + a wall cap keeps the present
        responsive regardless of interpreter speed."""
        nonlocal waiting_console, running, last_time
        if waiting_console:
            return
        any_done = False
        try:
            if paced:
                now = time.monotonic()
                n = min(MAX_CATCHUP, max(1, round((now - last_time) / period)))
                last_time = now
                deadline = now + PACE_WALL_CAP
                for _ in range(n):
                    if cpu.halted:
                        break
                    clock.stop_at = clock.frame + 1
                    frame_done = False
                    try:
                        while True:
                            cpu.run(PACE_CHUNK)     # until the next frame boundary
                            if time.monotonic() >= deadline:
                                break
                    except FramePaced:
                        frame_done = True
                    if not frame_done:
                        break                       # no frame yet (paused/stuck)
                    any_done = True
                stalled[0] = not any_done           # frame clock parked (paused)?
            else:
                cpu.run(20_000)
        except DosInputExhausted:
            waiting_console = True
        except Exception as e:  # noqa: BLE001 — the fail-loud frontier
            print(f"STOP at eip=0x{cpu.eip:X}: {type(e).__name__}: {e}")
            running = False
        if cpu.halted:
            print(f"program exited (code {dos.exit_code})")
            running = False

    def service_audio():
        """Deliver a due SB block-complete IRQ while the frame is parked.

        The game refills its single-cycle DMA in the IRQ handler; with the CPU
        idle between presents, that IRQ would wait up to a present period and
        the audio would gap/click.  Running here delivers the ISR (the frame
        clock is parked, so the pending-IRQ poll fires the audio ISR and IRETs
        back to the parked frame — no game advance) and refills promptly."""
        nonlocal waiting_console, running
        sb = dos.sound_blaster
        if not paced or waiting_console or sb is None or sb.clock is None:
            return False
        if not (sb._block_pending and sb.clock() >= sb._block_due):
            return False
        # Normally the frame is parked at a boundary, so the ISR fires and IRETs
        # straight back (FramePaced).  If it isn't (e.g. the game is paused in a
        # loop that never hits the frame tick), bound the run by wall time the
        # same way advance() does, so delivering the ISR can't turn into a
        # multi-hundred-thousand-instruction stall.
        deadline = time.monotonic() + PACE_WALL_CAP
        try:
            while True:
                cpu.run(PACE_CHUNK)
                if time.monotonic() >= deadline:
                    break
        except FramePaced:
            pass
        except DosInputExhausted:
            waiting_console = True
        except Exception as e:  # noqa: BLE001
            print(f"STOP at eip=0x{cpu.eip:X}: {type(e).__name__}: {e}")
            running = False
        return True

    waiting_console = False
    next_present = time.monotonic()
    running = True
    while running:
        now = time.monotonic()
        if now < next_present:
            if service_audio():
                pcm.pump()
            else:
                time.sleep(min(next_present - now, 0.002))
            continue
        next_present = max(next_present + period, now)
        advance()                    # one bounded step per present
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type in (pygame.KEYDOWN, pygame.KEYUP):
                make = ev.type == pygame.KEYDOWN
                name = pygame.key.name(ev.key)
                if make and name == "f10":
                    shots = artifacts / "screenshots"
                    shots.mkdir(parents=True, exist_ok=True)
                    rgb, w, h = render_pm_frame(dos)
                    out = shots / f"shot_{int(now * 1000)}.png"
                    write_rgb_png(out, rgb, width=w, height=h)
                    print(f"screenshot -> {out}")
                    continue
                if make and name == "f12":
                    from .pm_snapshot import save_pm_snapshot
                    out = artifacts / "snapshots" / f"snap_{int(now * 1000)}"
                    save_pm_snapshot(rt, out)
                    print(f"snapshot -> {out}")
                    continue
                if make and name == "f11":
                    if clock is None:
                        print("replay recording needs a frame_tick_addr (adapter)")
                    elif rec["recording"] is None:
                        # Flip the SB to the deterministic instruction-count
                        # clock BEFORE snapshotting: the casual viewer paces
                        # audio by wall time, but a recording made on that clock
                        # can't be replayed (its block-IRQ timeline diverges).
                        # The snapshot then captures the re-based block state, so
                        # replay continues it identically.
                        dos.set_sound_clock(deterministic=True)
                        bundle = (Path(record_replay) if record_replay else
                                  artifacts / "replays" / f"replay_{int(now * 1000)}")
                        if replay_profile is None:
                            raise ValueError("PM recording requires an execution profile")
                        if replay_profile.role != "oracle":
                            raise RuntimeError(
                                "ReplayArtifact recording requires the untouched "
                                "oracle plan"
                            )
                        recording = ReplayRecording(
                            bundle,
                            timeline_id=f"protected-mode-frame-boundaries:{frame_tick_addr:#x}:v1",
                            profile=replay_profile,
                            base_state=capture_pm_continuation(rt, event_cursor=0),
                            metadata={
                                "frame_tick_addr": int(frame_tick_addr),
                                "mouse_present": True,
                            },
                        )
                        rec.update(recording=recording, dir=bundle, start=clock.frame,
                                   last_mouse=None)
                        print(f"replay recording STARTED -> {bundle} "
                              f"(embedded base; F11 again to stop)")
                    else:
                        recording = rec["recording"]
                        total_frames = clock.frame - rec["start"]
                        end_state = capture_pm_continuation(
                            rt, event_cursor=recording.event_count)
                        recording.finish(total_frames, end_state=end_state)
                        print(f"replay recording STOPPED -> {rec['dir']} "
                              f"({total_frames} frames, "
                              f"{recording.event_count} events)")
                        rec["recording"] = None
                        dos.set_sound_clock(deterministic=False)  # back to live audio
                    continue
                if name in ("f10", "f11", "f12"):
                    continue          # viewer hotkeys — never game input (the
                                      # F11 release used to leak into the replay)
                if rec["recording"] is None:
                    send_key(dos, name, make)       # live play: deliver now
                elif stalled[0]:
                    # Paused: on_frame won't fire to flush the buffer, so deliver
                    # now (unpauses the game) and record at the paused frame so
                    # the replay collapses the pause to zero.
                    f = max(0, clock.frame - 1 - rec["start"])
                    rec["recording"].add(f, KEY_CHANNEL, key_payload(name, make))
                    send_key(dos, name, make)
                else:
                    pending["keys"].append((make, name))   # buffer for on_frame
                if make and waiting_console:
                    ch = ev.unicode
                    if ch:
                        dos.key_queue.append(ord(ch[0]) & 0xFF)
                        waiting_console = False
            elif ev.type == pygame.MOUSEMOTION:
                mx, my = ev.pos
                ww, wh = win.get_size()
                # Window-relative position, mapped onto the game's own
                # INT 33h virtual range by the host.
                mouse_norm[0] = mx / max(1, ww - 1)
                mouse_norm[1] = my / max(1, wh - 1)
                if rec["recording"] is None:              # recording defers to on_frame
                    dos.set_mouse_norm(mouse_norm[0], mouse_norm[1], mbtn[0])
            elif ev.type in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP):
                down = ev.type == pygame.MOUSEBUTTONDOWN
                bit = {1: 1, 3: 2}.get(ev.button, 0)
                mbtn[0] = (mbtn[0] | bit) if down else (mbtn[0] & ~bit)
                if rec["recording"] is None:              # recording defers to on_frame
                    dos.mouse_buttons = mbtn[0]

        rgb, w, h = render_pm_frame(dos)
        frame = pygame.image.frombuffer(rgb, (w, h), "RGB").convert(win)
        pygame.transform.scale(frame, win.get_size(), win)
        pygame.display.flip()
        pcm.pump()
    # Flush an in-progress recording on exit: closing the window (or ESC)
    # instead of pressing F11-to-stop must still save the captured replay, not
    # discard it — otherwise a full playthrough recorded live is lost and the
    # on-disk manifest is stuck at the empty START write (0 frames).
    if rec["recording"] is not None and clock is not None:
        recording = rec["recording"]
        total_frames = clock.frame - rec["start"]
        recording.finish(
            total_frames,
            end_state=capture_pm_continuation(
                rt, event_cursor=recording.event_count))
        print(f"replay recording flushed on exit -> {rec['dir']} "
              f"({total_frames} frames, {recording.event_count} events)")
    if hasattr(pcm, "close"):
        pcm.close()
    pygame.quit()
    return 0


def run_replay(rt, replay_path, *, boot_keys=(), extra_frames: int = 30,
               max_steps: int = 200_000_000, snapshot_dir: str | None = None,
               png: str = "", show: bool = False, scale: int = 3,
               title: str = "dos_re PM (replay)",
               replay_profile: ReplayExecutionIdentity | None = None) -> int:
    """Replay an input replay deterministically (no wall-clock pacing).

    Re-injects each frame's recorded input at the game's own frame boundary,
    then runs ``extra_frames`` past the replay's end.  Optionally saves a
    snapshot / PNG of the resulting state, or shows it in a window."""
    from .pm_replay_input import (
        FrameClock, FramePaced, ProtectedModeInputAdapter)

    artifact = ReplayArtifact.open(replay_path)
    metadata = artifact.metadata
    frame_tick_addr = int(metadata["frame_tick_addr"])
    end_point = ReplayPoint.from_json(metadata["end_point"])
    profile_id = str(metadata["recording_profile_id"])
    profiles = {profile.profile_id: profile for profile, _ in artifact.profiles()}
    if profile_id not in profiles:
        raise ValueError(f"recording profile is absent from artifact: {profile_id!r}")
    base = artifact.restore(
        profiles[profile_id], ReplayPoint(0, artifact.timeline_id))
    if replay_profile is not None:
        source_profile = profiles[profile_id]
        for field in ("image", "runtime", "devices", "continuation_schema"):
            if getattr(replay_profile, field) != getattr(source_profile, field):
                raise ValueError(
                    f"replay {field} identity differs from the recorded base")
        registered = {profile.profile_id for profile, _ in artifact.profiles()}
        if replay_profile.profile_id not in registered:
            artifact.register_profile(
                replay_profile,
                base_point=ReplayPoint(0, artifact.timeline_id),
                base_state=base,
            )
        else:
            artifact.require_profile(replay_profile)
    apply_pm_continuation(rt, base)
    adapter = ProtectedModeInputAdapter(
        artifact.events, event_cursor=base.event_cursor)
    dos = rt.dos
    end_frame = end_point.ordinal + extra_frames
    done = {"flag": False}

    def on_frame(frame):
        adapter.apply(frame, dos, deliver_key=send_key)
        if frame >= end_frame:
            done["flag"] = True

    clock = FrameClock(rt.cpu, frame_tick_addr, on_frame)

    def _finish():
        if snapshot_dir:
            from .pm_snapshot import save_pm_snapshot
            save_pm_snapshot(rt, snapshot_dir)
            print(f"snapshot -> {snapshot_dir}")
        if png:
            rgb, w, h = render_pm_frame(dos)
            write_rgb_png(Path(png), rgb, width=w, height=h)
            print(f"wrote {png}")

    if not show:
        # Headless deterministic replay (verification): run flat-out, no window.
        steps = 0
        try:
            while not done["flag"] and steps < max_steps and not rt.cpu.halted:
                rt.cpu.run(50_000)
                steps += 50_000
        except Exception as e:  # noqa: BLE001
            print(f"STOP at eip=0x{rt.cpu.eip:X}: {type(e).__name__}: {e}")
        print(f"replayed to frame ~{end_point.ordinal}+{extra_frames}; "
              f"{rt.cpu.instruction_count} instructions "
              f"(no window; pass --show to WATCH the replay)")
        _finish()
        return 0

    # Live, paced playback so a recorded replay can be WATCHED (verify by eye):
    # one game frame per iteration, rendered and throttled to 70 fps.  Esc or
    # closing the window quits early; the final frame stays up until dismissed.
    import pygame
    from .pm_replay_input import FramePaced
    pygame.init()
    win = pygame.display.set_mode((320 * scale, 240 * scale))
    pygame.display.set_caption(title)
    pygame.mouse.set_visible(False)          # the game draws its own cursor
    pcm = _make_audio_sink(rt.dos.sound_blaster)
    rt.dos.time_source = None                # frame clock paces; deterministic retrace
    period = 1 / 70.0

    def _present():
        rgb, w, h = render_pm_frame(dos)
        img = pygame.image.frombuffer(rgb, (w, h), "RGB").convert(win)
        pygame.transform.scale(img, win.get_size(), win)
        pygame.display.flip()

    running = True
    while running and not done["flag"] and not rt.cpu.halted:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT or (
                    ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE):
                running = False
        t0 = time.monotonic()
        clock.stop_at = clock.frame + 1
        try:
            rt.cpu.run(4_000_000)
        except FramePaced:
            pass
        except Exception as e:  # noqa: BLE001
            print(f"STOP at eip=0x{rt.cpu.eip:X}: {type(e).__name__}: {e}")
            break
        if pcm is not None:
            pcm.pump()
        _present()
        dt = time.monotonic() - t0
        if dt < period:
            time.sleep(period - dt)

    print(f"replayed to frame {clock.frame}; {rt.cpu.instruction_count} instructions")
    _finish()
    _present()                               # hold the final frame until dismissed
    while running:
        for ev in pygame.event.get():
            if ev.type in (pygame.QUIT, pygame.KEYDOWN):
                running = False
    pygame.quit()
    return 0


def run_headless(rt, *, steps: int, png: str = "", boot_keys=()) -> int:
    for k in boot_keys:
        rt.dos.key_queue.append(k)
    try:
        rt.cpu.run(steps)
    except Exception as e:  # noqa: BLE001
        print(f"STOP after {rt.cpu.instruction_count} at eip=0x{rt.cpu.eip:X}: "
              f"{type(e).__name__}: {e}")
        return 1
    print(f"ran {rt.cpu.instruction_count} instructions; halted={rt.cpu.halted}")
    if png:
        rgb, w, h = render_pm_frame(rt.dos)
        write_rgb_png(Path(png), rgb, width=w, height=h)
        print(f"wrote {png}")
    return 0


def _configure_sound(dos, sound_blaster, *, deterministic: bool):
    """Set up the emulated Sound Blaster for a run.

    DETERMINISM CONTRACT.  The block-complete IRQ's firing point steers the
    whole execution (its ISR runs mid-frame; where it lands changes the
    instruction stream the game sees — enough to make a replay playback diverge, or
    even crash).  So every reproducible path — recording a replay, replaying one,
    headless verify — keeps the SB on the DETERMINISTIC instruction-count clock
    (``instruction_count / EMULATED_IPS``, auto-serviced by ``pending_irq``), so
    the IRQ fires at the same emulated instant every run.  A replay recorded on
    this clock replays byte-identically on it.  ONLY the casual live viewer
    (not recording) retargets to wall-clock pacing — smoother audio when the
    interpreter keeps up, but explicitly NOT reproducible.

    On a resumed snapshot the host already carries the SB restored mid-stream on
    that instruction-count clock, so the deterministic paths leave it alone."""
    base, irq, dma = sound_blaster
    if dos.sound_blaster is not None:
        if not deterministic:                # casual viewer: wall-clock pacing
            dos.sound_blaster.clock = time.monotonic
            dos.sound_blaster.anchor_cadence = True
        return dos.sound_blaster             # else keep the instruction-count clock
    return dos.attach_sound_blaster(
        base=base, irq=irq, dma=dma,
        clock=None if deterministic else time.monotonic,   # None => instruction-count
        anchor_cadence=not deterministic)


class PMFrontend(GameFrontend):
    """Protected-mode execution driver for the canonical player pipeline."""

    default_steps_per_frame = 20_000_000

    def __init__(
        self,
        root: str | Path,
        *,
        default_exe: str | None = None,
        create_runtime=None,
        title: str = "dos_re PM",
        boot_keys=(),
        sound_blaster: tuple[int, int, int] | None = None,
        frame_tick_addr: int | None = None,
    ) -> None:
        super().__init__(root)
        self.default_exe = default_exe
        self._runtime_factory = create_runtime
        self.title = title
        self.boot_keys = tuple(boot_keys)
        self.sound_blaster = sound_blaster
        self.frame_tick_addr = frame_tick_addr

    def add_arguments(self, parser) -> None:
        parser.add_argument("--png", default="", help="render the final screen to this PNG")
        parser.add_argument("--no-sound", action="store_true")
        parser.add_argument("--show", action="store_true",
                            help="replay: show the final frame in a window")

    def launch(self, args, plan: ExecutionPlan) -> int:
        from .pm_snapshot import load_pm_snapshot
        from .runtime import create_pm_runtime

        deterministic = bool(args.headless or args.play_replay or args.record_replay)
        if args.snapshot:
            rt = load_pm_snapshot(args.exe, args.snapshot)
        else:
            factory = self._runtime_factory or create_pm_runtime
            rt = factory(args.exe)
        self.bind_execution_plan(rt, plan)
        if self.sound_blaster is not None and not args.no_sound:
            _configure_sound(rt.dos, self.sound_blaster, deterministic=deterministic)
        profile = _pm_profile(args.exe, rt, plan)
        if args.play_replay:
            return run_replay(
                rt, args.play_replay, boot_keys=self.boot_keys,
                snapshot_dir=None if args.save_snapshot in (None, "auto")
                else args.save_snapshot,
                png=args.png, show=args.show, scale=args.scale, title=self.title,
                replay_profile=profile,
            )
        if args.headless:
            return run_headless(
                rt, steps=args.steps or self.default_steps_per_frame,
                png=args.png, boot_keys=self.boot_keys,
            )
        return run_viewer(
            rt, scale=args.scale, title=self.title,
            artifacts_dir=self.artifacts_dir,
            frame_tick_addr=self.frame_tick_addr,
            record_replay=args.record_replay,
            replay_profile=profile,
        )
