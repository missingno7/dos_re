"""opl3_fast — a fast APPROXIMATE OPL3 (YMF262) synth for CPython (numpy).

The playback backend for CPython viewers: perceptually transparent against
the exact core for game music, at 10-40x real-time.  It is NOT bit-exact and
never claims to be — the vendored ``pynuked_opl3`` C core (bit-exact,
native) and ``graveyard/opl3_exact.py`` (the dormant literal Nuked-OPL3
translation, calibration reference) remain the exact implementations.
``dos_re.audio_sink.load_opl3`` selects this by default; the external
pynuked_opl3 package is an opt-in accuracy upgrade (DOSRE_OPL3_BACKEND=nuked).

What is modeled faithfully (calibrated against the exact core — see
``tests/test_opl3_fast.py`` and the calibration notes below):

* pitch — exact f_num/block/MULT arithmetic (incl. the doubled MULT table);
* note timing — register writes are applied sample-accurately by splitting
  render blocks at write positions;
* envelopes — ADSR in the chip's attenuation domain (0.1875 dB units) with
  rate/KSR/KSL semantics; stage slopes and the attack curve are CALIBRATED
  numerically against dos_re.opl3's envelope generator (constants below);
* 2-op FM and additive algorithms, operator feedback (two fixed-point
  iterations of the self-phase-modulation), all eight OPL3 waveforms,
  tremolo/vibrato as the chip's real stepped LFO patterns (3.7 Hz / 6.1 Hz),
  OPL3 stereo (CHA/CHB) and the second register bank;
* rhythm mode — BD/TOM as their real operator recipes; HH/SD/TC as their
  slots' envelopes/pitches driving noise-modulated phase (a standard
  lightweight approximation of the LFSR phase logic).

Deliberate approximations (inaudible or near-inaudible in game material):
float sine instead of the log-sin/exp ROM quantization, analytic envelope
segments instead of the bit-timed EG schedule, iterated instead of recursive
feedback, no 4-op pairing (each half of a pair plays as 2-op; none of the
current games use 4-op), no OPL3 CHC/CHD (4-channel) outputs.

Determinism: output is a pure function of the register-write sequence and
positions (the rhythm noise source is a per-chip seeded PCG64), so repeated
runs produce identical bytes.

API-compatible with ``dos_re.opl3.OPL3``/``pynuked_opl3.OPL3``:
``OPL3Fast(sample_rate)``, ``write``, ``write_immediate``,
``generate_stereo(n) -> bytes``, ``generate_mono``, ``reset``.
"""
from __future__ import annotations

import sys
from array import array

import numpy as np

__all__ = ["OPL3Fast", "OPL_NATIVE_RATE"]

OPL_NATIVE_RATE = 49716

_MT = (1, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 20, 24, 24, 30, 30)  # MULT x2
_KSL = (0, 32, 40, 45, 48, 51, 53, 55, 56, 58, 59, 60, 61, 62, 63, 64)
_KSLSHIFT = (8, 1, 2, 0)

# Modulation depths, derived from the integer chip semantics:
# a full-scale operator is +-4084 exp-ROM units on a 1024-unit phase circle.
_MOD_TURNS = 4084.0 / 1024.0            # carrier phase swing per unit modulator
# feedback: (prout + out) >> (9 - fb)  ->  2*4084 / 2^(9-fb) phase units.
_FB_TURNS = tuple(0.0 if fb == 0 else (2.0 * 4084.0 / (1 << (9 - fb))) / 1024.0
                  for fb in range(8))

# Attenuation domain: EG level 0..511 in 0.1875 dB units (level/32 = att in
# 6 dB units for exp2), TL adds level<<2 * ... (tl unit = 0.75 dB = 4 levels).
_SILENCE = 511.0                         # fully off
_ENV_OFF_GATE = 496.0                    # below-audibility gate for skipping work

# --- EG rate tables, CALIBRATED against dos_re.opl3 (the exact core) -------------
# Decay/release: linear level increase per CHIP sample for rate index 0..63
# (rate = 4*reg_rate + ks).  The exact EG steps on a bit-timed schedule; its
# long-run slope is (4+rate_lo)/4 * 2^(rate_hi-13), saturating at rate_hi 15.
# Verified against measured eg_rout trajectories (see tests).
def _dec_slope(rate: int) -> float:
    if rate <= 0:
        return 0.0
    rate_hi = min(rate >> 2, 15)
    rate_lo = rate & 3
    if rate_hi == 15:
        return 2.0                        # immediate-ish: 2 levels/sample
    return (4 + rate_lo) / 4.0 * (2.0 ** (rate_hi - 13))


