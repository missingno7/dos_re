# The agent toolbox — task → tool → command

**Audience: the AI agent using `dos_re` to recover a game.** This is the
operational index: for each recurring task, the tool that does it mechanically,
the command, and what to do when it fails. The porting *method* (phases, loop
protocol, checklists) lives in `template_dos_port`; the mechanism deep-dives
live in the other docs here. This page is the routing layer between them.

## The division of labor (who does what)

| Work | Owner |
|---|---|
| Mechanical first-draft translation of ASM → Python | the **lifter** (`liftgen`/`liftverify`), never hand-transcription |
| Deterministic comparison against the original | the **oracles** (hook verifier, frame verifier, demo replay), never eyeballing |
| Repetitive diagnosis (hotspots, lengths, layouts, leaks) | the **tools** (`profile_hotspots`, `lindis`, audits), never re-derivation |
| Interpretation, naming, refactoring, deciding what's next | **you** |
| The final claim "this equals the original" | an **oracle proof** — your reading of the code is never evidence |

If a step is deterministic and you're doing it by hand, stop: either a tool
exists (below) or the framework should grow one (`AGENTS.md`).

---

## 1. Boot / load a new DOS executable

```python
from dos_re.runtime import create_runtime
rt = create_runtime("assets/GAME.EXE", command_tail=b"")
rt.cpu.run(1_000_000)
print(rt.cpu.addr(), rt.cpu.instruction_count)
```

Or zero-setup, with a window: `python tools/view.py --exe assets/GAME.EXE`
(the standard player CLI: `--headless`, `--snapshot`, `--save-snapshot`,
`--record-demo`, `--play-demo`, hotkeys F10 screenshot / F11 demo / F12
snapshot).

- Packed EXE (LZEXE etc.)? The unpacker is *bootstrap, not gameplay*: run it
  once (`dos_re.bootstrap_lzexe` accelerates LZEXE 0.91), then snapshot past
  it and work from the unpacked image.
- Game needs Sound Blaster? `dos_re.runtime.enable_sound_blaster(rt)`.
- Arrived as a DOSBox-X save instead of a clean boot?
  `dos_re.dosbox_savestate` imports it.

## 2. The VM fails loud (unsupported CPU / DOS / BIOS / VGA behaviour)

This is by design — the framework never guesses. The exception names the
exact instruction/service/port and context.

1. Decode what the program actually needed (`tools/lindis.py` at that address;
   the exception text usually suffices).
2. Implement the **observed** behaviour in the owning module (`cpu.py`,
   `dos.py`, `memory.py`…) with the register/flag contract documented, plus a
   focused test. Rules and the per-area extension recipes: [`AGENTS.md`](../AGENTS.md).
3. Never work around it in the game port — the next game hits it too.

Port reads specifically: unmodeled reads return 0 by default but are always
recorded in `dos.unmodeled_port_reads`; set `rt.dos.strict_ports = True` to
make each one fail loud with the reading CS:IP.
[`hardware_support.md`](hardware_support.md) is the honest model-status matrix.

## 3. Capture / restore snapshots

```python
from dos_re.snapshot import write_snapshot, load_snapshot
write_snapshot(rt, "artifacts/snap_after_init",           # full machine freeze
               status="after init", steps=rt.cpu.instruction_count)
rt2 = load_snapshot("assets/GAME.EXE", "artifacts/snap_after_init")   # exact resume
```

When: after the packer bootstrap, after init/first playable state, before any
routine you're studying (the verifier's fixture), at any divergence. In the
viewer: F12. Snapshots are evidence, never a runtime dependency of the
shipped port.

## 4. See what the screen shows

```bash
python tools/render_frame.py <snapshot_dir>              # snapshot → PNG
python tools/render_frame.py out.png --exe GAME.EXE --steps 2000000
```

Decodes VGA mode 13h and the EGA/VGA planar path (shadow planes + CRTC start
+ DAC). A mode it doesn't cover means your adapter grows a rasterizer — this
tool is the template.

