"""Pure-Python Nuked-OPL3 — the canonical Yamaha OPL3 (YMF262) core of dos_re.

This is a literal Python translation of Nuked-OPL3 v1.8 by Alexey "Nuke.YKT"
Khokholov (https://github.com/nukeykt/Nuked-OPL3), the die-shot-accurate OPL3
emulator used by DOSBox-X and VGMPlay.  The translation mirrors the C source
function-for-function (each method names its C original) and is proven
BYTE-IDENTICAL to the compiled C reference over real game register streams,
rhythm/4-op/waveform sweeps and randomized fuzz by ``tests/test_opl3.py``
(golden PCM sha1 hashes recorded from the upstream cffi reference build
before it was retired from this repo).

DORMANT (2026-07): this bit-exact core is no longer the runtime backend and
is no longer in the importable ``dos_re`` package — it lives in ``graveyard/``
purely as the calibration/golden oracle for ``dos_re/opl3_fast.py`` (the numpy
approximate synth that replaced it) and as a provenance record.  It is ~1.0x
real-time on CPython on a busy chip — too slow for everyday playback, which is
why opl3_fast (~50x, perceptually indistinguishable on real game music)
became the default.  ``dos_re.audio_sink.load_opl3`` never selects this module;
runtime OPL3 is two-way (compiled pynuked_opl3 when built, else opl3_fast).
See graveyard/README.md.

Config parity: the C reference is compiled with the upstream defaults —
``OPL_ENABLE_STEREOEXT=0`` and therefore ``OPL_QUIRK_CHANNELSAMPLEDELAY=1``
(some FM channels are output one sample later on the left side than the
right, as on the real chip).  This translation implements exactly that
configuration.

Licensing: Nuked-OPL3 is licensed under the GNU LGPL v2.1 or later; as a
derivative work, THIS FILE is likewise LGPL-2.1-or-later —

    Nuked OPL3
    Copyright (C) 2013-2020 Nuke.YKT
    Python translation (C) 2026 the dos_re project

    This file is free software: you can redistribute it and/or modify it
    under the terms of the GNU Lesser General Public License as published by
    the Free Software Foundation, either version 2.1 of the License, or (at
    your option) any later version.  It is distributed WITHOUT ANY WARRANTY;
    see https://www.gnu.org/licenses/old-licenses/lgpl-2.1.html for details.

The rest of dos_re stays MIT; this module is self-contained and separable
(see the "Third-party components" note in LICENSE).

Original thanks (from the C source): MAME Development Team (Jarek
Burczynski, Tatsuyuki Satoh) — feedback and rhythm part calculation;
forums.submarine.org.uk (carbon14, opl3) — tremolo and phase generator;
OPLx decapsulated (Matthew Gambrell, Olli Niemitalo) — OPL2 ROMs;
siliconpr0n.org (John McMaster, digshadow) — YMF262 decaps and die shots.

API::

    from dos_re.opl3 import OPL3

    chip = OPL3(sample_rate=49716)
    chip.write(0x20, 0x01)      # buffered, chip-accurate register write
    chip.write(0xA0, 0x98)
    chip.write(0xB0, 0x31)      # key-on
    pcm = chip.generate_stereo(4096)   # little-endian int16 L,R frames
"""
from __future__ import annotations

import sys
from array import array

__all__ = ["OPL3", "OPL_NATIVE_RATE"]

#: Native output rate of the OPL3 (master clock 14.318 MHz / 288).
OPL_NATIVE_RATE = 49716

_WRITEBUF_SIZE = 1024
_WRITEBUF_DELAY = 2
_RSM_FRAC = 10

# Channel types (enum in the C source)
_CH_2OP = 0
_CH_4OP = 1
_CH_4OP2 = 2
_CH_DRUM = 3

# Envelope key types
_EGK_NORM = 0x01
_EGK_DRUM = 0x02

# Envelope generator stages
_EG_ATTACK = 0
_EG_DECAY = 1
_EG_SUSTAIN = 2
_EG_RELEASE = 3

# --- ROM tables, extracted mechanically from the C source (opl3.c v1.8) ---------------
# logsin/exp are the die-extracted OPL2 ROMs; do not edit by hand.

_LOGSIN = (
    0x859, 0x6c3, 0x607, 0x58b, 0x52e, 0x4e4, 0x4a6, 0x471,
    0x443, 0x41a, 0x3f5, 0x3d3, 0x3b5, 0x398, 0x37e, 0x365,
    0x34e, 0x339, 0x324, 0x311, 0x2ff, 0x2ed, 0x2dc, 0x2cd,
    0x2bd, 0x2af, 0x2a0, 0x293, 0x286, 0x279, 0x26d, 0x261,
    0x256, 0x24b, 0x240, 0x236, 0x22c, 0x222, 0x218, 0x20f,
    0x206, 0x1fd, 0x1f5, 0x1ec, 0x1e4, 0x1dc, 0x1d4, 0x1cd,
    0x1c5, 0x1be, 0x1b7, 0x1b0, 0x1a9, 0x1a2, 0x19b, 0x195,
    0x18f, 0x188, 0x182, 0x17c, 0x177, 0x171, 0x16b, 0x166,
    0x160, 0x15b, 0x155, 0x150, 0x14b, 0x146, 0x141, 0x13c,
    0x137, 0x133, 0x12e, 0x129, 0x125, 0x121, 0x11c, 0x118,
    0x114, 0x10f, 0x10b, 0x107, 0x103, 0x0ff, 0x0fb, 0x0f8,
    0x0f4, 0x0f0, 0x0ec, 0x0e9, 0x0e5, 0x0e2, 0x0de, 0x0db,
    0x0d7, 0x0d4, 0x0d1, 0x0cd, 0x0ca, 0x0c7, 0x0c4, 0x0c1,
    0x0be, 0x0bb, 0x0b8, 0x0b5, 0x0b2, 0x0af, 0x0ac, 0x0a9,
    0x0a7, 0x0a4, 0x0a1, 0x09f, 0x09c, 0x099, 0x097, 0x094,
    0x092, 0x08f, 0x08d, 0x08a, 0x088, 0x086, 0x083, 0x081,
    0x07f, 0x07d, 0x07a, 0x078, 0x076, 0x074, 0x072, 0x070,
    0x06e, 0x06c, 0x06a, 0x068, 0x066, 0x064, 0x062, 0x060,
    0x05e, 0x05c, 0x05b, 0x059, 0x057, 0x055, 0x053, 0x052,
    0x050, 0x04e, 0x04d, 0x04b, 0x04a, 0x048, 0x046, 0x045,
    0x043, 0x042, 0x040, 0x03f, 0x03e, 0x03c, 0x03b, 0x039,
    0x038, 0x037, 0x035, 0x034, 0x033, 0x031, 0x030, 0x02f,
    0x02e, 0x02d, 0x02b, 0x02a, 0x029, 0x028, 0x027, 0x026,
    0x025, 0x024, 0x023, 0x022, 0x021, 0x020, 0x01f, 0x01e,
    0x01d, 0x01c, 0x01b, 0x01a, 0x019, 0x018, 0x017, 0x017,
    0x016, 0x015, 0x014, 0x014, 0x013, 0x012, 0x011, 0x011,
    0x010, 0x00f, 0x00f, 0x00e, 0x00d, 0x00d, 0x00c, 0x00c,
    0x00b, 0x00a, 0x00a, 0x009, 0x009, 0x008, 0x008, 0x007,
    0x007, 0x007, 0x006, 0x006, 0x005, 0x005, 0x005, 0x004,
    0x004, 0x004, 0x003, 0x003, 0x003, 0x002, 0x002, 0x002,
    0x002, 0x001, 0x001, 0x001, 0x001, 0x001, 0x001, 0x001,
    0x000, 0x000, 0x000, 0x000, 0x000, 0x000, 0x000, 0x000,
)