_DEC_SLOPE = tuple(_dec_slope(r) for r in range(64))

# Attack: the exact EG does eg += ~eg >> (4-shift) on the same schedule — an
# exponential decay of the level toward 0 that terminates in finite time.
# Model: level(t) = L0 * exp(-t/tau), snapped to 0 below 1 level; tau in chip
# samples, tau = C / slope-rate.  C calibrated so 511->0 times match the
# exact core within a few percent across the audible rate range.
# C = 6.0 fitted from measured 90%-rise times of the exact core at ar 4..10
# (see the calibration harness in the introduction commit).
_ATTACK_TAU = tuple(0.0 if r >= 60 else (0.0 if r == 0 else 6.00 / _DEC_SLOPE[r])
                    for r in range(64))  # r>=60 (rate_hi 15): instant attack

# --- LFOs: the chip's real stepped patterns -----------------------------------------
# Tremolo: position advances every 64 chip samples through a 210-step
# triangle; depth >> tremoloshift (AM depth bit: 2 deep / 4 shallow).
_TREM_PATTERN = np.array([(p if p < 105 else 210 - p) for p in range(210)],
                         dtype=np.float64)
# Vibrato: position advances every 1024 chip samples through an 8-step
# pattern applied to f_num>>7 (see OPL3_PhaseGenerate).
def _vib_scale(pos: int, shift: int) -> float:
    if not (pos & 3):
        return 0.0
    s = 1.0 if not (pos & 1) else 0.5
    s /= (1 << shift)
    return -s if pos & 4 else s


class _Op:
    __slots__ = ("mult", "ksr", "egt", "vib", "trem", "ksl", "tl",
                 "ar", "dr", "sl", "rr", "wf",
                 "stage", "level", "phase", "key", "fb1", "fb2")

    def __init__(self) -> None:
        self.mult = 0
        self.ksr = 0
        self.egt = 0
        self.vib = 0
        self.trem = 0
        self.ksl = 0
        self.tl = 63
        self.ar = 0
        self.dr = 0
        self.sl = 0
        self.rr = 0
        self.wf = 0
        self.stage = 4          # 0 attack, 1 decay, 2 sustain, 3 release, 4 off
        self.level = _SILENCE
        self.phase = 0.0        # turns
        self.key = 0
        self.fb1 = 0.0          # serial-feedback history (fb 6-7), carried across blocks
        self.fb2 = 0.0


class _Ch:
    __slots__ = ("fnum", "block", "fb", "con", "cha", "chb", "ops")

    def __init__(self) -> None:
        self.fnum = 0
        self.block = 0
        self.fb = 0
        self.con = 0
        self.cha = True
        self.chb = True
        self.ops = (_Op(), _Op())


_AD_SLOT = (0, 1, 2, 3, 4, 5, -1, -1, 6, 7, 8, 9, 10, 11, -1, -1,
            12, 13, 14, 15, 16, 17, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1)
