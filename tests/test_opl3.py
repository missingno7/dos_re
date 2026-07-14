"""Oracle tests for the pure-Python Nuked-OPL3 translation (dos_re/opl3.py).

Two proof layers:

1. GOLDEN HASHES — sha1 of the PCM the compiled Nuked-OPL3 C reference
   (pynuked_opl3, upstream v1.8, default config) produced for fixed register
   scripts: silence, an OPL2 melodic note, full rhythm mode, the four OPL2
   waveforms with feedback sweeps, OPL3 new-mode 4-op/second-bank/stereo at
   44100 (exercising the OPL3L resampler), and three deterministic full
   register-space fuzz streams at three sample rates.  These run everywhere
   (CI included) with no C compiler.

2. DIRECT DIFFERENTIAL — when the optional pynuked_opl3 cffi build is
   importable, byte-compare both implementations over an additional fuzz
   stream not covered by the goldens.

The Python module was additionally proven byte-identical to the C build over
80 seconds of real captured SKYROADS AdLib music (3,670 register writes,
3.5M output frames) when it was introduced; that stream is game-derived so it
lives outside this game-free suite.
"""
from __future__ import annotations

import hashlib
import random

from dos_re.opl3 import OPL3, OPL_NATIVE_RATE

_GOLDEN = {
    "silence": "0631457264ff7f8d5fb1edc2c0211992a67c73e6",
    "opl2_note": "cdcece14f38a9a55847165af00b8a9645ec491a4",
    "rhythm": "8327961bdbae2691a151ec711752b6587e6e4cbd",
    "waveforms_fb": "75d100ff2cd35448db6838479327aeb1c9fc10c6",
    "opl3_4op_44100": "17c4833c8220b8ce1b64d740dd443b9a5d5b8f55",
    "fuzz_seed1_49716": "7a56e5b7ceaf39b829819de2b924b33a6861ba2b",
    "fuzz_seed2_44100": "2630bcf8eb9bb6bafde2e896809e3b2ba636b76b",
    "fuzz_seed3_8000": "29a4c0da4514209668a77131226aae4fb8eea02b",
}


def _fuzz_script(seed: int) -> list:
    rng = random.Random(seed)
    script = []
    for _ in range(600):
        if rng.random() < 0.75:
            script.append(("w", rng.randrange(0x200), rng.randrange(256)))
        else:
            script.append(("g", rng.randrange(1, 300), 0))
    script.append(("g", 2048, 0))
    return script


def _scripts():
    yield "silence", OPL_NATIVE_RATE, [("g", 2048, 0)]
    yield "opl2_note", OPL_NATIVE_RATE, [
        ("w", 0x20, 0x01), ("w", 0x40, 0x10), ("w", 0x60, 0xF0), ("w", 0x80, 0x77),
        ("w", 0xA0, 0x98), ("w", 0x23, 0x01), ("w", 0x43, 0x00), ("w", 0x63, 0xF0),
        ("w", 0x83, 0x77), ("w", 0xB0, 0x31), ("g", 4096, 0), ("w", 0xB0, 0x11),
        ("g", 2048, 0)]
    yield "rhythm", OPL_NATIVE_RATE, [
        ("w", 0xBD, 0xFF), ("w", 0xA6, 0x40), ("w", 0xB6, 0x0D), ("w", 0xA7, 0x91),
        ("w", 0xB7, 0x09), ("w", 0xA8, 0x37), ("w", 0xB8, 0x05), ("g", 4096, 0),
        ("w", 0xBD, 0x20), ("g", 1024, 0)]
    s4 = [("w", 0x01, 0x20)]
    for wf in range(4):
        s4 += [("w", 0xE0 + wf, wf), ("w", 0x20 + wf, 0x21), ("w", 0x40 + wf, 0x08),
               ("w", 0x60 + wf, 0xF4), ("w", 0x80 + wf, 0x53)]
    for ch in range(4):
        s4 += [("w", 0xC0 + ch, (ch << 1) | (ch & 1)), ("w", 0xA0 + ch, 0x41 + 37 * ch),
               ("w", 0xB0 + ch, 0x25 + (ch & 3))]
    s4 += [("g", 4096, 0)]
    yield "waveforms_fb", OPL_NATIVE_RATE, s4
    s5 = [("w", 0x105, 0x01), ("w", 0x104, 0x3F)]
    for ch in (0, 1, 2, 9, 10, 11):
        hb = 0x100 if ch >= 9 else 0
        c = ch % 9
        s5 += [("w", hb + 0x20 + c, 0x21), ("w", hb + 0x40 + c, 0x0B),
               ("w", hb + 0x60 + c, 0xE4), ("w", hb + 0x80 + c, 0x9B),
               ("w", hb + 0xE0 + c, (ch * 3) & 7),
               ("w", hb + 0xC0 + c, 0x30 | ((ch & 7) << 1)),
               ("w", hb + 0xA0 + c, 0x67 + ch * 23), ("w", hb + 0xB0 + c, 0x2C | (ch & 3))]
    s5 += [("g", 4096, 0), ("w", 0x105, 0x00), ("g", 1024, 0)]
    yield "opl3_4op_44100", 44100, s5
    yield "fuzz_seed1_49716", 49716, _fuzz_script(1)
    yield "fuzz_seed2_44100", 44100, _fuzz_script(2)
    yield "fuzz_seed3_8000", 8000, _fuzz_script(3)


def _run(chip, script) -> bytes:
    out = bytearray()
    for op, a, b in script:
        if op == "w":
            chip.write(a, b)
        else:
            out += chip.generate_stereo(a)
    return bytes(out)


def test_pcm_matches_c_reference_goldens():
    for name, rate, script in _scripts():
        pcm = _run(OPL3(sample_rate=rate), script)
        digest = hashlib.sha1(pcm).hexdigest()
        assert digest == _GOLDEN[name], (
            f"{name}: PCM diverged from the Nuked-OPL3 C reference "
            f"(got {digest}, want {_GOLDEN[name]})")


def test_differential_vs_c_build_when_available():
    try:
        from pynuked_opl3 import OPL3 as COPL3
        COPL3(sample_rate=44100)
    except Exception:
        import pytest
        pytest.skip("pynuked_opl3 C extension not built (optional accelerator)")
    script = _fuzz_script(0xC0FFEE)
    a = _run(COPL3(sample_rate=44100), script)
    b = _run(OPL3(sample_rate=44100), script)
    assert a == b


def test_deterministic_and_resettable():
    script = _fuzz_script(7)
    first = _run(OPL3(sample_rate=22050), script)
    chip = OPL3(sample_rate=44100)
    _run(chip, _fuzz_script(8))          # dirty the chip thoroughly
    chip.reset(22050)
    assert _run(chip, script) == first    # reset == fresh construction


def test_mono_is_left_channel_and_zero_frames():
    chip = OPL3()
    assert chip.generate_stereo(0) == b""
    assert chip.generate_mono(0) == b""
    chip.write(0x20, 0x01)
    chip.write(0xA0, 0x98)
    chip.write(0xB0, 0x31)
    stereo = chip2 = OPL3()
    chip2.write(0x20, 0x01)
    chip2.write(0xA0, 0x98)
    chip2.write(0xB0, 0x31)
    stereo = chip.generate_stereo(512)
    mono = chip2.generate_mono(512)
    assert len(mono) == len(stereo) // 2
    assert memoryview(stereo).cast("h")[0::2].tobytes() == mono
