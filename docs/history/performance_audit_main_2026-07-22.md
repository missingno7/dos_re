# Performance audit — `main` at `a6ae58c` (2026-07-22)

## 1. Executive summary

`main` already makes several good representation choices: the scalar CPU paths
stay on Python integers and `bytearray`, frame decoding uses NumPy only for
whole-frame array work, and both 16-bit and 32-bit interpreters have guarded
bulk `REP MOVS`/`REP STOS` paths. NumPy should not be introduced into scalar
instruction execution.

Two issues are confirmed:

1. `ReplayArtifact.timeline_coordinate()` reparses and linearly scans every
   coordinate for every lookup. Replay playback therefore becomes O(points^2).
   The local worktree already contains the correct parsed-tuple + ordinal-index
   fix and a focused regression test; it should be split out and merged.
2. `CPU386._bulk_string()` is semantically wrong for a forward-overlapping
   `REP MOVS`: its snapshot/slice copy has memmove semantics, but x86 executes
   each element in order. This is a correctness bug in a performance path, not
   permission to broaden the fast path.

The next low-risk throughput win is DOS `INT 21h/AH=3F`: normal RAM file reads
write each byte through `Memory.wb`, even when the entire destination is a
contiguous, side-effect-free range. Under the intended PyPy runtime a guarded
slice is still four times faster in isolation; a NumPy view is faster again,
but should not be imported into this core path without an end-to-end startup
and asset-load measurement. The guard must retain the scalar loop for segment
wrap, BIOS ROM, planar aperture, and memory-write observation.

No port replay, Atlas corpus, executable, or production profile was present in
this framework checkout. Findings marked conditional need a replay/profile from
an actual port before implementation.

### Follow-up status

The replay-coordinate cache was merged into `main` after this audit. The PM
bulk-string path was also corrected to fall back for unsafe forward RAM overlap,
with new MOVSB/MOVSD regression coverage. The guarded AH=3F bulk-write and the
larger conditional experiments remain proposals.

## 2. Confirmed hot-path issues

### A. Replay coordinate lookup is accidentally quadratic

- **Location:** `dos_re/replay.py`, `ReplayArtifact.timeline_coordinates` and
  `timeline_coordinate` (around lines 1020–1045 on `main`); used by
  `dos_re/player.py` replay stepping.
- **Current work:** every lookup deserializes every coordinate, then builds a
  matching list by scanning all coordinates.
- **Scale/frequency:** once per semantic replay point. A timeline with `P`
  coordinates performs roughly `P²` coordinate parses/comparisons.
- **Evidence:** a 256-point synthetic timeline took **423.91 ms** using the
  `main` algorithm versus **0.0041 ms** with one parsed ordinal index
  (**~103,015×**). The ratio includes repeated object construction, which is
  exactly what the current implementation does.
- **Boundary:** cache the immutable timeline authority as one tuple plus a
  dictionary indexed by ordinal; invalidate only when its raw manifest object
  is replaced.
- **Risk:** low. Keep timeline identity validation and reject missing or
  duplicate ordinals. Do not make a cache mutation into replay authority.
- **Status:** merged after this audit; `tests/test_replay.py` asserts that 640
  lookups parse 64 records exactly once.

### B. PM bulk `REP MOVS` breaks overlapping-copy semantics

- **Location:** `dos_re/cpu386.py`, `CPU386._bulk_string` (around lines
  1254–1330).
- **Current work:** ordinary RAM `REP MOVS` reads the full source into a
  temporary `bytes` object and assigns it to the destination slice.
- **Why wrong:** with forward direction and `src < dst < src + n`, the scalar
  instruction reads a value it just stored on the next iteration. The slice
  instead snapshots the whole source first. For `src=b"abcdef"`, `dst=src+1`,
  and `REP MOVSB count=5`, the current fast path returns `b"aabcde"`; the
  forced scalar path returns `b"aaaaaa"`.
- **Performance context:** on PyPy the scalar loop is JIT-compiled, so at
  `REP MOVSD` count 4,096 the fast path measured **0.0182 ms** versus
  **0.0198 ms** (**1.09×**). Preserve the fast path only for already-proven
  safe ranges; this is a correctness fix, not a measured PyPy throughput loss.
