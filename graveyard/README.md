# graveyard/ — dormant, kept-for-reference code

Code here is **not part of the importable `dos_re` package** and is **never
selected at runtime**. It lives outside `dos_re/dos_re/` on purpose: nothing
can `import dos_re.<name>` it by accident, and `audio_sink.load_opl3()` cannot
pick it. It is kept only as a reference / provenance record and as a test
oracle, not because anything ships it.

## `opl3_exact.py` — the bit-exact pure-Python Nuked-OPL3 core

A literal, byte-identical Python translation of Nuked-OPL3 v1.8
(LGPL-2.1-or-later — see the file header and the repo `LICENSE`). It was the
default software OPL3 backend until 2026-07, when `dos_re/opl3_fast.py` (a
numpy approximate synth, ~50x real-time on CPython, perceptually
indistinguishable on real game music in blind A/B) replaced it everywhere in
the runtime.

Why it was retired from the runtime: it is **~1x real-time on CPython** on a
busy chip — too slow to be the everyday playback backend (the whole point of
`opl3_fast`). It is **not deleted** because:

- it is the calibration ground-truth `opl3_fast` is measured against
  (`tests/test_opl3_fast.py`);
- its golden PCM hashes (`tests/test_opl3.py`) are the recorded output of the
  upstream compiled C reference, so it remains a regression guard that the
  *exact* semantics are preserved should we ever need bit-exact software
  synthesis again;
- it documents the provenance of the OPL3 model.

Runtime OPL3 selection (`dos_re.audio_sink.load_opl3`) is two-way now:
compiled `pynuked_opl3` when built (bit-exact, native), else `opl3_fast`.
Tests reach this module via `tests/conftest.py`, which puts `graveyard/` on
`sys.path` so `import opl3_exact` resolves.