_EXP = (
    0x7fa, 0x7f5, 0x7ef, 0x7ea, 0x7e4, 0x7df, 0x7da, 0x7d4,
    0x7cf, 0x7c9, 0x7c4, 0x7bf, 0x7b9, 0x7b4, 0x7ae, 0x7a9,
    0x7a4, 0x79f, 0x799, 0x794, 0x78f, 0x78a, 0x784, 0x77f,
    0x77a, 0x775, 0x770, 0x76a, 0x765, 0x760, 0x75b, 0x756,
    0x751, 0x74c, 0x747, 0x742, 0x73d, 0x738, 0x733, 0x72e,
    0x729, 0x724, 0x71f, 0x71a, 0x715, 0x710, 0x70b, 0x706,
    0x702, 0x6fd, 0x6f8, 0x6f3, 0x6ee, 0x6e9, 0x6e5, 0x6e0,
    0x6db, 0x6d6, 0x6d2, 0x6cd, 0x6c8, 0x6c4, 0x6bf, 0x6ba,
    0x6b5, 0x6b1, 0x6ac, 0x6a8, 0x6a3, 0x69e, 0x69a, 0x695,
    0x691, 0x68c, 0x688, 0x683, 0x67f, 0x67a, 0x676, 0x671,
    0x66d, 0x668, 0x664, 0x65f, 0x65b, 0x657, 0x652, 0x64e,
    0x649, 0x645, 0x641, 0x63c, 0x638, 0x634, 0x630, 0x62b,
    0x627, 0x623, 0x61e, 0x61a, 0x616, 0x612, 0x60e, 0x609,
    0x605, 0x601, 0x5fd, 0x5f9, 0x5f5, 0x5f0, 0x5ec, 0x5e8,
    0x5e4, 0x5e0, 0x5dc, 0x5d8, 0x5d4, 0x5d0, 0x5cc, 0x5c8,
    0x5c4, 0x5c0, 0x5bc, 0x5b8, 0x5b4, 0x5b0, 0x5ac, 0x5a8,
    0x5a4, 0x5a0, 0x59c, 0x599, 0x595, 0x591, 0x58d, 0x589,
    0x585, 0x581, 0x57e, 0x57a, 0x576, 0x572, 0x56f, 0x56b,
    0x567, 0x563, 0x560, 0x55c, 0x558, 0x554, 0x551, 0x54d,
    0x549, 0x546, 0x542, 0x53e, 0x53b, 0x537, 0x534, 0x530,
    0x52c, 0x529, 0x525, 0x522, 0x51e, 0x51b, 0x517, 0x514,
    0x510, 0x50c, 0x509, 0x506, 0x502, 0x4ff, 0x4fb, 0x4f8,
    0x4f4, 0x4f1, 0x4ed, 0x4ea, 0x4e7, 0x4e3, 0x4e0, 0x4dc,
    0x4d9, 0x4d6, 0x4d2, 0x4cf, 0x4cc, 0x4c8, 0x4c5, 0x4c2,
    0x4be, 0x4bb, 0x4b8, 0x4b5, 0x4b1, 0x4ae, 0x4ab, 0x4a8,
    0x4a4, 0x4a1, 0x49e, 0x49b, 0x498, 0x494, 0x491, 0x48e,
    0x48b, 0x488, 0x485, 0x482, 0x47e, 0x47b, 0x478, 0x475,
    0x472, 0x46f, 0x46c, 0x469, 0x466, 0x463, 0x460, 0x45d,
    0x45a, 0x457, 0x454, 0x451, 0x44e, 0x44b, 0x448, 0x445,
    0x442, 0x43f, 0x43c, 0x439, 0x436, 0x433, 0x430, 0x42d,
    0x42a, 0x428, 0x425, 0x422, 0x41f, 0x41c, 0x419, 0x416,
    0x414, 0x411, 0x40e, 0x40b, 0x408, 0x406, 0x403, 0x400,
)

_MT = (1, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 20, 24, 24, 30, 30)
_KSL = (0, 32, 40, 45, 48, 51, 53, 55, 56, 58, 59, 60, 61, 62, 63, 64)
_KSLSHIFT = (8, 1, 2, 0)
_EG_INCSTEP = ((0, 0, 0, 0), (1, 0, 0, 0), (1, 0, 1, 0), (1, 1, 1, 0))
_AD_SLOT = (0, 1, 2, 3, 4, 5, -1, -1, 6, 7, 8, 9, 10, 11, -1, -1, 12, 13, 14, 15, 16, 17, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1)
_CH_SLOT = (0, 1, 2, 6, 7, 8, 12, 13, 14, 18, 19, 20, 24, 25, 26, 30, 31, 32)


# PERF: flattened output tables — the eight EnvelopeCalcSin* functions each
# reduce to (magnitude-level, negate) as pure functions of the 10-bit phase,
# and EnvelopeCalcExp to one lookup in a level-indexed table with the clamp
# baked in.  Built here mechanically FROM the same definitions the direct
# translation used; the golden/differential tests prove the equivalence.
_EXPFULL = tuple((_EXP[(l if l <= 0x1FFF else 0x1FFF) & 0xFF] << 1)
                 >> ((l if l <= 0x1FFF else 0x1FFF) >> 8) for l in range(0x4000))


def _wf_tables():
    mags, negs = [], []
    for wf in range(8):
        mag = [0] * 0x400
        neg = [0] * 0x400
        for p in range(0x400):
            if wf == 0:
                m = _LOGSIN[(p & 0xFF) ^ 0xFF] if p & 0x100 else _LOGSIN[p & 0xFF]
                n = p & 0x200
            elif wf == 1:
                m = 0x1000 if p & 0x200 else (
                    _LOGSIN[(p & 0xFF) ^ 0xFF] if p & 0x100 else _LOGSIN[p & 0xFF])
                n = 0
            elif wf == 2:
                m = _LOGSIN[(p & 0xFF) ^ 0xFF] if p & 0x100 else _LOGSIN[p & 0xFF]
                n = 0
            elif wf == 3:
                m = 0x1000 if p & 0x100 else _LOGSIN[p & 0xFF]
                n = 0
            elif wf == 4:
                if p & 0x200:
                    m = 0x1000
                elif p & 0x80:
                    m = _LOGSIN[((p ^ 0xFF) << 1) & 0xFF]
                else:
                    m = _LOGSIN[(p << 1) & 0xFF]
                n = (p & 0x300) == 0x100
            elif wf == 5:
                if p & 0x200:
                    m = 0x1000
                elif p & 0x80:
                    m = _LOGSIN[((p ^ 0xFF) << 1) & 0xFF]
                else:
                    m = _LOGSIN[(p << 1) & 0xFF]
                n = 0
            elif wf == 6:
                m = 0
                n = p & 0x200
            else:
                m = (((p & 0x1FF) ^ 0x1FF) << 3) if p & 0x200 else (p << 3)
                n = p & 0x200
            mag[p] = m
            neg[p] = 1 if n else 0
        mags.append(tuple(mag))
        negs.append(tuple(neg))
    return tuple(mags), tuple(negs)