- **Boundary:** make this a memmove-*unsafe* fast path, exactly as the 16-bit
  implementation already does: decline when forward overlap would alter the
  ordered source stream. Backward direction and VGA/device cases stay scalar
  until separately proven.
- **Risk:** high correctness sensitivity; add an adversarial overlap test
  beside `test_bulk_string_equals_per_unit_loop`, then run oracle/replay
  verification on PM ports.

### C. DOS file reads dispatch one Python memory write per byte

- **Location:** `dos_re/dos.py`, `DOSMachine.int21`, AH=3F (around lines
  1342–1353).
- **Current work:** `for i in range(n): cpu.mem.wb(ds, dx+i, file_byte)`.
- **Scale/frequency:** asset and save-file loads; a 60 KiB read invokes the
  full segmented write path 61,440 times.
- **Evidence:** on normal, unobserved real-mode RAM a 60 KiB loop measured
  **0.09 ms**; direct `bytearray` slice assignment measured **0.0251 ms**
  (**~4×**). A zero-copy NumPy view measured **0.0074 ms**, but it would pull
  NumPy into a currently lightweight core path.
- **Boundary:** add one memory-owned guarded bulk-write helper, not a DOS-local
  address shortcut. It may slice only when the entire write has no 16-bit or
  20-bit wrap, is below BIOS ROM, does not touch the EGA aperture, and has no
  write watchers. Otherwise retain the exact existing byte loop.
- **Risk:** medium. Write watchers, ROM suppression, EGA latches/map masks,
  and segment wrapping are observable semantics.

## 3. NumPy candidates

| Candidate | Classification | Evidence and decision |
| --- | --- | --- |
| Indexed mode-13h / Mode X frame conversion (`dos_re/dos4gw.py`) | **Conditional candidate** | Current NumPy frame decode measured 1.079 ms versus 1.81 ms for the PyPy scalar loop at 320×200. Removing two small copies reduced it to 0.798 ms (1.35×), which is worthwhile only if a viewer profile shows frame-presentation pressure. |
| BIOS text frame (`dos_re/textmode.py:render_text_rgb`) | **Strong candidate when text mode is live** | The current 2,000-cell Python loop measured 29.05 ms; a glyph-LUT/broadcast prototype measured 4.67 ms (6.2×). It allocates a full 640×400×3 result plus intermediate masks/colors, so cache the 256-glyph LUT, not frame contents, unless a write-generation contract exists. |
| Replay changed-page discovery (`dos_re/replay.py:cache`) | **Strong candidate for large caches** | For a 16 MiB region and 4 KiB pages with one changed byte, NumPy zero-copy views plus row-wise comparison measured 8.12 ms versus 144.05 ms for the existing slice/copy scan (17.7×) on PyPy. Profile total cache time including decompression, zlib, and disk before changing artifact code. |
| Frame-verification divergence images (`dos_re/frame_verify.py:diff_rgb_frame`) | **Probably not worthwhile** | The code is scalar and slice-heavy, but it runs only after a mismatch while generating diagnostics. Optimize correctness/diagnostics first, not a cold failure path. |
| Scalar CPU/memory instruction execution | **Not a candidate** | NumPy array creation and scalar extraction would add overhead and weaken PyPy prospects. Keep Python integer/`bytearray` operations; use bulk helpers only at explicit instruction/DOS operation islands. |
| Trace/profile aggregation | **Conditional candidate** | `tools/profile_hotspots.py` uses `Counter` and is offline. NumPy histogramming may help very large traces, but there is no corpus in this checkout and no need to burden the live interpreter. |

## 4. Better non-NumPy bulk operations

1. **Guarded `bytearray` slice writes for AH=3F.** This is the clearest
   immediate win. It is faster than NumPy and does not introduce a new
   representation.
2. **Preserve and extend existing slice fast paths, not vector arrays.**
   `CPU8086.string_op` and `CPU386._bulk_string` are the right execution
   islands. First fix PM overlap; then use replay evidence to decide whether
   backward, watched, or device variants deserve dedicated paths.
3. **Use a precomputed glyph LUT for text mode when a port has a live text
   display.** The LUT is naturally cached; the display result must remain newly
   materialized unless the frontend owns a frame/memory generation counter.
4. **Cache parsed immutable replay structures.** This is allocation and
   dispatch removal, not numerical work.