# slot index within bank (0..17) -> (channel 0..8, op 0/1)
_SLOT_CH = tuple((s % 3 + (s // 6) * 3, (s // 3) & 1) for s in range(18))


class OPL3Fast:
    """Approximate OPL3.  See the module docstring for the fidelity contract."""

    def __init__(self, sample_rate: int = 44100) -> None:
        self.sample_rate = int(sample_rate)
        self.reset()

    def reset(self, sample_rate: int | None = None) -> None:
        if sample_rate is not None:
            self.sample_rate = int(sample_rate)
        self.chan = [_Ch() for _ in range(18)]
        self.newm = 0
        self.nts = 0
        self.rhy = 0
        self.dam = 0                     # tremolo depth bit
        self.dvb = 0                     # vibrato depth bit
        self._pos = 0                    # absolute output-sample position
        self._events: list[tuple[int, int, int]] = []
        self._rng = np.random.Generator(np.random.PCG64(0xAD11B))
        self._chip_per_out = OPL_NATIVE_RATE / self.sample_rate
        # LFO positions in chip samples (advance across blocks)
        self._trem_chip = 0.0
        self._vib_chip = 0.0

    # --- register interface ----------------------------------------------------------

    def write(self, reg: int, value: int) -> None:
        self._events.append((self._pos, int(reg) & 0x1FF, int(value) & 0xFF))

    def write_immediate(self, reg: int, value: int) -> None:
        self._apply(int(reg) & 0x1FF, int(value) & 0xFF)

    def _ksv(self, ch: _Ch) -> int:
        return (ch.block << 1) | ((ch.fnum >> (9 - self.nts)) & 1)

    def _rate_index(self, ch: _Ch, op: _Op, reg_rate: int) -> int:
        ks = self._ksv(ch) >> ((op.ksr ^ 1) << 1)
        return min(63, ks + (reg_rate << 2))

    def _key_on(self, ch: _Ch) -> None:
        for op in ch.ops:
            if not op.key:
                op.key = 1
                op.phase = 0.0
                if op.stage == 4:            # was fully off: no history to carry
                    op.fb1 = op.fb2 = 0.0
                # retrigger of a still-ringing voice keeps its feedback history
                # (the real chip never clears it on key-on) -> no onset click
                op.stage = 0 if op.ar else 4     # AR=0: never starts (real chip)
                if op.ar >= 15 or self._rate_index(ch, op, op.ar) >= 60:
                    op.level = 0.0
                    op.stage = 1

    def _key_off(self, ch: _Ch) -> None:
        for op in ch.ops:
            if op.key:
                op.key = 0
                if op.stage < 3:
                    op.stage = 3

    def _apply(self, reg: int, v: int) -> None:
        high = (reg >> 8) & 1
        regm = reg & 0xFF
        group = regm & 0xF0
        if group == 0x00:
            if high and (regm & 0x0F) == 0x05:
                self.newm = v & 1
            elif not high and (regm & 0x0F) == 0x08:
                self.nts = (v >> 6) & 1
            return
        if group in (0x20, 0x30, 0x40, 0x50, 0x60, 0x70, 0x80, 0x90, 0xE0, 0xF0):
            s = _AD_SLOT[regm & 0x1F]
            if s < 0:
                return
            chn, opi = _SLOT_CH[s]
            op = self.chan[chn + 9 * high].ops[opi]
            if group in (0x20, 0x30):
                op.trem = (v >> 7) & 1
                op.vib = (v >> 6) & 1
                op.egt = (v >> 5) & 1
                op.ksr = (v >> 4) & 1
                op.mult = v & 0x0F
            elif group in (0x40, 0x50):
                op.ksl = (v >> 6) & 3
                op.tl = v & 0x3F
            elif group in (0x60, 0x70):
                op.ar = (v >> 4) & 0x0F
                op.dr = v & 0x0F
            elif group in (0x80, 0x90):
                op.sl = (v >> 4) & 0x0F
                op.rr = v & 0x0F
            else:
                op.wf = (v & 0x07) if self.newm else (v & 0x03)
            return
        if (regm & 0x0F) > 8 and regm != 0xBD:
            return
        if group == 0xA0:
            ch = self.chan[(regm & 0x0F) + 9 * high]
            ch.fnum = (ch.fnum & 0x300) | v
        elif group == 0xB0:
            if regm == 0xBD and not high:
                self.dam = (v >> 7) & 1
                self.dvb = (v >> 6) & 1
                old = self.rhy
                self.rhy = v & 0x3F
                if self.rhy & 0x20:
                    self._rhythm_keys(old)
                else:
                    for chn in (6, 7, 8):
                        self._key_off(self.chan[chn])
                return
            ch = self.chan[(regm & 0x0F) + 9 * high]
            ch.fnum = (ch.fnum & 0xFF) | ((v & 3) << 8)
            ch.block = (v >> 2) & 7
            if v & 0x20:
                self._key_on(ch)
            else:
                self._key_off(ch)
        elif group == 0xC0:
            ch = self.chan[(regm & 0x0F) + 9 * high]
            ch.fb = (v >> 1) & 7
            ch.con = v & 1
            if self.newm:
                ch.cha = bool(v & 0x10)
                ch.chb = bool(v & 0x20)
            else:
                ch.cha = ch.chb = True

    def _rhythm_keys(self, old: int) -> None:
        """Key the five percussion voices from the 0xBD bits (edge-triggered)."""
        new = self.rhy
        # bd: ch6 both ops (normal FM pair)
        for bit, chn, ops in ((0x10, 6, (0, 1)), (0x01, 7, (0,)),
                              (0x08, 7, (1,)), (0x04, 8, (0,)), (0x02, 8, (1,))):
            ch = self.chan[chn]
            if new & bit and not (old & bit):
                for oi in ops:
                    op = ch.ops[oi]
                    op.key = 1
                    op.phase = 0.0
                    op.level = 0.0 if op.ar >= 12 else op.level
                    op.stage = 1 if op.ar >= 12 else (0 if op.ar else 4)
            elif not (new & bit) and (old & bit):
                for oi in ops:
                    op = ch.ops[oi]
                    op.key = 0
                    if op.stage < 3:
                        op.stage = 3

    # --- envelope -----------------------------------------------------------------------

    def _env_block(self, ch: _Ch, op: _Op, n: int) -> np.ndarray | None:
        """Level(t) array (attenuation units) for n output samples, or None if
        the operator stays inaudibly off for the whole block."""
        if op.stage == 4 and op.level >= _ENV_OFF_GATE:
            return None
        cps = self._chip_per_out
        out = np.empty(n, dtype=np.float64)
        i = 0
        level = op.level
        stage = op.stage
        sl_level = float((op.sl if op.sl != 0x0F else 0x1F) << 4)
        while i < n:
            remain = n - i
            if stage == 0:  # attack (exponential toward 0)
                tau = _ATTACK_TAU[self._rate_index(ch, op, op.ar)] / cps
                if tau <= 0.0 or level < 1.0:
                    level = 0.0
                    stage = 1
                    continue
                t = np.arange(1, remain + 1, dtype=np.float64)
                seg = level * np.exp(-t / tau)
                done = np.searchsorted(-seg, -1.0)  # first index below 1 level
                if done < remain:
                    out[i:i + done + 1] = seg[:done + 1]
                    i += done + 1
                    level = 0.0
                    stage = 1
                    continue
                out[i:] = seg
                level = float(seg[-1])
                i = n
            elif stage in (1, 2, 3):
                if stage == 1:
                    slope = _DEC_SLOPE[self._rate_index(ch, op, op.dr)] * cps
                    target = sl_level
                elif stage == 2:
                    if op.egt:
                        out[i:] = level
                        i = n
                        continue
                    slope = _DEC_SLOPE[self._rate_index(ch, op, op.rr)] * cps
                    target = _SILENCE
                else:
                    slope = _DEC_SLOPE[self._rate_index(ch, op, op.rr)] * cps
                    target = _SILENCE
                if slope <= 0.0 or level >= target:
                    if stage == 1 and level >= target:
                        stage = 2
                        continue
                    out[i:] = level
                    i = n
                    continue
                steps = int((target - level) / slope) + 1
                take = min(steps, remain)
                t = np.arange(1, take + 1, dtype=np.float64)
                out[i:i + take] = level + slope * t
                level = float(out[i + take - 1])
                i += take
                if level >= target:
                    level = target
                    if stage == 1:
                        stage = 2
                    else:
                        stage = 4
                        level = _SILENCE
            else:  # off
                out[i:] = _SILENCE
                i = n
        op.level = level
        op.stage = stage
        return out

    def _op_gain(self, ch: _Ch, op: _Op, env: np.ndarray, trem: np.ndarray) -> np.ndarray:
        """Linear gain array from level + TL + KSL + tremolo (att units)."""
        ksl = (_KSL[ch.fnum >> 6] << 2) - ((8 - ch.block) << 5)
        if ksl < 0:
            ksl = 0
        att = env + (op.tl << 2) + (ksl >> _KSLSHIFT[op.ksl])
        if op.trem:
            att = att + trem
        return np.exp2(att * (-1.0 / 32.0))

    # --- waveforms ---------------------------------------------------------------------

    @staticmethod
    def _wave(wf: int, phase: np.ndarray) -> np.ndarray:
        x = phase - np.floor(phase)
        if wf == 0:
            return np.sin(2 * np.pi * x)
        if wf == 1:
            return np.maximum(np.sin(2 * np.pi * x), 0.0)
        if wf == 2:
            return np.abs(np.sin(2 * np.pi * x))
        if wf == 3:
            s = np.abs(np.sin(2 * np.pi * x))
            return np.where((x % 0.5) < 0.25, s, 0.0)
        if wf == 4:
            return np.where(x < 0.5, np.sin(4 * np.pi * x), 0.0)
        if wf == 5:
            return np.where(x < 0.5, np.abs(np.sin(4 * np.pi * x)), 0.0)
        if wf == 6:
            return np.where(x < 0.5, 1.0, -1.0)
        # wf 7: exponential "log-saw" (derived from the ROM definition)
        d = np.where(x < 0.5, x * 2.0, (1.0 - x) * 2.0)
        mag = np.exp2(-d * 16.0)          # 16 levels/oct in the <<3 domain / 256
        return np.where(x < 0.5, mag, -mag)

    # --- rendering ------------------------------------------------------------------------

    def _phase_inc(self, ch: _Ch, op: _Op) -> float:
        """Turns per OUTPUT sample (vibrato applied per block separately)."""
        inc_chip = (((ch.fnum << ch.block) >> 1) * _MT[op.mult]) >> 1
        return inc_chip / float(1 << 19) * self._chip_per_out

    def _lfo_blocks(self, n: int) -> tuple[np.ndarray, np.ndarray]:
        """(tremolo att-units array, vibrato pos array) for n output samples."""
        cps = self._chip_per_out
        chip_t = self._trem_chip + np.arange(n, dtype=np.float64) * cps
        trem_pos = ((chip_t / 64.0).astype(np.int64)) % 210
        trem = _TREM_PATTERN[trem_pos] / float(1 << (2 if self.dam else 4))
        chip_v = self._vib_chip + np.arange(n, dtype=np.float64) * cps
        vib_pos = ((chip_v / 1024.0).astype(np.int64)) & 7
        self._trem_chip = float(chip_t[-1] + cps) % (64.0 * 210.0)
        self._vib_chip = float(chip_v[-1] + cps) % (1024.0 * 8.0)
        return trem, vib_pos

    def _render(self, n: int, left: np.ndarray, right: np.ndarray, base: int) -> None:
        trem, vib_pos = self._lfo_blocks(n)
        vib_shift = 0 if self.dvb else 1
        vib_scale = np.array([_vib_scale(p, vib_shift) for p in range(8)],
                             dtype=np.float64)[vib_pos]
        rhythm = bool(self.rhy & 0x20)
        for chn, ch in enumerate(self.chan):
            is_drum_ch = rhythm and 6 <= chn <= 8
            if is_drum_ch and chn == 8:
                continue  # tom + tc render inside the ch7 block (single advance)
            m_op, c_op = ch.ops
            env_c = self._env_block(ch, c_op, n)
            env_m = self._env_block(ch, m_op, n)
            drum7 = is_drum_ch and chn == 7
            if env_c is None and env_m is None and not drum7:
                # ch7 in rhythm mode NEVER skips: tc/tom render in its block,
                # and slot 13's phase must keep advancing — its bits feed the
                # rhythm comb even while the hh envelope is off.
                continue
            if env_c is None and not (ch.con or is_drum_ch):
                continue                      # silent carrier, FM alg: nothing audible
            inc_m = self._phase_inc(ch, m_op)
            inc_c = self._phase_inc(ch, c_op)
            # vibrato: the chip adds a scaled (f_num>>7) to f_num
            vib_frac = (ch.fnum >> 7) / max(ch.fnum, 1)
            t = np.arange(1, n + 1, dtype=np.float64)
            vshift_m = (vib_scale * vib_frac * inc_m) if m_op.vib else 0.0
            vshift_c = (vib_scale * vib_frac * inc_c) if c_op.vib else 0.0
            ph_m = m_op.phase + inc_m * t + (np.cumsum(vshift_m) if m_op.vib else 0.0)
            ph_c = c_op.phase + inc_c * t + (np.cumsum(vshift_c) if c_op.vib else 0.0)
            m_op.phase = float(ph_m[-1]) % 1.0
            c_op.phase = float(ph_c[-1]) % 1.0

            sig = None
            drum_gain = 2.0  # rhythm wiring: each drum slot feeds TWO output taps
            if is_drum_ch and chn == 7:
                # hh (ch7 op0) + sd (ch7 op1) + tc (ch8 op1), built from the
                # REAL phase-bit recipe of the chip (rm_xor comb from the hh/tc
                # slot phases + a noise bit choosing fixed phase offsets) —
                # this reproduces the metallic comb structure; only the noise
                # bits differ statistically from the chip LFSR.  tom (ch8 op0)
                # is a pure tone and renders in ch8's normal pass.
                ch8 = self.chan[8]
                tc_op = ch8.ops[1]
                env_tc = self._env_block(ch8, tc_op, n)
                inc_tc = self._phase_inc(ch8, tc_op)
                ph_tc = tc_op.phase + inc_tc * t
                tc_op.phase = float(ph_tc[-1]) % 1.0
                hh_u = ((ph_m - np.floor(ph_m)) * 1024.0).astype(np.int64)
                tc_u = ((ph_tc - np.floor(ph_tc)) * 1024.0).astype(np.int64)
                b2 = (hh_u >> 2) & 1
                b3 = (hh_u >> 3) & 1
                b7 = (hh_u >> 7) & 1
                b8 = (hh_u >> 8) & 1
                t3 = (tc_u >> 3) & 1
                t5 = (tc_u >> 5) & 1
                rm_xor = ((b2 ^ b7) | (b3 ^ t5) | (t3 ^ t5)).astype(np.float64)
                nz = self._rng.integers(0, 2, n)
                if env_m is not None:   # hh
                    phase_units = rm_xor * 512.0 + np.where(
                        (rm_xor.astype(np.int64) ^ nz) != 0, 0xD0, 0x34)
                    g = self._op_gain(ch, m_op, env_m, trem)
                    sig = self._wave(m_op.wf, phase_units / 1024.0) * g
                if env_c is not None:   # sd
                    phase_units = b8 * 512.0 + ((b8 ^ nz) * 256.0)
                    g = self._op_gain(ch, c_op, env_c, trem)
                    part = self._wave(c_op.wf, phase_units / 1024.0) * g
                    sig = part if sig is None else sig + part
                if env_tc is not None:  # tc (mixed into ch8's outputs by the
                    # real chip; summing here is equivalent for CHA/CHB stereo)
                    phase_units = rm_xor * 512.0 + 0x80
                    g = self._op_gain(ch8, tc_op, env_tc, trem)
                    part = self._wave(tc_op.wf, phase_units / 1024.0) * g
                    sig = part if sig is None else sig + part
                tom_op = ch8.ops[0]
                env_tom = self._env_block(ch8, tom_op, n)
                if env_tom is not None:  # tom: pure tone on its own phase
                    inc_tom = self._phase_inc(ch8, tom_op)
                    ph_tom = tom_op.phase + inc_tom * t
                    tom_op.phase = float(ph_tom[-1]) % 1.0
                    g = self._op_gain(ch8, tom_op, env_tom, trem)
                    part = self._wave(tom_op.wf, ph_tom) * g
                    sig = part if sig is None else sig + part
            else:
                g_m = self._op_gain(ch, m_op, env_m, trem) if env_m is not None else None
                if ch.con and not is_drum_ch:
                    sig = np.zeros(n) if env_c is None and g_m is None else None
                    parts = []
                    if g_m is not None:
                        m = self._modulator(m_op, ph_m, g_m, ch.fb)
                        parts.append(m)
                    if env_c is not None:
                        parts.append(self._wave(c_op.wf, ph_c)
                                     * self._op_gain(ch, c_op, env_c, trem))
                    sig = parts[0] if len(parts) == 1 else parts[0] + parts[1]
                else:
                    if env_c is None:
                        continue
                    if g_m is not None:
                        m = self._modulator(m_op, ph_m, g_m, ch.fb)
                        ph = ph_c + _MOD_TURNS * m
                    else:
                        ph = ph_c
                    sig = self._wave(c_op.wf, ph) * self._op_gain(ch, c_op, env_c, trem)
            if sig is None:
                continue
            if is_drum_ch:
                sig = sig * drum_gain
            if ch.cha:
                left[base:base + n] += sig
            if ch.chb:
                right[base:base + n] += sig

    def _modulator(self, op: _Op, ph: np.ndarray, gain: np.ndarray, fb: int) -> np.ndarray:
        """Modulator output in [-1, 1] x gain.

        fb 1..5: two fixed-point iterations of the self-phase-modulation
        (measured maxHdiff <= 0.02 vs the exact core).  fb 6..7: the loop gain
        reaches ~1..2 turns and the true recurrence is chaotic (the chip's
        characteristic harsh/noisy timbre), which no fixed point reproduces —
        run the real two-sample-average recurrence serially; it is at most a
        modulator or two per block, so the cost is negligible.
        """
        w = self._wave
        if not fb:
            return w(op.wf, ph) * gain
        if fb < 6:
            g = _FB_TURNS[fb]
            m = w(op.wf, ph)
            m = w(op.wf, ph + g * m * gain)
            m = w(op.wf, ph + g * m * gain)
            return m * gain
        # serial: out[t] = wave(ph[t] + g*(out[t-1] + out[t-2])) * gain[t].
        # m1/m2 (the last two outputs) are carried on the operator across block
        # and segment boundaries; resetting them each block caused an audible
        # per-frame click on feedback voices (fb 6-7).
        import math
        g = _FB_TURNS[fb] * 0.5
        wf = op.wf
        out = np.empty(len(ph), dtype=np.float64)
        m1 = op.fb1
        m2 = op.fb2
        two_pi = 2.0 * math.pi
        phl = ph.tolist()
        gl = gain.tolist()
        for i in range(len(phl)):
            x = phl[i] + g * (m1 + m2)
            x -= math.floor(x)
            if wf == 0:
                v = math.sin(two_pi * x)
            elif wf == 1:
                v = max(math.sin(two_pi * x), 0.0)
            elif wf == 2:
                v = abs(math.sin(two_pi * x))
            elif wf == 3:
                v = abs(math.sin(two_pi * x)) if (x % 0.5) < 0.25 else 0.0
            elif wf == 4:
                v = math.sin(2 * two_pi * x) if x < 0.5 else 0.0
            elif wf == 5:
                v = abs(math.sin(2 * two_pi * x)) if x < 0.5 else 0.0
            elif wf == 6:
                v = 1.0 if x < 0.5 else -1.0
            else:
                d = x * 2.0 if x < 0.5 else (1.0 - x) * 2.0
                v = 2.0 ** (-d * 16.0)
                if x >= 0.5:
                    v = -v
            v *= gl[i]
            out[i] = v
            m2 = m1
            m1 = v
        op.fb1 = m1
        op.fb2 = m2
        return out

    # --- output -------------------------------------------------------------------------------

    #: master scale: one full-scale operator -> the exact core's 4084 int16 units
    _MASTER = 4084.0

    def generate_stereo(self, num_frames: int) -> bytes:
        num_frames = max(0, int(num_frames))
        if num_frames == 0:
            return b""
        left = np.zeros(num_frames, dtype=np.float64)
        right = np.zeros(num_frames, dtype=np.float64)
        # apply queued writes sample-accurately by segmenting the block
        events = [e for e in self._events if e[0] < self._pos + num_frames]
        self._events = [e for e in self._events if e[0] >= self._pos + num_frames]
        cursor = 0
        for when, reg, val in events:
            seg = min(max(when - self._pos, 0), num_frames) - cursor
            if seg > 0:
                self._render(seg, left, right, cursor)
                cursor += seg
            self._apply(reg, val)
        if cursor < num_frames:
            self._render(num_frames - cursor, left, right, cursor)
        self._pos += num_frames
        pcm = np.empty(num_frames * 2, dtype=np.int16)
        np.clip(left * self._MASTER, -32768, 32767, out=left)
        np.clip(right * self._MASTER, -32768, 32767, out=right)
        pcm[0::2] = left.astype(np.int16)
        pcm[1::2] = right.astype(np.int16)
        if sys.byteorder == "big":  # pragma: no cover
            pcm = pcm.byteswap()
        return pcm.tobytes()

    def generate_mono(self, num_frames: int) -> bytes:
        stereo = self.generate_stereo(num_frames)
        if not stereo:
            return stereo
        return memoryview(stereo).cast("h")[0::2].tobytes()
