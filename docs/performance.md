# Performance — how to run dos_re fast (and how to make it faster safely)

The interpreter is pure Python by design (scalar stdlib hot path, hookable at any
CS:IP, byte-exact verifiable). That means the two biggest speedups are *free*
— they come from how you run it, not from changing it.

## 1. Run under PyPy (~13–17x interpretation; ~2x the live viewer)

The interpreter's per-instruction path is scalar stdlib code (numpy — a core
dependency for bulk pixel/array work — installs fine under PyPy and stays OFF
that hot path), so every path — oracle runs, hook verification, demo replay,
the test suites, **and the live viewer** — runs unchanged under
[PyPy](https://pypy.org). Measured (PyPy 3.11
v7.3.20 vs CPython 3.11, Windows, 20M-instruction steady state):

| Workload | CPython | PyPy | speedup |
|---|---|---|---|
| SkyRoads pure-ASM interpretation | 714k instr/s | 12.5M instr/s | **17.5x** |
| PRE2 pure-ASM interpretation (hybrid boot) | 623k instr/s | 8.3M instr/s | **13.4x** |
| dos_re test suite | ~11s | 2.75s | 4x |
| overkill_port test suite (verify-heavy) | 4m17s | 1m35s | 2.7x |
| pre2_port test suite | 24s | 15s | 1.6x |
| SkyRoads live viewer, 1200 frames | 2m35s | 1m14s | **2.1x** |

(Suites gain less than raw interpretation because fixture/boot overhead and
numpy-bound tests don't JIT; long verify sweeps and oracle runs gain the most.
The JIT needs ~1–2M instructions of warmup before reaching steady state.)

### What a whole differential HARNESS gains: ~10x, and only when it is long

The rows above are raw interpretation. A frame-exact differential also captures
and compares a framebuffer per frame, which does not speed up as much, so quote
the harness number for a harness. Measured 2026-07-18, skyroads
A 5,109-frame CPUless ReplayArtifact verification run measured end-to-end:

| run length | CPython | PyPy | speedup |
|---|---|---|---|
| 5,109 frames (full) | 500.8 s | **47.3 s** | **10.6x** |
| 60 frames | 40.4 s | 7.5 s | 5.4x |

**Short runs gain far less** — at 60 frames, process start plus JIT warmup is
most of the run. PyPy pays off on the long gates, which is where the cost is.

**Switching interpreter is only sound if you PROVE the two agree**, because a
faster interpreter that quietly disagrees turns a real failure into a green
gate. The evidence for the run above: both logs hash identically (sha1
`1da10d27…`, excluding timing heartbeats) — same per-frame lines, same
`oracle peak 17126327 steps/frame`, same verdict. overkill has the same result
independently (a full lockstep re-record under each produces the identical
cache sha1). Re-run such a comparison after a PyPy upgrade; do not inherit it.

Setup (Windows): `winget install PyPy.PyPy.3.11`, then
`pypy -m ensurepip && pypy -m pip install pytest pytest-xdist numpy pygame-ce`.

**Use `pygame-ce`, not `pygame`** — the community fork ships PyPy 3.11 wheels
(upstream pygame has none and fails to build from source). It is a drop-in
replacement: same `import pygame`, and `pygame._sdl2` works, so
`dos_re.display`'s GPU present path is available. numpy also has PyPy wheels.

### Why the viewer only gains ~2x when raw interpretation gains ~17x

Neither pygame nor numpy is the bottleneck — measured per presented frame:
`Display.draw_game()` + `flip()` is **0.42 ms** and `decode_frame_default()`
is **0.78 ms**, essentially identical on both interpreters. The VM is the
cost, and the viewer calls `cpu.run()` in small per-frame slices
(`--steps-per-frame`, e.g. 30k) with a Python/pygame boundary crossing
between each. That chunking gives the tracing JIT far less to work with than
one long uninterrupted `cpu.run()`, so the viewer lands at ~2x rather than
~17x. Per-frame VM cost still drops sharply (SkyRoads steady-state idle loop:
**57.4 ms → 12.7 ms**), which is the difference between missing and making a
16.7 ms 60 Hz frame budget.

### The `sys.path` requirement (bites every entry-point script)

Two interpreters means two ways `dos_re` resolves: CPython finds it via the
pip editable install; PyPy has none, so it resolves through the script's own
`sys.path` header. **Every entry-point script that imports `dos_re` must
insert the submodule repo root**, one level above the package:

```python
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dos_re"))   # submodule repo root; the package is dos_re/dos_re/
```

Without it the script works on CPython and dies on PyPy with a confusing
`ModuleNotFoundError: No module named 'dos_re.cpu'` (the bare submodule dir
is picked up as an empty namespace package). This silently affected 26
scripts across the ports until the 2026-07-09 PyPy trial surfaced it. To
audit a port:

```bash
for f in scripts/*.py tools/*.py; do
  grep -qE '^\s*(from|import) dos_re' "$f" && ! grep -q 'dos_re"' "$f" && echo "MISSING: $f"
done
```

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
serial for everything else** — long single-process runs (verify sweeps,
oracle runs, probes, headless replay), where the 13-17x dwarfs everything,
and the live viewer, where it is worth a steady ~2x.

## Aside: the OPL3 backends (audio_sink.load_opl3 picks one)

| Backend | What | Speed | When used |
|---|---|---|---|
| `pynuked_opl3` (external, optional) | compiled Nuked-OPL3, bit-exact | native (~1% core) | opt-in accuracy upgrade: install/build the external package, select with `DOSRE_OPL3_BACKEND=nuked` |
| `dos_re/opl3_fast.py` | numpy APPROXIMATE synth, perceptually matched | ~50x RT CPython / ~43x PyPy | the default whenever the compiled build is absent (both interpreters) |
| `graveyard/opl3_exact.py` | pure-Python bit-exact Nuked translation | ~1x CPython / ~24x PyPy | DORMANT — never selected at runtime; the calibration/golden reference only |