_WF_MAG, _WF_NEG = _wf_tables()


def _cdiv(a: int, b: int) -> int:
    """C integer division: truncate toward zero (Python // floors)."""
    q = a // b
    if q < 0 and q * b != a:
        q += 1
    return q




class _Slot:
    """opl3_slot.  ``out``/``fbmod`` live in the chip's shared value list
    ``sv`` (C pointer targets become indices): slot ``i``'s out is
    ``sv[1 + i]``, its fbmod is ``sv[37 + i]``; ``sv[0]`` is zeromod."""

    __slots__ = (
        "channel", "chip", "prout", "eg_rout", "eg_out", "eg_gen",
        "trem_on", "reg_vib", "reg_type", "reg_ksr", "reg_mult", "reg_ksl",
        "reg_tl", "reg_ar", "reg_dr", "reg_sl", "reg_rr", "reg_wf", "key",
        "pg_reset", "pg_phase", "pg_phase_out", "slot_num", "mod_idx", "is_rhythm",
        "eg_ksl", "out_idx", "fb_idx",
    )

    def __init__(self, chip: "OPL3", num: int) -> None:
        self.chip = chip
        self.channel: "_Channel" | None = None
        self.slot_num = num
        self.is_rhythm = num in (13, 16, 17)  # hh/sd/tc: phase feeds the rhythm generators
        self.out_idx = 1 + num
        self.fb_idx = 37 + num
        self.mod_idx = 0            # &chip->zeromod
        self.prout = 0
        self.eg_rout = 0x1FF
        self.eg_out = 0x1FF
        self.eg_gen = _EG_RELEASE
        self.eg_ksl = 0
        self.trem_on = False        # trem -> &chip->zeromod
        self.reg_vib = 0
        self.reg_type = 0
        self.reg_ksr = 0
        self.reg_mult = 0
        self.reg_ksl = 0
        self.reg_tl = 0
        self.reg_ar = 0
        self.reg_dr = 0
        self.reg_sl = 0
        self.reg_rr = 0
        self.reg_wf = 0
        self.key = 0
        self.pg_reset = 0
        self.pg_phase = 0
        self.pg_phase_out = 0


class _Channel:
    """opl3_channel.  ``out`` (four output taps) are indices into ``sv``."""

    __slots__ = ("slotz", "pair", "chip", "out_idx", "chtype", "f_num",
                 "block", "fb", "con", "alg", "ksv", "cha", "chb", "chc",
                 "chd", "ch_num")

    def __init__(self, chip: "OPL3", num: int) -> None:
        self.chip = chip
        self.ch_num = num
        self.slotz: list[_Slot] = []
        self.pair: "_Channel" | None = None
        self.out_idx = [0, 0, 0, 0]
        self.chtype = _CH_2OP
        self.f_num = 0
        self.block = 0
        self.fb = 0
        self.con = 0
        self.alg = 0
        self.ksv = 0
        self.cha = 0xFFFF
        self.chb = 0xFFFF
        self.chc = 0
        self.chd = 0


