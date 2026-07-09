# Performance — how to run dos_re fast (and how to make it faster safely)

The interpreter is pure Python by design (stdlib-only core, hookable at any
CS:IP, byte-exact verifiable). That means the two biggest speedups are *free*
— they come from how you run it, not from changing it.

## 1. Run headless workloads under PyPy (~13–17x interpretation)

The core never imports anything outside the stdlib, so any headless path —
oracle runs, hook verification, demo replay, the test suites — runs unchanged
under [PyPy](https://pypy.org). Measured (PyPy 3.11 v7.3.20 vs CPython 3.11,
Windows, 20M-instruction steady state):

| Workload | CPython | PyPy | speedup |
|---|---|---|---|
| SkyRoads pure-ASM interpretation | 714k instr/s | 12.5M instr/s | **17.5x** |
| PRE2 pure-ASM interpretation (hybrid boot) | 623k instr/s | 8.3M instr/s | **13.4x** |
| dos_re test suite | ~11s | 2.75s | 4x |
| overkill_port test suite (verify-heavy) | 4m17s | 1m35s | 2.7x |
| pre2_port test suite | 24s | 15s | 1.6x |

(Suites gain less than raw interpretation because fixture/boot overhead and
numpy-bound tests don't JIT; long verify sweeps and oracle runs gain the most.
The JIT needs ~1–2M instructions of warmup before reaching steady state.)

Setup (Windows): `winget install PyPy.PyPy.3.11`, then
`pypy -m ensurepip && pypy -m pip install pytest pytest-xdist numpy`.
numpy ships PyPy 3.11 wheels; **pygame does not** — the live viewer
(`scripts/play.py` without `--headless`) stays on CPython. `--headless` runs,
demo replay/record, and every verifier work under PyPy.

Two interpreters means two installs resolve `dos_re`: CPython uses the pip
editable install; PyPy resolves through the port's own `sys.path` header
(`ROOT/dos_re` — the submodule repo root). Port entry-point scripts must keep
that header (see template_dos_port/scripts/play.py); relying on the editable
install alone breaks any interpreter that lacks it.

## 2. Parallelize suites with pytest-xdist (`-n auto`)

`pip install pytest-xdist`, then `python -m pytest tests -q -n auto`.
Helps when a suite has many similar-cost tests; does nothing when one long
test dominates the critical path. Measured locally (CPython, 24 threads):

| Suite | serial | `-n auto` | verdict |
|---|---|---|---|
| overkill_port | 4m17s | **56s (4.6x)** | the main beneficiary |
| pre2_port | 24s | 18s | mild |
| dos_re | 11s | 8s | mild |
| ancient_port / skyroads_port | 47s / 20s | ~same | critical-path-bound (one long demo-roundtrip test each) |

**xdist and PyPy do NOT compose well for suites**: every PyPy worker re-pays
JIT warmup, so overkill under `pypy -n auto` is 1m27s — slower than
CPython+xdist (56s). Rule of thumb: **CPython + `-n auto` for suites; PyPy
serial for long single-process runs** (verify sweeps, oracle runs, probes,
headless replay), where the 13-17x interpretation speedup dwarfs everything.

## 3. If you change the interpreter itself: the equivalence gate

Every optimization to cpu/memory hot paths must be proven byte-exact before
it lands. The working method (used for all of the 2026-07 rounds — bulk REP,
planar fetch fast path, slotted CPUState, modrm inlining):

1. **Long-run state gate**: boot a real game, run N million instructions,
   compare full-memory sha1 + CPU snapshot before/after the change.
2. **Trace gate**: run with `trace_enabled` and diff the first ~30k trace
   lines — catches "same end state, different path/text" regressions.
3. **Adversarial fuzz vs the old implementation** for anything with guards
   (e.g. bulk REP falls back to the element loop on DF=1, overlap, EGA
   aperture, wrap); force both paths and compare memory + registers + flags.
4. **Every consumer suite green** (dos_re + all four ports), and watch the
   known-failure baseline — never let an optimization "fix" or add one.

Hookability constraints that killed otherwise-easy wins, for the record: the
per-step replacement-hook probe must stay (ports assign plain dicts to
`cpu.replacement_hooks`), trace text is contract (tests assert on it), and
decoded-instruction caching needs a self-modifying-code invalidation design
first (`runtime_code.py` games patch their own instructions).