opl3_fast's fidelity contract: exact pitch arithmetic, ADSR slopes/attack
calibrated against the exact core, the chip's real stepped tremolo/vibrato
patterns, the actual rhythm phase-bit recipe (hh/sd/tc combs), serial
recurrence for chaotic high feedback (fb 6-7), drum output doubling — all
tolerance-tested in tests/test_opl3_fast.py; A/B WAVs over 80 s of real
SKYROADS music were part of its acceptance.  It is approximate: bit-level
output differs (float sine vs ROM quantization, analytic envelopes, seeded
noise instead of the LFSR).  Anything that needs sample-exactness uses the
exact cores.

## 3. The recovered (CPUless) program: a different cost model

Everything above is about the **interpreter**. A CPUless port does not run one:
it runs *emitted Python* (`lift/emit_cpuless.py` output) over the device model.
Different hot path, different advice — and section 1's "the speedups are free,
run PyPy" only half-applies here. Two reasons.

**PyPy still wins big, because the emitted code is JIT-shaped.** Measured on
VGA Lemmings' recovered program (2026-07-17, CPython 3.11 vs PyPy 3.11 v7.3.20,
Windows; sim only, no render; boundaries/sec against the 73 Hz the game
expects):

| Screen | CPython | PyPy |
|---|---|---|
| front-end (mode 10h) | 30.8/s — **42% of real speed** | 161/s — 221% |
| gameplay (mode 0Dh) | 247/s — 338% | 162/s — 223% |

That **5.2x** on the front end *is* the emitter's overhead: the JIT erases it,
CPython pays it. Which matters because **the one host that cannot have PyPy is
the one that needs it** — an Android APK ships CPython (see
lemmings_port/android/README.md; the same program runs ~53% of real speed on a
Galaxy S24). For a shipped CPUless port, emitted-code quality *is* the
performance story.

**But under PyPy the standalone shell caps itself.** PyPy lands on ~6.2 ms per
boundary in *both* screens above — an identical floor for opposite workloads,
which is never the workload. Cause: a standalone shell parks its worker thread
every boundary, and a `threading.Event` round trip costs **7.5 ms on PyPy vs
0.012 ms on CPython** (Windows; `sys.setswitchinterval()` makes it *worse*, 9.5 ms
at 50 µs). So PyPy's 162/s is the handoff cap, not its speed. A shell that
drives the program from `boundary_cb` on one thread — the pygame event loop
must own the main thread anyway — would remove the floor entirely. **Unimplemented.**

### The open emitter work, in expected-value order

Ranked by measured evidence, not intuition. All of it is one file,
`lift/emit_cpuless.py`, so a change multiplies across a port's whole corpus
(Lemmings: 284 modules). Every item below is gated by the same acceptance
harness the corpus already passes, so a semantic slip fails loudly at a
boundary index rather than shipping.

1. **Dead-flag elimination.** The emitter computes CF/PF/AF/ZF/SF/OF eagerly
   for every ALU op. Measured: an emitted 16-bit `ADD` with flags is **176 ns
   vs 24 ns** without — flags cost **7.3x the operation**. In Lemmings' hottest
   function: **2,375 flag assignments vs 277 lines that read a flag**, so ~90%
   are dead stores. The machinery is half there — `lift/cfg.py` has the CFG,
   and the emitter already knows which flags each `jcc` READS (its flag-live-in
   refusal check). Constraint: the `_compat` contract reproduces exit flags for
   the CPU-ABI adapter, so liveness must treat function exit as a USE.
   *Highest ceiling.*
2. **Block dispatch: linear chain → binary tree.** Emitted functions are
   `while True:` + N independent `if bb == K:`. Fall-through costs one
   comparison, but every *backward* jump — i.e. every loop in the original asm —
   rescans the chain. Measured on a 327-block function: a full scan is **1.5 µs
   vs 0.06 µs** for a binary dispatch tree (**23x**), and its inner loops spin
   **5,722 while-iterations per boundary** ≈ **8.6 ms (~25%)** of a 35 ms
   front-end boundary. Mechanical codegen change; the cheapest probe of whether
   emitter work converts to wall-clock at all.
3. **Memory access inlining.** `mem.rb`/`mem.wb` are Python calls per byte:
   **15,033 reads + 9,662 writes per front-end boundary** (1,678 + 1,091 in
   gameplay). Emitting a direct index for non-EGA segments would cut the call
   overhead — but EGA latch/set-reset semantics live in `_ega_wb`, so the fast
   path must be provably narrow.

### Measuring it (two traps that cost real time)

* **cProfile is per-thread.** A standalone shell runs the program on a worker
  thread; profiling the shell shows nothing but `lock.acquire`. Enable the
  profiler *inside* the worker (wrap `runtime.call`).
* **cProfile lies about call-heavy code, in the direction you want to believe.**
  It attributed **21%** of a gameplay boundary to the port-write fan-out; the
  fix delivered **~6%** (A/B median, 3 runs each, ~10% run-to-run noise).
  Per-call overhead is exactly what a deterministic profiler inflates. Always
  A/B against a wall clock before believing a percentage — and land the
  measurement, not the intuition.

*Done so far:* `port_write` routes each OUT to its one owner instead of offering
it to all four device trackers (~+6% gameplay). A port-side counterpart worth
copying: decode planar pixels **once** and paint each palette band's rows once —
re-decoding per band doubled a split-screen render (1.49 ms → 0.83 ms in
lemmings_port; see docs/raster_effects.md).

## 4. If you change the interpreter itself: the equivalence gate

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