5. **Do not replace bytearray page comparison with `memoryview ==`.** The
   verifier already documents that CPython performs element-wise memoryview
   equality. Existing bytearray comparisons are C-speed; a NumPy experiment is
   only justified in the cache writer's many-page scan.

## 5. Architectural performance problems

### Generated 16-bit CPUless strings retain a tiny-operation boundary

`dos_re/lift/emit_cpuless.py` emits `while cx:` bodies for generated
`REP MOVS`/`REP STOS` (around lines 563–590), each making segmented `rb/rw` and
`wb/ww` calls. A 64 KiB copied region is therefore still tens of thousands of
Python calls after lifting. This is the right *semantic* fallback but may be
the wrong performance boundary for a lifted decompressor/blitter.

Do not add NumPy to generated functions. After a port census identifies hot
string instructions, have the generic generator call a shared, evidence-tested
bulk-string primitive with the same overlap, wrap, device, watcher, and virtual
instruction-count contract as the interpreter. `emit32.py` already delegates
string work to `CPU386._string`, so it benefits from that carrier's fast path.

### Replay caches repeatedly reconstruct the base state

`ReplayArtifact.cache()` calls `_read_full_state()` before comparing every
candidate boundary, which decompresses and hashes the profile base anew. It
then allocates two Python slices per 4 KiB page during comparison. On a corpus
with many cached boundaries and large PM memory this can dominate cache
ingestion before compression and I/O. This is a **conditional** medium-sized
experiment: memoize the immutable verified base per artifact/profile, with
clear invalidation on manifest reload, then measure real artifact cache time.

## 6. Suspicious or obviously wrong code

- **Confirmed:** PM forward-overlap `REP MOVS` bulk behavior is wrong (section
  2B). The current unit matrix does not include overlapping ordinary RAM.
- **Confirmed:** replay coordinate lookup on `main` is O(points²) (section 2A).
- **Suspicious but not yet measured end-to-end:** `SoundBlaster._start_dma()`
  extends PCM from a generator that calls a generic single-byte callback for
  each DMA byte. The installed callbacks are direct bytearray reads, but the
  generic API deliberately permits different behavior. Add an optional
  contiguous/ring bulk-read capability only if audio profiling proves it
  material; do not bypass the callback by type inspection.
- **Not a finding:** `render_pm_frame()` copies small palette/frame intermediates.
  The no-extra-copy NumPy prototype showed no repeatable improvement, so this
  is not worth changing.

## 7. Ranked action plan

### Immediate, low-risk

1. **Merge the local replay-coordinate cache as its own commit.** Highest
   replay impact, small change, low regression risk, all platforms. It is
   already tested.
2. **Add the PM-overlap regression test and narrow `_bulk_string`.** High
   correctness impact and moderate implementation effort; preserve the huge
   safe-path speedup. Relevant to desktop and any PM/native migration because
   it defines faithful semantics.
3. **Prototype a guarded `Memory` bulk-write helper for AH=3F.** High load-time
   impact, low-to-medium effort, medium semantic risk. Focused memory edge-case
   tests and replay verification are required.

### Medium-sized, evidence-gated

4. **Give CPUless generated REP strings the shared bulk boundary.** Potentially
   high impact for lifted blitters/decompressors, but requires generator output
   tests plus oracle evidence for each allowed memory mode.
5. **Memoize replay base state and profile cache ingestion.** Medium impact for
   large PM replay corpora; retain artifact lock/publication semantics.
6. **Vectorize text-mode rendering after confirming a live text-mode use.** A
   6.2× PyPy microbenchmark gain is real, but it is presentation-only and text
   screens are often static.

### Larger experiments

7. **Profile a selected execution plan over real replays, then replace a whole
   recovered routine/scanline/sprite renderer where it dominates.** This is
   more promising than vectorizing individual interpreter instructions and is
   the path that transfers to Android/iOS native implementations.
8. **Evaluate a NumPy page-diff path against actual cache workloads.** The
   isolated 17.7× PyPy gain is substantial, but zlib/disk costs may still
   dominate end-to-end ingestion.

## 8. Benchmark evidence

Host commands used the intended runtime: PyPy 7.3.20 / Python 3.11.13 with
NumPy 2.4.6, pygame-ce 2.5.7, and pytest 9.1.1. The shell-selected MSYS Python
3.10 is not the project runtime and was not used for the final timings:

```powershell
$env:PYTHONPATH = '.'
& 'C:\Users\jiriv\AppData\Local\Microsoft\WinGet\Packages\PyPy.PyPy.3.11_Microsoft.Winget.Source_8wekyb3d8bbwe\pypy3.11-v7.3.20-win64\python.exe' -m pytest -q tests\test_cpu386.py tests\test_render_frame.py tests\test_textmode.py tests\test_player.py tests\test_replay.py
```

Result: **102 passed in 8.71 s**.

The focused benchmarks used `time.perf_counter`, five warm samples, median
per-operation timing, random-like buffer contents, and checked result equality
before timing. Their essential forms were:

```python
# `main` replay lookup versus indexed immutable cache
def old_lookup(point):
    coordinates = tuple(ReplayPointCoordinate.from_json(x) for x in raw)
    return [c for c in coordinates if c.point == point][0]

parsed = tuple(ReplayPointCoordinate.from_json(x) for x in raw)
by_ordinal = {c.point.ordinal: c for c in parsed}
def indexed_lookup(point):
    return by_ordinal[point.ordinal]

# AH=3F safe-RAM operation: 60 KiB, no watcher/device/wrap/ROM range
for i in range(count):
    mem.wb(seg, (off + i) & 0xFFFF, payload[i])
mem.data[linear(seg, off):linear(seg, off) + count] = payload

# Page discovery: 16 MiB, 4 KiB pages, one changed byte
current = [i for i, start in enumerate(range(0, len(data), 4096))
           if data[start:start+4096] != base[start:start+4096]]
vector = np.flatnonzero(np.any(
    np.frombuffer(data, np.uint8).reshape(-1, 4096) !=
    np.frombuffer(base, np.uint8).reshape(-1, 4096), axis=1))
```

| Workload | Current | Straightforward bulk Python | NumPy | Verdict |
| --- | ---: | ---: | ---: | --- |
| Replay lookup, 256 points | 423.91 ms | 0.0041 ms indexed dict | N/A | Merge cache |
| DOS read, 60 KiB safe RAM | 0.09 ms | 0.0251 ms slice | 0.0074 ms | Slice first; measure lazy NumPy |
| PM `REP MOVSD`, count 4,096 | 0.0182 ms fast path | 0.0198 ms forced scalar | N/A | Fix overlap; preserve safe path |
| PM RGB 320×200 | 1.079 ms NumPy | 1.81 ms scalar | 0.798 ms no-extra-copy variant | Conditional |
| Text RGB 80×25 | 29.05 ms | N/A | 4.67 ms LUT/broadcast prototype | Strong when live |
| Page scan 16 MiB | 144.05 ms | N/A | 8.12 ms | Strong for large caches |

## Local branch assessment (completed)

The former `feature/presentation-frame-pipeline` branch was not stale:

- Its three committed changes cleanly form a presentation feature series:
  fixed simulation/presentation clocks, host responsiveness during replay
  seeks, and an optional product GPU presenter. They belong in the framework
  because they keep presentation explicitly non-authoritative and do not add a
  parallel execution-selection authority.
- Its commits were merged as a reviewed feature branch, separate from the
  replay and interpreter fixes. The most important remaining review area is an
  interactive/replay integration test for fixed-tick input timing; the current
  unit tests cover parser defaults and accumulator phase.
- The formerly uncommitted `replay.py` coordinate cache was committed and
  merged independently. The `player.py` accumulator refinement and its tests
  were merged with the fixed-tick presentation work.

The branch's focused suite passed: 57 passed, 2 skipped before the expanded
CPU/frame/replay set above. No user changes were discarded or altered.

## Follow-up on merged main

The first safe DOS I/O improvement from the audit is now implemented.  INT 21h
AH=3F delegates its destination write to `Memory.write_external_block()`.  That
memory-owned operation performs one slice assignment only for contiguous,
unwatched conventional RAM.  It deliberately falls back to exact repeated
`wb()` writes for selector mappings, 16-bit offset or 20-bit physical wrapping,
the BIOS ROM, planar EGA, and write watchers.

On the same PyPy runtime, a 64 KiB ordinary-RAM destination write measured
**0.046 ms** through the guarded bulk path versus **2.115 ms** through scalar
`wb()` calls (**46.1x**).  Focused semantic and integration tests passed
(67 tests), together with the repository lint and undefined-name checks.