**DOS/4GW (MZ+LE, 32-bit protected mode) titles** use the flat-386 pair
instead:

```bash
python tools/le_info.py assets/GAME.EXE                  # objects/entry/fixups
python tools/pm_boot.py --exe assets/GAME.EXE --png frame.png \
    --keys 20 --scancodes 39,b9 --at 30000000            # run to the frontier
```

`pm_boot` is the bring-up loop: each run stops at the first unimplemented
opcode/service and names it — implement the observed behaviour, re-run.  The
PNG render follows the live VGA state (chained 13h or unchained Mode X).
Programmatic use: `dos_re.runtime.create_pm_runtime` +
`dos_re.dos4gw.render_pm_frame`.

## 5. Trace / read code

```python
rt.cpu.trace_enabled = True          # per-instruction disassembly + state
...
for line in rt.cpu.trace: ...
```

```bash
python tools/lindis.py assets/GAME.EXE <snapshot_dir> 1010 9AFF 9C6B
```

`lindis` = linear disassembly of CS:START..END at a snapshot — static decoder
lengths (the lifter's, interpreter-cross-checked), interpreter-rendered text.
When: reading a routine before/after lifting it, decoding a fail-loud address,
checking what a divergence trace pointed at.

## 6. Find frame boundaries and wait loops (profile first!)

```bash
python tools/profile_hotspots.py assets/GAME.EXE 5000000 --snapshot <snap> --top 40
```

Reports where runtime goes, **tight backward edges** (= busy-wait loops: PIT
wait, retrace wait, input polls) and boundary crossings. This is the
mechanical answer to "where are my frame boundaries?" and "which loops need
classifying?" — run it before any manual tracing.

Classify what it finds:

- **Timer/retrace waits** → the frame-verify boundary hooks +
  `reference_env_hooks` (the oracle keeps these so it doesn't spin forever).
- **Boundary-less input polls** (title/menu "press fire") → the adapter's
  shared input-wait registry, consumed by EVERY driver. Miss one and demos
  freeze — [`demos_and_snapshots.md`](demos_and_snapshots.md) (the
  boundary-clock invariant) before recording anything.
- Role bookkeeping: `dos_re.hook_taxonomy` (checkpoint / env_wait /
  debug_probe / glue).

## 7. Record / replay deterministic demos

```python
from dos_re.input_demo import InputDemoRecorder, InputDemoPlayback
```

Interactively: `python tools/view.py --exe GAME.EXE --record-demo NAME` (or
F11), replay with `--play-demo <path>` (add `--headless` for speed). Keys are
delivered via `dos_re.interrupts.deliver_scancode` (port 60h + the game's own
INT 09h ISR — not BIOS); `dos_re.keyboard.KeyDispatcher` holds each key a full
polled frame.

A demo is a proof artifact only if it replays byte-identically under every
driver. Divergence deep into a demo? `InputDemoPlayback.write_suffix` carves a
snapshot + rebased tail at the boundary — resume there, not from the start
(`dos_re.repro_artifacts` pairs with it).

## 8. Hook a routine

```python
@registry.replace(0x1030, 0x4537, "scan_4537")   # dos_re.hooks.HookRegistry
def hook(cpu): ...                                # thin adapter over a pure rule
```

- Return mechanics must be exact; child hook boundaries route through
  `call_installed_hook_like_near_call` / `jump_installed_hook_boundary`
  (a direct Python call hides the child from verification —
  `tools/audit_hook_oracle.py` catches this statically).
- Runtime-patched routine? Guard with `self_disable_if_patched` /
  `code_matches`; multiple live variants → `dos_re.runtime_code`.
- Unrecovered path reached at runtime → raise `dos_re.gaps.HybridGap`. Never
  fall back silently.
- Shared 8086 flag semantics for hand-written hooks: `dos_re.asm` (INC/DEC
  preserve CF, REP string ops honouring the EGA aperture, …).

Details: [`hooks_and_verification.md`](hooks_and_verification.md).

## 9. Verify a hook against the oracle

```python
from dos_re.verification import HookVerifierConfig, install_hook_verifier
install_hook_verifier(rt, HookVerifierConfig.strict())   # no metadata needed
```

Strict mode: run the hook, take its final address as the target, run the
original ASM there, diff registers + flags + **full memory**. Faster metadata
mode: declare a `HookStop` per address. On divergence:

```bash
OK_TRACE_HOOK=CS:IP python <your repro>    # prints the ASM oracle's trace
```

Read the trace before theorizing — the classic causes are freed-stack scratch
words, flag shape, early-out branch to a shared RET, a nested child hook, and
capture phase. Whole-frame equivalence: `dos_re.frame_verify.run_frame_verifier`
(reference vs candidate stepped to adapter frame boundaries, PNG + report
artifacts on mismatch).

## 10. Lift routines automatically (never hand-translate a first draft)

```bash
# Census: which entries are liftable, and why not (indirect jump / x87 / …)
python tools/liftgen.py --exe assets/GAME.EXE --snapshot <snap> \
    --entries-file docs/<game>/candidates.txt

# Lift + prove in situ: emit literal hooks, install under the strict verifier,
# run the VM, diff EVERY call against the ASM oracle, write the proof ledger
python tools/liftverify.py --exe assets/GAME.EXE --snapshot <snap> \
    --entry CS:IP --steps 5000000 --emit-dir <game>/lifted
```

Pick a snapshot where the target actually executes — `NOT_REACHED` means the
proof never ran, not that it passed. `ORACLE_PASSING` means the first
`--samples` calls (default 20) were byte-exact — a sample, not a whole-run
proof; the output marks "(hit --samples cap; later calls unverified)", and a
deeper branch can still diverge past the cap (raise `--samples`, or lean on
demo-replay equivalence). `DIVERGED` = read the reported call and treat it
like any hook divergence. Callees/INTs run through the VM
(`dos_re.lift.runtime`), so lifting order is irrelevant and lifted +
hand-written hooks compose.
Full design + failure policy: [`lifting_design.md`](lifting_design.md).

## 11. LIFTED is not RECOVERED (the metrics-honesty rule)

Lifted functions live in their own `<game>/lifted/` tier with their own JSON
proof ledger (`dos_re.lift.manifest`: `LIFTED → ORACLE_PASSING → INSTALLED →
REFACTORED`), **disjoint** from `@oracle_link` islands. A lift counts as
recovered only after **you** rename and simplify it into clean pure-layer
Python and tag it `@oracle_link` — with the same oracle tests unchanged. A
wall of unread-but-verified lifted code must never inflate "recovered %".

## 12. Prove the VM-less native core (the endgame proof)

`dos_re.tick_demo` — the mode-independent equivalence engine. Input demos ride
an instruction-count clock, which is **mode-dependent** (a hook runs fewer
emulated instructions than the ASM it replaces) and doesn't exist VM-less; a
tick demo is keyed to the GAME TICK, so one recording replays identically in
pure-ASM, hybrid, and native.

```python
from dos_re.tick_demo import TickDemo, masked_digest, record_ticks, verify_ticks

# RECORD (VM = the oracle): adapter supplies the seam addresses + capture logic
demo = record_ticks(rt, cs=0x1030, ds=0x1A0F,
                    seed_ip=FRAME_TOP,            # main-loop top -> the native seed
                    commit_ip=GAP_SITE,           # end-of-tick -> digest + commit
                    observe={DECODE: grab_keys, KEY_SAMPLE: refine_keys},
                    commit=finish_tick, digest=lambda rt: my_digest(rt),
                    advance_one_frame=drive)      # an input-demo replay usually drives this
demo.save("artifacts/.../tick_demo.bin")

# VERIFY (no VM at all): every tick — inject keys+sidebands, ONE native tick, compare
n_ok, div = verify_ticks(TickDemo.load(path), native_state,
                         inject=my_inject, tick=my_tick, digest=my_digest)
```

All ticks matching = the VM-less game provably reproduced the oracle
byte-for-byte (under the digest's ownership mask) over the whole recording —
the "flip the engine" exit condition. The three rules that make it sound (each
learned from a real divergence — the module docstring has the full stories):

- **Capture at the consumption point** — keys are observed where the tick
  *consumes* them (an ISR between frame-start and the read otherwise falsifies
  the recording); later observers overwrite earlier ones by design.
- **Sidebands** — state the native core can't reproduce (PIT-fed idle timers,
  anything instruction-count-derived) is recorded per tick and *injected*.
- **The digest is the ownership mask** — `masked_digest` neutralises
  render/input-plumbing/audio state so a match means "same gameplay" by the
  same definition the forward lockstep oracle proves.
- `tick()` returns a terminal message for level-end/game-over/game-complete
  (transitions whose VM frames have no native counterpart) — the compare ends
  there legitimately; an unrecovered path raises and is reported.

**On divergence, carve a repro — never re-debug from the seed.** `verify_ticks`
reported tick `i`; reposition and slice:

```python
st = make_state(demo.seed)
replay_to(demo, st, i, inject=my_inject, tick=my_tick)   # fast: no digest checks
demo.suffix(i, capture_bytes(st)).save("artifacts/.../repro_tick_i.bin")
```

The suffix reproduces the divergence at its own tick 0 — every subsequent
debugging iteration replays one tick, not the whole recording. (The input-demo
analogue is `InputDemoPlayback.write_suffix`.) The same rule applies to the
native runner itself: **every gap/crash writes a resumable snapshot and prints
the exact repro command** — `dos_re.player` does this for VM runners; your
VM-less native runner must implement the same (endgame accelerator; the
completed port's `dump_gap_snapshot` is the worked example).

Inspect a recording: `python tools/tick_demo_info.py <demo.bin>`.

## 12b. Prove the VM-less FRONT END (the non-gameplay screens)

The tick demo captures **zero** of the front end: intro / title / menu / attract
/ world-map / tally all run with no game tick. Those are exactly where a VM-less
port drifts undetected — a screen shown in the wrong ORDER, a dropped fade, a
screen before/after the wrong transition (e.g. a "you must be expert" wall shown
*after* the map+level load instead of *before*). `dos_re.frontend_timeline` is
the front-end analogue of the tick demo: a per-PRESENT-FRAME timeline.

A per-frame pixel diff is the WRONG proof here — the reference recording rides a
wall-clock/instruction budget the native scene generator does not share, so frame
cadences are incomparable. What IS well-defined and cadence-free is the flow's
DISCRETE structure, proven by **four gates** (all in `dos_re.frontend_timeline`;
worked end-to-end in pre2's `scripts/verify_native_frontend.py`):

```python
from dos_re.frontend_timeline import (capture, collapse, filter_runs, diff_sequence,
                                      pack_fields, diff_fields,                 # gate 2
                                      input_segments, SegmentedInput,           # causal input
                                      diff_offsets, spread_beyond)              # gates 3+4

ref  = filter_runs(collapse(capture(vm_sample, N)),  ignore=TRANSIENT)   # ground truth
segs = input_segments(ref, per_frame_raw_input, N)   # the VM's input, segmented PER SCREEN
feeder = SegmentedInput(segs, blank=ZEROS)           # candidate consumes it causally
cand = filter_runs(collapse(capture(native_sample, N)), ignore=TRANSIENT)

diff_sequence(ref, cand, duration_tolerance=None)          # [1] screen ORDER
diff_fields(pack_fields(vm_d, FIELDS), pack_fields(nat_d, FIELDS), FIELDS)   # [2] per transition
owned = set(diff_offsets(masked(seed_dgroup), masked(native_end_dgroup)))    # [3] ownership split
spread_beyond(masked(a_tick), masked(b_tick), owned)                         # [4] inertness
```

- **[1] ORDER**: the filtered run-length screen sequence must match. Catches
  out-of-order / extra / missing screens (e.g. a "you must be expert" wall shown
  after the map+level load instead of before).
- **[2] WITNESS at every transition**: the adapter declares the DECISION-STATE
  fields (chosen level/mode, live-vs-attract input source, lives, password state)
  and they are byte-compared at each screen change. Cadence-free; a mismatch is a
  real behaviour divergence. (In pre2 this caught a fresh-start block the VM runs
  at MENU entry that native deferred to level load.)
- **[3] ENTRY-STATE OWNERSHIP**: the candidate's gameplay-entry state must be
  byte-identical to the reference's first-gameplay-tick seed OUTSIDE an OWNED byte
  set (`diff_offsets`) — the bytes where a VM-less product legitimately differs
  (sound-driver module data in DGROUP, load-layout pointers, scene scratch).
- **[4] INERTNESS — the ownership claim is PROVEN, not assumed**: replay the
  demo's recorded gameplay ticks TWICE (from the reference seed and from the
  candidate's own front-end output) with identical injected input; every tick must
  show `spread_beyond(...) == []` — the owned bytes demonstrably never influence
  gameplay. All four green = the native front end behaves like the original, from
  cold boot through byte-identical gameplay.

Input honesty (same oracle trick as the tick demo, plus causal alignment): while
replaying the demo on the VM, capture the raw key-flag window the front end samples
each frame (include EVERYTHING the flow reads — pre2's menu reads '1'/'2' flags at
[0x27F6/F7] *below* the scancode table, and the idle counter drives the attract
timeout). Then `input_segments` + `SegmentedInput` deliver it per-SCREEN, so a
keypress lands on the same screen at the same relative moment with no shared clock.
A cold-start demo (boot → oldies → titles → menu → level) is required so the native
cold-boot entry aligns with the VM — and seed the candidate from the FRONT-END entry
state (boot constants), not a level-jump bootstrap. pre2 adapters:
`scripts/probe_frontend_timeline.py` (ground-truth prober — run on ANY demo to see
what the original does) + `scripts/verify_native_frontend.py` (the 4-gate proof).

## 13. Measure progress (never estimate)

```bash
python tools/gen_island_manifest.py <game>.codecs <game>.recovered -o docs/recovered_islands.md
```

- `@oracle_link(boundary, contract, status, merge_target)` on every recovered
  function (`dos_re.islands`); the manifest is generated, never hand-edited.
- **Native %** over a demo replay — the framework collector:

  ```python
  from dos_re.coverage import CoverageCollector
  cov = CoverageCollector(classifier=my_addr_to_island,      # the adapter's only job
                          cache_path=Path("artifacts/coverage_cache.json"))
  rt.cpu.coverage_telemetry = cov
  ...replay a demo...; print(cov.format_summary()); cov.save_cache()
  ```

  Hooked work is measured in verifier-reported **ASM-equivalent instructions**
  (never hook calls); unverified runs estimate from the cache; anything
  unmeasurable is reported OUTSIDE the percentage. Wrap oracle reference runs
  in `cov.bounded_original()` so they don't read as un-recovered ASM.
- Endgame triage of the last unhooked addresses: `dos_re.frontier`.

## 14. Keep yourself honest (run with every change)

```bash
python tools/lint.py                        # the game-agnostic / lean-deps boundary (stdlib+numpy core)
python tools/audit_layers.py <game>/recovered   # pure layer never imports the VM
python tools/audit_hook_oracle.py <game>    # no child hooks hidden from verification
python tools/check_undefined_names.py       # latent-NameError guard
python tools/check_doc_links.py .           # broken doc links (docs are agent-maintained; run after edits)
python -m pytest tests -q                   # framework suite (pypy runs it ~4x faster)
```

Slow headless work? PyPy runs all headless paths unchanged, ~13–17× on raw
interpretation — [`performance.md`](performance.md). Interpreter optimizations
themselves need the byte-exact equivalence gate described there.