class OPL3:
    """One Nuked-OPL3 chip.

    After :meth:`reset` the chip is in OPL2 (YM3812) compatible mode; writing
    the OPL3 "new" bit (register ``0x105``) enables full OPL3 mode.
    ``generate_*`` return raw little-endian ``int16`` PCM bytes.
    """

    def __init__(self, sample_rate: int = OPL_NATIVE_RATE) -> None:
        self.sample_rate = int(sample_rate)
        self.reset()

    # --- OPL3_Reset -----------------------------------------------------------------

    def reset(self, sample_rate: int | None = None) -> None:
        if sample_rate is not None:
            self.sample_rate = int(sample_rate)
        # sv: shared int value list = C pointer targets.
        # sv[0] = zeromod, sv[1+i] = slot[i].out, sv[37+i] = slot[i].fbmod.
        self.sv = [0] * 73
        self.slot = [_Slot(self, i) for i in range(36)]
        self.channel = [_Channel(self, i) for i in range(18)]
        self.timer = 0
        self.eg_timer = 0
        self.eg_timerrem = 0
        self.eg_state = 0
        self.eg_add = 0
        self.eg_timer_lo = 0
        self.newm = 0
        self.nts = 0
        self.rhy = 0
        self.vibpos = 0
        self.vibshift = 1
        self.tremolo = 0
        self.tremolopos = 0
        self.tremoloshift = 4
        self.noise = 1
        # PERF: OPL2-era programs never write bank 1 (reg >= 0x100), so slots
        # 18..35 provably remain in their reset state (phase 0, envelope off,
        # out 0) forever.  While dormant, _generate4ch skips their processing
        # wholesale and only advances the noise LFSR the 18 steps they would
        # have advanced it (their ONLY global side effect; none of them is a
        # rhythm slot).  Any bank-1 write clears this permanently.
        self.bank1_dormant = True
        self.mixbuff = [0, 0, 0, 0]
        self.rm_hh_bit2 = 0
        self.rm_hh_bit3 = 0
        self.rm_hh_bit7 = 0
        self.rm_hh_bit8 = 0
        self.rm_tc_bit3 = 0
        self.rm_tc_bit5 = 0
        # OPL3L resampler
        self.rateratio = (self.sample_rate << _RSM_FRAC) // OPL_NATIVE_RATE
        self.samplecnt = 0
        self.oldsamples = [0, 0, 0, 0]
        self.samples = [0, 0, 0, 0]
        # Buffered write queue
        self.writebuf_samplecnt = 0
        self.writebuf_cur = 0
        self.writebuf_last = 0
        self.writebuf_lasttime = 0
        self.wb_time = [0] * _WRITEBUF_SIZE
        self.wb_reg = [0] * _WRITEBUF_SIZE
        self.wb_data = [0] * _WRITEBUF_SIZE

        for channum in range(18):
            channel = self.channel[channum]
            local_ch_slot = _CH_SLOT[channum]
            channel.slotz = [self.slot[local_ch_slot], self.slot[local_ch_slot + 3]]
            self.slot[local_ch_slot].channel = channel
            self.slot[local_ch_slot + 3].channel = channel
            if (channum % 9) < 3:
                channel.pair = self.channel[channum + 3]
            elif (channum % 9) < 6:
                channel.pair = self.channel[channum - 3]
            self._channel_setup_alg(channel)

    # --- Envelope generator ---------------------------------------------------------

    def _envelope_update_ksl(self, slot: _Slot) -> None:
        """OPL3_EnvelopeUpdateKSL"""
        channel = slot.channel
        ksl = (_KSL[channel.f_num >> 6] << 2) - ((0x08 - channel.block) << 5)
        if ksl < 0:
            ksl = 0
        slot.eg_ksl = ksl

    def _envelope_calc(self, slot: _Slot) -> None:
        """OPL3_EnvelopeCalc"""
        slot.eg_out = (slot.eg_rout + (slot.reg_tl << 2)
                       + (slot.eg_ksl >> _KSLSHIFT[slot.reg_ksl])
                       + (self.tremolo if slot.trem_on else 0))
        reg_rate = 0
        reset = 0
        if slot.key and slot.eg_gen == _EG_RELEASE:
            reset = 1
            reg_rate = slot.reg_ar
        else:
            eg_gen = slot.eg_gen
            if eg_gen == _EG_ATTACK:
                reg_rate = slot.reg_ar
            elif eg_gen == _EG_DECAY:
                reg_rate = slot.reg_dr
            elif eg_gen == _EG_SUSTAIN:
                if not slot.reg_type:
                    reg_rate = slot.reg_rr
            else:  # release
                reg_rate = slot.reg_rr
        slot.pg_reset = reset
        ks = slot.channel.ksv >> ((slot.reg_ksr ^ 1) << 1)
        nonzero = reg_rate != 0
        rate = ks + (reg_rate << 2)
        rate_hi = rate >> 2
        rate_lo = rate & 0x03
        if rate_hi & 0x10:
            rate_hi = 0x0F
        shift = 0
        if nonzero:
            if rate_hi < 12:
                if self.eg_state:
                    eg_shift = rate_hi + self.eg_add
                    if eg_shift == 12:
                        shift = 1
                    elif eg_shift == 13:
                        shift = (rate_lo >> 1) & 0x01
                    elif eg_shift == 14:
                        shift = rate_lo & 0x01
            else:
                shift = (rate_hi & 0x03) + _EG_INCSTEP[rate_lo][self.eg_timer_lo]
                if shift & 0x04:
                    shift = 0x03
                if not shift:
                    shift = self.eg_state
        eg_rout = slot.eg_rout
        eg_inc = 0
        eg_off = (slot.eg_rout & 0x1F8) == 0x1F8
        # Instant attack
        if reset and rate_hi == 0x0F:
            eg_rout = 0x00
        if slot.eg_gen != _EG_ATTACK and not reset and eg_off:
            eg_rout = 0x1FF
        eg_gen = slot.eg_gen
        if eg_gen == _EG_ATTACK:
            if not slot.eg_rout:
                slot.eg_gen = _EG_DECAY
            elif slot.key and shift > 0 and rate_hi != 0x0F:
                eg_inc = ~slot.eg_rout >> (4 - shift)
        elif eg_gen == _EG_DECAY:
            if (slot.eg_rout >> 4) == slot.reg_sl:
                slot.eg_gen = _EG_SUSTAIN
            elif not eg_off and not reset and shift > 0:
                eg_inc = 1 << (shift - 1)
        else:  # sustain or release
            if not eg_off and not reset and shift > 0:
                eg_inc = 1 << (shift - 1)
        slot.eg_rout = (eg_rout + eg_inc) & 0x1FF
        # Key off
        if reset:
            slot.eg_gen = _EG_ATTACK
        if not slot.key:
            slot.eg_gen = _EG_RELEASE

    # --- Phase generator ------------------------------------------------------------

    # --- Slot -----------------------------------------------------------------------

    def _slot_write_20(self, slot: _Slot, data: int) -> None:
        slot.trem_on = bool((data >> 7) & 0x01)
        slot.reg_vib = (data >> 6) & 0x01
        slot.reg_type = (data >> 5) & 0x01
        slot.reg_ksr = (data >> 4) & 0x01
        slot.reg_mult = data & 0x0F

    def _slot_write_40(self, slot: _Slot, data: int) -> None:
        slot.reg_ksl = (data >> 6) & 0x03
        slot.reg_tl = data & 0x3F
        self._envelope_update_ksl(slot)

    def _slot_write_60(self, slot: _Slot, data: int) -> None:
        slot.reg_ar = (data >> 4) & 0x0F
        slot.reg_dr = data & 0x0F

    def _slot_write_80(self, slot: _Slot, data: int) -> None:
        slot.reg_sl = (data >> 4) & 0x0F
        if slot.reg_sl == 0x0F:
            slot.reg_sl = 0x1F
        slot.reg_rr = data & 0x0F

    def _slot_write_e0(self, slot: _Slot, data: int) -> None:
        slot.reg_wf = data & 0x07
        if self.newm == 0x00:
            slot.reg_wf &= 0x03

    def _process_slot(self, slot: _Slot) -> None:
        """OPL3_ProcessSlot = SlotCalcFB + EnvelopeCalc + PhaseGenerate + SlotGenerate.

        Single flat method (no sub-calls; waveform+exp are the precomputed
        _WF_MAG/_WF_NEG/_EXPFULL tables) with two provable fast lanes -- see
        the comments.  Byte-exactness of all paths is proven by the golden
        and differential tests.
        """
        sv = self.sv
        # PERF fast lane 1 -- a RELEASED slot (key off, release stage, envelope
        # at 0x1FF) is frozen: the EG equations keep eg_rout/eg_gen fixed until
        # key-on, and the output magnitude is 0 for every waveform (level >=
        # 0xF00 shifts the exp ROM to zero), so only the 0/-1 DC sign (a real
        # chip quirk) depends on the still-advancing phase.  Rhythm slots
        # (hh/sd/tc) always take the full path -- their phase bits feed the
        # rhythm generators.
        if slot.key == 0 and slot.eg_rout == 0x1FF and slot.eg_gen == 3 and not slot.is_rhythm:
            out_idx = slot.out_idx
            out = sv[out_idx]
            channel = slot.channel
            fb = channel.fb
            if fb:
                sv[slot.fb_idx] = (slot.prout + out) >> (0x09 - fb)
            else:
                sv[slot.fb_idx] = 0
            slot.prout = out
            f_num = channel.f_num
            if slot.reg_vib:
                rng = (f_num >> 7) & 7
                vibpos = self.vibpos
                if not (vibpos & 3):
                    rng = 0
                elif vibpos & 1:
                    rng >>= 1
                rng >>= self.vibshift
                if vibpos & 4:
                    rng = -rng
                f_num = (f_num + rng) & 0xFFFF
            pg_phase = slot.pg_phase
            phase = (pg_phase >> 9) & 0xFFFF
            slot.pg_phase = (pg_phase
                             + (((f_num << channel.block) >> 1)
                                * _MT[slot.reg_mult] >> 1)) & 0xFFFFFFFF
            slot.pg_phase_out = phase
            noise = self.noise
            self.noise = (noise >> 1) | ((((noise >> 14) ^ noise) & 0x01) << 22)
            sv[out_idx] = -1 if _WF_NEG[slot.reg_wf][(phase + sv[slot.mod_idx]) & 0x3FF] else 0
            return

        # OPL3_SlotCalcFB (shared by the remaining paths)
        out_idx = slot.out_idx
        out = sv[out_idx]
        channel = slot.channel
        fb = channel.fb
        if fb:
            sv[slot.fb_idx] = (slot.prout + out) >> (0x09 - fb)
        else:
            sv[slot.fb_idx] = 0
        slot.prout = out

        # PERF fast lane 2 -- a SOUNDING but envelope-static slot: key on, in
        # SUSTAIN, EGT set (reg_type != 0 -> sustain rate 0), envelope outside
        # the 0x1F8 off-band.  The EG machinery changes nothing this sample
        # (rate 0 -> shift 0 -> inc 0; no transition, no reset); only eg_out
        # (carrying the per-sample tremolo term) must be recomputed.
        if slot.key and slot.eg_gen == 2 and slot.reg_type and (slot.eg_rout & 0x1F8) != 0x1F8:
            eg_out = (slot.eg_rout + (slot.reg_tl << 2)
                      + (slot.eg_ksl >> _KSLSHIFT[slot.reg_ksl])
                      + (self.tremolo if slot.trem_on else 0))
            slot.eg_out = eg_out
            reset = 0
        else:
            # OPL3_EnvelopeCalc (full)
            eg_rout_cur = slot.eg_rout
            eg_out = (eg_rout_cur + (slot.reg_tl << 2)
                      + (slot.eg_ksl >> _KSLSHIFT[slot.reg_ksl])
                      + (self.tremolo if slot.trem_on else 0))
            slot.eg_out = eg_out
            key = slot.key
            eg_gen = slot.eg_gen
            if key and eg_gen == 3:
                reset = 1
                reg_rate = slot.reg_ar
            else:
                reset = 0
                if eg_gen == 0:
                    reg_rate = slot.reg_ar
                elif eg_gen == 1:
                    reg_rate = slot.reg_dr
                elif eg_gen == 2:
                    reg_rate = 0 if slot.reg_type else slot.reg_rr
                else:
                    reg_rate = slot.reg_rr
            rate = (channel.ksv >> ((slot.reg_ksr ^ 1) << 1)) + (reg_rate << 2)
            rate_hi = rate >> 2
            if rate_hi & 0x10:
                rate_hi = 0x0F
            rate_lo = rate & 0x03
            shift = 0
            if reg_rate:
                if rate_hi < 12:
                    if self.eg_state:
                        eg_shift = rate_hi + self.eg_add
                        if eg_shift == 12:
                            shift = 1
                        elif eg_shift == 13:
                            shift = (rate_lo >> 1) & 0x01
                        elif eg_shift == 14:
                            shift = rate_lo & 0x01
                else:
                    shift = (rate_hi & 0x03) + _EG_INCSTEP[rate_lo][self.eg_timer_lo]
                    if shift & 0x04:
                        shift = 0x03
                    if not shift:
                        shift = self.eg_state
            eg_rout = eg_rout_cur
            eg_inc = 0
            eg_off = (eg_rout_cur & 0x1F8) == 0x1F8
            if reset and rate_hi == 0x0F:  # instant attack
                eg_rout = 0x00
            if eg_gen != 0 and not reset and eg_off:  # envelope off
                eg_rout = 0x1FF
            if eg_gen == 0:
                if not eg_rout_cur:
                    slot.eg_gen = 1
                elif key and shift > 0 and rate_hi != 0x0F:
                    eg_inc = ~eg_rout_cur >> (4 - shift)
            elif eg_gen == 1:
                if (eg_rout_cur >> 4) == slot.reg_sl:
                    slot.eg_gen = 2
                elif not eg_off and not reset and shift > 0:
                    eg_inc = 1 << (shift - 1)
            else:  # sustain or release
                if not eg_off and not reset and shift > 0:
                    eg_inc = 1 << (shift - 1)
            slot.eg_rout = (eg_rout + eg_inc) & 0x1FF
            if reset:  # key on
                slot.eg_gen = 0
            if not key:  # key off
                slot.eg_gen = 3

        # OPL3_PhaseGenerate (shared full path; `reset` from the envelope)
        f_num = channel.f_num
        if slot.reg_vib:
            rng = (f_num >> 7) & 7
            vibpos = self.vibpos
            if not (vibpos & 3):
                rng = 0
            elif vibpos & 1:
                rng >>= 1
            rng >>= self.vibshift
            if vibpos & 4:
                rng = -rng
            f_num = (f_num + rng) & 0xFFFF
        pg_phase = slot.pg_phase
        phase = (pg_phase >> 9) & 0xFFFF
        if reset:
            pg_phase = 0
        slot.pg_phase = (pg_phase
                         + (((f_num << channel.block) >> 1)
                            * _MT[slot.reg_mult] >> 1)) & 0xFFFFFFFF
        phase_out = phase
        slot_num = slot.slot_num
        noise = self.noise
        if slot_num == 13:  # hh
            self.rm_hh_bit2 = (phase >> 2) & 1
            self.rm_hh_bit3 = (phase >> 3) & 1
            self.rm_hh_bit7 = (phase >> 7) & 1
            self.rm_hh_bit8 = (phase >> 8) & 1
        if self.rhy & 0x20:
            if slot_num == 17:  # tc
                self.rm_tc_bit3 = (phase >> 3) & 1
                self.rm_tc_bit5 = (phase >> 5) & 1
            rm_xor = ((self.rm_hh_bit2 ^ self.rm_hh_bit7)
                      | (self.rm_hh_bit3 ^ self.rm_tc_bit5)
                      | (self.rm_tc_bit3 ^ self.rm_tc_bit5))
            if slot_num == 13:  # hh
                phase_out = rm_xor << 9
                if rm_xor ^ (noise & 1):
                    phase_out |= 0xD0
                else:
                    phase_out |= 0x34
            elif slot_num == 16:  # sd
                phase_out = ((self.rm_hh_bit8 << 9)
                             | ((self.rm_hh_bit8 ^ (noise & 1)) << 8))
            elif slot_num == 17:  # tc
                phase_out = (rm_xor << 9) | 0x80
        slot.pg_phase_out = phase_out
        self.noise = (noise >> 1) | ((((noise >> 14) ^ noise) & 0x01) << 22)

        # OPL3_SlotGenerate via the flattened tables
        wf = slot.reg_wf
        pp = (phase_out + sv[slot.mod_idx]) & 0x3FF
        m = _EXPFULL[_WF_MAG[wf][pp] + (eg_out << 3)]
        sv[out_idx] = -m - 1 if _WF_NEG[wf][pp] else m

    # --- Channel --------------------------------------------------------------------

    def _channel_update_rhythm(self, data: int) -> None:
        """OPL3_ChannelUpdateRhythm"""
        self.rhy = data & 0x3F
        if self.rhy & 0x20:
            channel6 = self.channel[6]
            channel7 = self.channel[7]
            channel8 = self.channel[8]
            channel6.out_idx = [channel6.slotz[1].out_idx, channel6.slotz[1].out_idx, 0, 0]
            channel7.out_idx = [channel7.slotz[0].out_idx, channel7.slotz[0].out_idx,
                                channel7.slotz[1].out_idx, channel7.slotz[1].out_idx]
            channel8.out_idx = [channel8.slotz[0].out_idx, channel8.slotz[0].out_idx,
                                channel8.slotz[1].out_idx, channel8.slotz[1].out_idx]
            for chnum in range(6, 9):
                self.channel[chnum].chtype = _CH_DRUM
            self._channel_setup_alg(channel6)
            self._channel_setup_alg(channel7)
            self._channel_setup_alg(channel8)
            # hh
            if self.rhy & 0x01:
                channel7.slotz[0].key |= _EGK_DRUM
            else:
                channel7.slotz[0].key &= ~_EGK_DRUM
            # tc
            if self.rhy & 0x02:
                channel8.slotz[1].key |= _EGK_DRUM
            else:
                channel8.slotz[1].key &= ~_EGK_DRUM
            # tom
            if self.rhy & 0x04:
                channel8.slotz[0].key |= _EGK_DRUM
            else:
                channel8.slotz[0].key &= ~_EGK_DRUM
            # sd
            if self.rhy & 0x08:
                channel7.slotz[1].key |= _EGK_DRUM
            else:
                channel7.slotz[1].key &= ~_EGK_DRUM
            # bd
            if self.rhy & 0x10:
                channel6.slotz[0].key |= _EGK_DRUM
                channel6.slotz[1].key |= _EGK_DRUM
            else:
                channel6.slotz[0].key &= ~_EGK_DRUM
                channel6.slotz[1].key &= ~_EGK_DRUM
        else:
            for chnum in range(6, 9):
                channel = self.channel[chnum]
                channel.chtype = _CH_2OP
                self._channel_setup_alg(channel)
                channel.slotz[0].key &= ~_EGK_DRUM
                channel.slotz[1].key &= ~_EGK_DRUM

    def _channel_write_a0(self, channel: _Channel, data: int) -> None:
        if self.newm and channel.chtype == _CH_4OP2:
            return
        channel.f_num = (channel.f_num & 0x300) | data
        channel.ksv = ((channel.block << 1)
                       | ((channel.f_num >> (0x09 - self.nts)) & 0x01))
        self._envelope_update_ksl(channel.slotz[0])
        self._envelope_update_ksl(channel.slotz[1])
        if self.newm and channel.chtype == _CH_4OP:
            channel.pair.f_num = channel.f_num
            channel.pair.ksv = channel.ksv
            self._envelope_update_ksl(channel.pair.slotz[0])
            self._envelope_update_ksl(channel.pair.slotz[1])

    def _channel_write_b0(self, channel: _Channel, data: int) -> None:
        if self.newm and channel.chtype == _CH_4OP2:
            return
        channel.f_num = (channel.f_num & 0xFF) | ((data & 0x03) << 8)
        channel.block = (data >> 2) & 0x07
        channel.ksv = ((channel.block << 1)
                       | ((channel.f_num >> (0x09 - self.nts)) & 0x01))
        self._envelope_update_ksl(channel.slotz[0])
        self._envelope_update_ksl(channel.slotz[1])
        if self.newm and channel.chtype == _CH_4OP:
            channel.pair.f_num = channel.f_num
            channel.pair.block = channel.block
            channel.pair.ksv = channel.ksv
            self._envelope_update_ksl(channel.pair.slotz[0])
            self._envelope_update_ksl(channel.pair.slotz[1])

    def _channel_setup_alg(self, channel: _Channel) -> None:
        """OPL3_ChannelSetupAlg"""
        if channel.chtype == _CH_DRUM:
            if channel.ch_num in (7, 8):
                channel.slotz[0].mod_idx = 0
                channel.slotz[1].mod_idx = 0
                return
            if channel.alg & 0x01:
                channel.slotz[0].mod_idx = channel.slotz[0].fb_idx
                channel.slotz[1].mod_idx = 0
            else:
                channel.slotz[0].mod_idx = channel.slotz[0].fb_idx
                channel.slotz[1].mod_idx = channel.slotz[0].out_idx
            return
        if channel.alg & 0x08:
            return
        if channel.alg & 0x04:
            pair = channel.pair
            pair.out_idx = [0, 0, 0, 0]
            alg = channel.alg & 0x03
            if alg == 0x00:
                pair.slotz[0].mod_idx = pair.slotz[0].fb_idx
                pair.slotz[1].mod_idx = pair.slotz[0].out_idx
                channel.slotz[0].mod_idx = pair.slotz[1].out_idx
                channel.slotz[1].mod_idx = channel.slotz[0].out_idx
                channel.out_idx = [channel.slotz[1].out_idx, 0, 0, 0]
            elif alg == 0x01:
                pair.slotz[0].mod_idx = pair.slotz[0].fb_idx
                pair.slotz[1].mod_idx = pair.slotz[0].out_idx
                channel.slotz[0].mod_idx = 0
                channel.slotz[1].mod_idx = channel.slotz[0].out_idx
                channel.out_idx = [pair.slotz[1].out_idx, channel.slotz[1].out_idx, 0, 0]
            elif alg == 0x02:
                pair.slotz[0].mod_idx = pair.slotz[0].fb_idx
                pair.slotz[1].mod_idx = 0
                channel.slotz[0].mod_idx = pair.slotz[1].out_idx
                channel.slotz[1].mod_idx = channel.slotz[0].out_idx
                channel.out_idx = [pair.slotz[0].out_idx, channel.slotz[1].out_idx, 0, 0]
            else:
                pair.slotz[0].mod_idx = pair.slotz[0].fb_idx
                pair.slotz[1].mod_idx = 0
                channel.slotz[0].mod_idx = pair.slotz[1].out_idx
                channel.slotz[1].mod_idx = 0
                channel.out_idx = [pair.slotz[0].out_idx, channel.slotz[0].out_idx,
                                   channel.slotz[1].out_idx, 0]
        else:
            if channel.alg & 0x01:
                channel.slotz[0].mod_idx = channel.slotz[0].fb_idx
                channel.slotz[1].mod_idx = 0
                channel.out_idx = [channel.slotz[0].out_idx, channel.slotz[1].out_idx, 0, 0]
            else:
                channel.slotz[0].mod_idx = channel.slotz[0].fb_idx
                channel.slotz[1].mod_idx = channel.slotz[0].out_idx
                channel.out_idx = [channel.slotz[1].out_idx, 0, 0, 0]

    def _channel_update_alg(self, channel: _Channel) -> None:
        """OPL3_ChannelUpdateAlg"""
        channel.alg = channel.con
        if self.newm:
            if channel.chtype == _CH_4OP:
                channel.pair.alg = 0x04 | (channel.con << 1) | channel.pair.con
                channel.alg = 0x08
                self._channel_setup_alg(channel.pair)
            elif channel.chtype == _CH_4OP2:
                channel.alg = 0x04 | (channel.pair.con << 1) | channel.con
                channel.pair.alg = 0x08
                self._channel_setup_alg(channel)
            else:
                self._channel_setup_alg(channel)
        else:
            self._channel_setup_alg(channel)

    def _channel_write_c0(self, channel: _Channel, data: int) -> None:
        channel.fb = (data & 0x0E) >> 1
        channel.con = data & 0x01
        self._channel_update_alg(channel)
        if self.newm:
            channel.cha = 0xFFFF if (data >> 4) & 0x01 else 0
            channel.chb = 0xFFFF if (data >> 5) & 0x01 else 0
            channel.chc = 0xFFFF if (data >> 6) & 0x01 else 0
            channel.chd = 0xFFFF if (data >> 7) & 0x01 else 0
        else:
            channel.cha = channel.chb = 0xFFFF
            channel.chc = channel.chd = 0

    def _channel_key_on(self, channel: _Channel) -> None:
        if self.newm:
            if channel.chtype == _CH_4OP:
                channel.slotz[0].key |= _EGK_NORM
                channel.slotz[1].key |= _EGK_NORM
                channel.pair.slotz[0].key |= _EGK_NORM
                channel.pair.slotz[1].key |= _EGK_NORM
            elif channel.chtype in (_CH_2OP, _CH_DRUM):
                channel.slotz[0].key |= _EGK_NORM
                channel.slotz[1].key |= _EGK_NORM
        else:
            channel.slotz[0].key |= _EGK_NORM
            channel.slotz[1].key |= _EGK_NORM

    def _channel_key_off(self, channel: _Channel) -> None:
        if self.newm:
            if channel.chtype == _CH_4OP:
                channel.slotz[0].key &= ~_EGK_NORM
                channel.slotz[1].key &= ~_EGK_NORM
                channel.pair.slotz[0].key &= ~_EGK_NORM
                channel.pair.slotz[1].key &= ~_EGK_NORM
            elif channel.chtype in (_CH_2OP, _CH_DRUM):
                channel.slotz[0].key &= ~_EGK_NORM
                channel.slotz[1].key &= ~_EGK_NORM
        else:
            channel.slotz[0].key &= ~_EGK_NORM
            channel.slotz[1].key &= ~_EGK_NORM

    def _channel_set_4op(self, data: int) -> None:
        """OPL3_ChannelSet4Op"""
        for bit in range(6):
            chnum = bit
            if bit >= 3:
                chnum += 9 - 3
            if (data >> bit) & 0x01:
                self.channel[chnum].chtype = _CH_4OP
                self.channel[chnum + 3].chtype = _CH_4OP2
                self._channel_update_alg(self.channel[chnum])
            else:
                self.channel[chnum].chtype = _CH_2OP
                self.channel[chnum + 3].chtype = _CH_2OP
                self._channel_update_alg(self.channel[chnum])
                self._channel_update_alg(self.channel[chnum + 3])

    # --- Core sample generation (OPL3_Generate4Ch, quirk-delay configuration) --------

    def _generate4ch(self, buf4: list) -> None:
        sv = self.sv
        mixbuff = self.mixbuff
        channels = self.channel
        slots = self.slot
        process = self._process_slot
        # PERF: while bank 1 is dormant (never written), slots 18..35 are at
        # reset state (out 0, phase 0) — skip them and only advance the noise
        # LFSR the 18 steps they would have; channels 9..17 contribute exactly
        # 0 to both mixes.  See reset() for the proof sketch; the wake is any
        # bank-1 register write.
        dormant = self.bank1_dormant
        nch = 9 if dormant else 18

        buf4[1] = _clip(mixbuff[1])
        buf4[3] = _clip(mixbuff[3])

        for ii in range(15):
            process(slots[ii])

        mix0 = mix1 = 0
        for ii in range(nch):
            channel = channels[ii]
            o = channel.out_idx
            accm = sv[o[0]] + sv[o[1]] + sv[o[2]] + sv[o[3]]
            if channel.cha:
                mix0 += accm
            if channel.chc:
                mix1 += accm
        mixbuff[0] = mix0
        mixbuff[2] = mix1

        for ii in range(15, 18):
            process(slots[ii])

        buf4[0] = _clip(mixbuff[0])
        buf4[2] = _clip(mixbuff[2])

        if dormant:
            noise = self.noise
            for _ in range(18):  # the skipped slots' only global side effect
                noise = (noise >> 1) | ((((noise >> 14) ^ noise) & 0x01) << 22)
            self.noise = noise
        else:
            for ii in range(18, 33):
                process(slots[ii])

        mix0 = mix1 = 0
        for ii in range(nch):
            channel = channels[ii]
            o = channel.out_idx
            accm = sv[o[0]] + sv[o[1]] + sv[o[2]] + sv[o[3]]
            if channel.chb:
                mix0 += accm
            if channel.chd:
                mix1 += accm
        mixbuff[1] = mix0
        mixbuff[3] = mix1

        if not dormant:
            for ii in range(33, 36):
                process(slots[ii])

        if (self.timer & 0x3F) == 0x3F:
            self.tremolopos = (self.tremolopos + 1) % 210
        if self.tremolopos < 105:
            self.tremolo = self.tremolopos >> self.tremoloshift
        else:
            self.tremolo = (210 - self.tremolopos) >> self.tremoloshift

        if (self.timer & 0x3FF) == 0x3FF:
            self.vibpos = (self.vibpos + 1) & 7

        self.timer = (self.timer + 1) & 0xFFFF

        if self.eg_state:
            shift = 0
            eg_timer = self.eg_timer
            while shift < 13 and ((eg_timer >> shift) & 1) == 0:
                shift += 1
            self.eg_add = 0 if shift > 12 else shift + 1
            self.eg_timer_lo = eg_timer & 0x3

        if self.eg_timerrem or self.eg_state:
            if self.eg_timer == 0xFFFFFFFFF:
                self.eg_timer = 0
                self.eg_timerrem = 1
            else:
                self.eg_timer += 1
                self.eg_timerrem = 0

        self.eg_state ^= 1

        # Flush due buffered register writes.
        wb_time = self.wb_time
        wb_reg = self.wb_reg
        cur = self.writebuf_cur
        samplecnt = self.writebuf_samplecnt
        while wb_time[cur] <= samplecnt:
            reg = wb_reg[cur]
            if not (reg & 0x200):
                break
            wb_reg[cur] = reg & 0x1FF
            self.write_immediate(reg & 0x1FF, self.wb_data[cur])
            cur = (cur + 1) % _WRITEBUF_SIZE
        self.writebuf_cur = cur
        self.writebuf_samplecnt = samplecnt + 1

    def _generate4ch_resampled(self, buf4: list) -> None:
        """OPL3_Generate4ChResampled (the OPL3L linear resampler)"""
        rateratio = self.rateratio
        while self.samplecnt >= rateratio:
            self.oldsamples[0] = self.samples[0]
            self.oldsamples[1] = self.samples[1]
            self.oldsamples[2] = self.samples[2]
            self.oldsamples[3] = self.samples[3]
            self._generate4ch(self.samples)
            self.samplecnt -= rateratio
        samplecnt = self.samplecnt
        old = self.oldsamples
        new = self.samples
        buf4[0] = _cdiv(old[0] * (rateratio - samplecnt) + new[0] * samplecnt, rateratio)
        buf4[1] = _cdiv(old[1] * (rateratio - samplecnt) + new[1] * samplecnt, rateratio)
        buf4[2] = _cdiv(old[2] * (rateratio - samplecnt) + new[2] * samplecnt, rateratio)
        buf4[3] = _cdiv(old[3] * (rateratio - samplecnt) + new[3] * samplecnt, rateratio)
        self.samplecnt = samplecnt + (1 << _RSM_FRAC)

    # --- Register interface -----------------------------------------------------------

    def write_immediate(self, reg: int, value: int) -> None:
        """OPL3_WriteReg — apply a register write immediately."""
        reg = int(reg) & 0x1FF
        v = int(value) & 0xFF
        high = (reg >> 8) & 0x01
        if high:
            self.bank1_dormant = False  # bank 1 touched: slots 18..35 live from now on
        regm = reg & 0xFF
        group = regm & 0xF0
        if group == 0x00:
            if high:
                if (regm & 0x0F) == 0x04:
                    self._channel_set_4op(v)
                elif (regm & 0x0F) == 0x05:
                    self.newm = v & 0x01
            else:
                if (regm & 0x0F) == 0x08:
                    self.nts = (v >> 6) & 0x01
        elif group in (0x20, 0x30):
            if _AD_SLOT[regm & 0x1F] >= 0:
                self._slot_write_20(self.slot[18 * high + _AD_SLOT[regm & 0x1F]], v)
        elif group in (0x40, 0x50):
            if _AD_SLOT[regm & 0x1F] >= 0:
                self._slot_write_40(self.slot[18 * high + _AD_SLOT[regm & 0x1F]], v)
        elif group in (0x60, 0x70):
            if _AD_SLOT[regm & 0x1F] >= 0:
                self._slot_write_60(self.slot[18 * high + _AD_SLOT[regm & 0x1F]], v)
        elif group in (0x80, 0x90):
            if _AD_SLOT[regm & 0x1F] >= 0:
                self._slot_write_80(self.slot[18 * high + _AD_SLOT[regm & 0x1F]], v)
        elif group in (0xE0, 0xF0):
            if _AD_SLOT[regm & 0x1F] >= 0:
                self._slot_write_e0(self.slot[18 * high + _AD_SLOT[regm & 0x1F]], v)
        elif group == 0xA0:
            if (regm & 0x0F) < 9:
                self._channel_write_a0(self.channel[9 * high + (regm & 0x0F)], v)
        elif group == 0xB0:
            if regm == 0xBD and not high:
                self.tremoloshift = (((v >> 7) ^ 1) << 1) + 2
                self.vibshift = ((v >> 6) & 0x01) ^ 1
                self._channel_update_rhythm(v)
            elif (regm & 0x0F) < 9:
                channel = self.channel[9 * high + (regm & 0x0F)]
                self._channel_write_b0(channel, v)
                if v & 0x20:
                    self._channel_key_on(channel)
                else:
                    self._channel_key_off(channel)
        elif group == 0xC0:
            if (regm & 0x0F) < 9:
                self._channel_write_c0(self.channel[9 * high + (regm & 0x0F)], v)

    def write(self, reg: int, value: int) -> None:
        """OPL3_WriteRegBuffered — queue through the chip's timed write buffer.

        Models the real chip's short write latency; the correct entry point
        for time-ordered playback interleaved with ``generate``.
        """
        reg = int(reg) & 0x1FF
        v = int(value) & 0xFF
        last = self.writebuf_last
        if self.wb_reg[last] & 0x200:
            self.write_immediate(self.wb_reg[last] & 0x1FF, self.wb_data[last])
            self.writebuf_cur = (last + 1) % _WRITEBUF_SIZE
            self.writebuf_samplecnt = self.wb_time[last]
        self.wb_reg[last] = reg | 0x200
        self.wb_data[last] = v
        time1 = self.writebuf_lasttime + _WRITEBUF_DELAY
        time2 = self.writebuf_samplecnt
        if time1 < time2:
            time1 = time2
        self.wb_time[last] = time1
        self.writebuf_lasttime = time1
        self.writebuf_last = (last + 1) % _WRITEBUF_SIZE

    # --- PCM output -------------------------------------------------------------------

    def generate_stereo(self, num_frames: int) -> bytes:
        """Render interleaved stereo (L,R) little-endian int16 frames.

        The OPL3L resampler (OPL3_Generate4ChResampled) is inlined here for
        channels 0/1 — one Python call and a list round-trip per output frame
        removed; _generate4ch_resampled remains for the 4-channel API surface.
        """
        num_frames = max(0, int(num_frames))
        if num_frames == 0:
            return b""
        out = array("h", bytes(4 * num_frames))
        buf4 = [0, 0, 0, 0]
        generate = self._generate4ch
        rateratio = self.rateratio
        samplecnt = self.samplecnt
        old_s = self.oldsamples
        new_s = self.samples
        pos = 0
        for _ in range(num_frames):
            while samplecnt >= rateratio:
                old_s[0] = new_s[0]
                old_s[1] = new_s[1]
                old_s[2] = new_s[2]
                old_s[3] = new_s[3]
                generate(new_s)
                samplecnt -= rateratio
            k = rateratio - samplecnt
            n = old_s[0] * k + new_s[0] * samplecnt
            q = n // rateratio
            out[pos] = q + 1 if q < 0 and q * rateratio != n else q
            n = old_s[1] * k + new_s[1] * samplecnt
            q = n // rateratio
            out[pos + 1] = q + 1 if q < 0 and q * rateratio != n else q
            samplecnt += 1024  # 1 << RSM_FRAC
            pos += 2
        # Channels 2/3 of the resampler state must stay coherent for the
        # 4-channel API: their old/new samples were maintained by generate()
        # above; only the per-frame interpolation for them was skipped, which
        # touches no state.
        self.samplecnt = samplecnt
        if sys.byteorder == "big":  # pragma: no cover - x86/ARM LE everywhere we run
            out.byteswap()
        return out.tobytes()

    def generate_mono(self, num_frames: int) -> bytes:
        """Render mono int16 frames (the left channel; OPL2 mode is mono)."""
        stereo = self.generate_stereo(num_frames)
        if not stereo:
            return stereo
        return memoryview(stereo).cast("h")[0::2].tobytes()


def _clip(sample: int) -> int:
    """OPL3_ClipSample"""
    if sample > 32767:
        return 32767
    if sample < -32768:
        return -32768
    return sample

