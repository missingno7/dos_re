> Framework method reference. Authority: [`execution_planner.md`](execution_planner.md)
> defines execution and release closure; [`dos_re_2.0.md`](dos_re_2.0.md)
> defines generated recovery techniques. Promoted from the
> DOS_RE 1.0 starter (template_dos_port, retired) because the mechanics remain valid.

# Cookbook — proven techniques that live in the source repos

Not everything the two ports invented could be promoted into this framework:
some mechanisms are inseparably welded to their game's addresses and layouts.
But every one of them **solved a problem your game will likely also have**,
and the worked example is sitting on disk. This is the problem-indexed map.

The worked examples live in the sibling repositories: `pre2_port` (P2 —
github.com/missingno7/pre2_port), `overkill_port` (OK) and `kegg_port` (KE —
the first DOS/4GW title), typically checked out next to this repo. Paths below are relative to those repos. If neither is
available on your machine, the entries below still carry the essential shape
of each technique — enough to re-derive it against your own oracle; treat the
missing example as lost convenience, not lost method.
When you re-implement one of these for a new game, read the original first —
each encodes debugging that took days — and if your version comes out generic,
promote it (see `roadmap.md`, "parameterize-and-promote").

## Protected mode (DOS/4GW / Watcom LE titles)

**The EXE is MZ but tiny, mentions DOS/4G(W), and `create_runtime` boots
garbage.** → It's an MZ *stub* + `LE` (Linear Executable) at `e_lfanew` — a
32-bit flat protected-mode program. The extender is *bootstrap, not
gameplay*: don't emulate DOS/4GW, replace it. `dos_re.le.load_le` maps the
image, `dos_re.runtime.create_pm_runtime` boots it on the flat 386 core
(`cpu386.py`) with the DOS/4GW host (`dos4gw.py`). Day-0 tools:
`tools/le_info.py` (what am I looking at), `tools/pm_boot.py` (run to the
fail-loud frontier), `tools/view.py --protected-mode` (zero-setup live window). The whole
bring-up loop is: run pm_boot, read the named unimplemented opcode/service,
implement the observed behaviour, re-run. KE went from LE parsing to
rendered gameplay in one such session; its `docs/kegg/run_status.md` is the
worked log.

**The Watcom heap corrupts itself minutes in; heap pointers land in
A0000h.** → LE object bases are *link* bases. Real DOS/4G relocates the
image above 1 MB (that's what the internal fixups are for) and keeps the
low megabyte 1:1 (VGA at A0000h). Load at the link base and the C runtime's
sbrk grows the heap straight into the VGA aperture. `create_pm_runtime`
rebases +0x100000; analysis tools keep link addresses (`le_info --rebase`
maps between them). Found the hard way: KE's heap free-list head at a
planar-VGA address, shredded by plane writes.

**The game's own hardware-detection rejects a device you emulate (mouse,
SB...).** → It may not call the driver API at all: KE detects the mouse by
reading the real-mode IVT entry (flat linear 0xCC) through DOS/4GW's 1:1
low map. `dos_re.dos4gw.seed_low_memory` populates a power-on BIOS
environment — but seed IVT vectors *narrowly* (BIOS/DOS/mouse/IRQ ranges):
seeding everything non-null made KE probe VCPI (INT 67h looked installed).
The Watcom runtime also probes the extender with INT 21h AX=FF00h/AH=EDh
and INT 2Fh AX=1687h — each answer selects a startup path; KE's
`docs/kegg/symbol_ledger.md` maps the probe flow.

**The rendered frame shows 4 squeezed copies of the screen side by side.**
→ The game unchained VGA into Mode X (sequencer memory-mode chain-4 off;
how 90s action titles hit 70 fps). Linear A0000h bytes are now
plane-interleaved. `dos_re.dos4gw.VGASequencer` models the planes /
map-mask / latched write-mode-1 copies, and `render_pm_frame` composes
pixels from the planes at the CRTC display start. If a frame looks
right-but-frozen, check `display_start` — the game page-flips.

**You want the lifter on a 32-bit title.** → The whole pipeline has a PM
counterpart: `lift/decode32|cfg32|emit32|runtime32`,
`pm_verification.PMHookVerifier` (strict auto-continuation, full-machine
diff, samples cap), CLI `tools/pmlift.py` (census + emit + in-situ verify).
Flat Watcom C code lifts far better than 16-bit spaghetti — KE's census:
98% of the 300 most-called functions mechanically liftable, first 13 lifts
ORACLE_PASSING with zero divergences. `kegg_port/docs/kegg/
lifter_gap_analysis.md` is the porting story if dos_re ever needs another
ISA variant.

**A recorded demo replays differently from the recording (even crashes),
though input is re-injected at the exact frame boundaries.** → Some IRQ
source ticks on *wall clock* while recording but on the instruction count
(or fire-at-once) while replaying — KE's Sound Blaster block-complete IRQ.
That ISR lands mid-frame and steers the whole instruction stream: two
timelines. The 16-bit rule ("instructions ARE time",
`pre2/docs/live_view_timing_design.md`) applies to PM unchanged: every
reproducible path (record / replay / headless) must clock every IRQ from
`instruction_count`; only a casual live view may pace by wall clock
(`pm_player._configure_sound(deterministic=...)`). PM demos also record a
complete continuation state through `ReplayArtifact`; oracle/candidate
verification uses canonical endpoint comparison. Any change to
an emulated clock invalidates old recordings — re-record, don't debug them.

**The next routine to recover isn't a leaf — it calls helpers you haven't
recovered yet.** → Don't full-diff it. Split verifiers: pure leaves prove
under `pm_verification.PMHookVerifier` (strict full-machine diff); composed
non-leaves prove under `pm_composition.PMCompositionVerifier`, which diffs
only *observable* state — bytes written outside the routine's transient
stack frame `[min_esp, entry_esp)` — so nested calls' spill/scratch don't
count against it. Un-recovered callees are delegated with
`cpu.call_through(...)` (run the callee through the interpreter from inside
a hook: push args + sentinel return, run to it, clean up, IRQs suppressed).
Keep composed hooks in a separate module from strict-leaf hooks so the
strict verifier never sees them. Worked example: `kegg/composition_hooks.py`
(0x114085, the ball-vs-brick loop) + `kegg_port/docs/kegg/control_flow.md`.

**"What do I recover next, and how do I prove it?" needs a repeatable
corpus.** → A recorded gameplay demo *is* the corpus. Two tools close the
loop from the port root: the execution atlas replays N frames and
ranks hot call targets (static profile: ins/calls/INT/port-I/O; HOOKED
tagged) — the top un-hooked pure LEAF in the game's code region is usually
the next slice; `replay.verify_interval` replays the exact covered interval under the
differential verifier (`--focus 0xADDR` while iterating on one routine,
unfocused as the pre-commit pass). KE's level-2 demo proved `rects_overlap`
2364/2364 calls in one unfocused pass.

## Timing and speed

**Headless runs crawl because the game busy-waits (retrace/PIT spins).**
→ *Deterministic timing fast-forward*: collapse provably-identical poll
iterations in closed form, re-emitting every due IRQ at its emulated-time
point, never skipping across a pump boundary. The emulated timeline stays
byte-identical; the runtime identity changes, so derived boundary caches are
revalidated automatically. Worked examples:
`pre2/bridge/timing_fastforward.py` (+ `tests/test_timing_fastforward.py`,
whose mock-CPU sweep is the template for proving skip arithmetic) and the
simpler `overkill/timing_fastforward.py`. Read pitfalls #12–13 first.

**The interactive viewer runs too fast once waits are hooked.**
→ *Wall-clock parking* (a different mechanism from fast-forward — pitfall #14):
sleep until the real retrace phase matches, keep servicing IRQs/audio/input,
let the VM's own poll exit naturally. Design doc:
`pre2_port/docs/pre2/live_view_timing_design.md`.

**Forward traces hang forever at a timer flag.**
→ The game's ISR increments the flag; free-running the VM never delivers it.
Deliver the *real installed IRQ0 ISR* at the wait points (never poke the flag
— OK measured ~314 bytes of lost music/BIOS state from the poke shortcut).
Worked example: `overkill/timing_fastforward.py::advance_frames_fast`.

**Every probe/investigation replays minutes of VM to reach its target.**
→ Promote the target to a stable `ReplayPoint` and let `ReplayArtifact` cache
it. Each boundary stores complete non-memory continuation state plus only pages
changed from the artifact's base; it restores independently and is rejected
when any relevant identity changes.

## Bootstrap and cold start

**The packed EXE takes ages to boot; you want one canonical initialized state.**
→ *Static runtime bundle*: run the original bootstrap once to a declared
frontier, snapshot, and record a manifest (PSP tail, frontier address, memory
+ data-segment hashes). Worked examples: `overkill/static_runtime_bundle.py`,
`overkill/bootstrap_boundary.py`, doc
`overkill_port/docs/overkill/bootstrap_static_boundary.md`.

**The native port must cold-boot without the EXE (endgame).**
→ *Boot-data extraction*: trace which memory the game reads-before-writes
after bootstrap, extract those tables into native constants. Worked examples:
`pre2/native/boot_data.py` (the result), `pre2/probes/map_boot_reads.py` and
`pre2/probes/extract_boot_data.py` (how it was derived).

**The game arrived as a DOSBox save, not a clean boot.**
→ The core imports DOSBox-X saves (`dos_re/dosbox_savestate.py`); the adapter
locates the program inside the image by a code signature. Worked example:
`pre2/runtime.py::load_dosbox_savestate` (signature + segment derivation).

## Hard code shapes

**The game patches its own code at runtime (a routine has multiple live bodies).**
→ *Runtime-code staticization*: name every accepted variant, guard by
signature (the guards are already in `dos_re.hooks`), map each variant to an
explicit static Python owner; unknown bytes fail loud. Worked examples:
`overkill/runtime_code.py` (the slot/variant registry), doc
`overkill_port/docs/overkill/runtime_code_staticization.md`. To *find* the
patching: `overkill_port/scripts/trace_runtime_code_writes.py` and
`scripts/probe_ega_self_mod.py` (watch a code range for writes via
`mem.write_watchers`, report the patched spans).

**The game is procedural spaghetti (handler zoos, dispatch tables, choreography).**
→ The Overkill campaign is the worked example of decomposing it: handler
cross-referencing (`overkill/scripts/behavior_zoo_xref.py`), the generated
hook inventory (`overkill/scripts/gen_hook_inventory.py`), per-island truth
tables (`overkill_port/docs/overkill/island_truth_tables.md`), and the
actor-model recovery write-up (`docs/overkill/actor_model.md`). Goal: recover
the implicit actor/choreography model, not a rewrite of each handler.

## Audio

**The music driver is a resident segment doing millions of interpreted instructions.**
→ *Layered audio recovery* (never big-bang): asset decode → typed data model →
sequencer/tracker → mixer (verify: same state + events + timing → same PCM
block against the emulated device's `pcm_out`) → detach the ASM path. Worked
examples: `pre2/codecs/audio.py`, `pre2/recovered/tracker.py`,
`pre2/recovered/mixer.py`, plan in `pre2_port/docs/pre2/audio_architecture.md`
and the layered section of `docs/pre2/source_port_plan.md`.

**Audio makes bring-up unbearably slow before you care about it.**
→ *Fast-AdLib service*: replace the driver's delay/write loops with an
instant-return service during bring-up — reaches graphics fastest, mutes
music. Treat it as a distinct `ExecutionProfile` identity so replay caches
cannot cross the implementation change. Worked
example: `pre2/bootstrap_hooks.py` (`install_fast_adlib_service`).

**You need to hear/verify FM music without the game running.**
→ Capture the OPL register stream via `dos.adlib_callback`, render offline
through the OPL3 backend (`load_opl3`; optionally the external `pynuked_opl3`). Worked example: `pre2_port/scripts/render_music.py`.

## Automatic lifting (the first-draft accelerator)

**Not wanting to hand-translate a routine's first Python version from ASM.**
→ *The lifter* (`dos_re/docs/lifting_design.md`, framework-level): point it at
a function entry and it generates a literal, per-instruction Python hook —
ugly but faithful — then proves it byte-exact against the interpreted original
so you refactor from a verified artifact instead of decompiling from scratch.
`liftgen --report` censuses which entries are liftable and why not (indirect
jumps and x87 are the usual refusals); `liftgen --emit` writes the hooks;
`liftverify` installs each one and, every time it runs, diffs the full machine
state against the ASM oracle, reporting `ORACLE_PASSING` / `DIVERGED` /
`NOT_REACHED` into a proof ledger. ~95% of a lifted function is real
per-instruction Python (ALU/mov/flags/shifts/string ops calling the
interpreter's own helpers); the remainder are exact interpreter-fallback lines
that mark the to-do list. Calls and INTs run through the VM, so callees never
need lifting and lifted/hand-written hooks compose. Measured: 269/335 overkill
functions liftable, the lifted `4537` passes its hand-hook's 300-case fuzz
byte-exact (and was correct on a flag tail the *hand* version got wrong).

**Keeping "lifted" honest against "recovered".** → Lifted functions are
coverage of the *verification* frontier, not the *understanding* frontier, so
they live in their own `<game>/lifted/` tier with their own JSON proof ledger
(`dos_re.lift.manifest`), disjoint from `@oracle_link` islands. A lift becomes
recovered source only after the agent renames and simplifies it into clean
Python and tags it `@oracle_link` — with the same oracle test unchanged. Never
let a wall of unread-but-verified lifted code inflate the campaign's
"recovered %".

## Verification depth (the endgame)

**Proving a native port against the machine-backed oracle.** → Record one
`ReplayArtifact`, define stable points at the seams the game actually observes,
and compare the exact relevant interval. Machine-backed candidates use complete
continuation-state comparison. Detached native candidates project authoritative
state into the oracle's `CanonicalState` schema.

**Proving front-end and presentation flow.** → Put stable points at discrete
presentation and decision transitions instead of assuming that the oracle and a
native renderer share a frame clock. Capture consumed input as immutable replay
events and include every semantic field needed to prove equivalent choices and
transitions in the canonical projection.

**A divergence appears 10 minutes into a replay.**
→ Use `replay.bisect_divergence`. The artifact caches and annotates the latest
equivalent boundary, making the failing X→Y transition directly replayable.

**Tracking what the replay corpus actually covers.**
→ Use each artifact's function-visit index and the execution atlas to enumerate
covered functions, invocation counts, and first-entry/last-exit intervals. Report
unvisited behavior as a coverage gap.

## The play.py entry point (the human's window into the port)

`dos_re/dos_re/player.py` owns the standard
CLI (viewer by default / `--headless`; `--snapshot`/`--save-snapshot`;
`--record-demo`/`--play-demo`/`--demo-continue`; execution profiles;
pacing knobs), the viewer loop with the standard hotkeys (F10 screenshot,
F11 demo-record toggle, F12 snapshot), headless replay and crash snapshots.
Your project's only player subclasses `GameFrontend`; the worked
examples below are the GAME-SPECIFIC ideas you graduate into as the port
matures.

**Which pacing model? (the main thing ports actually differ in).**
→ Start with the library default: a fixed `--steps-per-frame` budget +
`--timer-irqs-per-frame` INT 08h ticks — no wall clock, the frame index IS
the demo clock, record/replay trivially deterministic. Graduate only when the
game demands it, cheapest first:
- **Deterministic tick-wait park** (simplest, no wall clock): the game paces
  off a timer-tick counter its INT 08h ISR bumps, but the driver delivers all
  of a frame's IRQs at frame start — so that counter is *constant for the whole
  step budget*, and any loop waiting on it spins out the rest of the budget for
  nothing. Classify those wait heads and end the frame the instant the game
  parks in one (byte-equivalent trajectory: the spins have no side effects).
  Worked example: `skyroads_port/skyroads/pacing.py` (`--frame-park`) — ~6×
  fewer interpreted steps on gameplay-heavy frames, keeping the deterministic
  fixed-budget clock.
- **Wall-clock model** (P2): PIT ch0 + the 70 Hz retrace on the WALL clock for
  live play, while demos keep a deterministic instruction-count clock
  (`pre2_port/scripts/play.py`, `docs/pre2/timing_hook_design.md` — read its §7
  before touching live pacing).
- **Modelled wait boundaries** (Overkill): present at the game's timer/retrace
  wait addresses from an emulator thread with a producer/consumer handoff
  (`overkill_port/scripts/play.py`).

Whatever you pick: every knob a replay must match goes into
`demo_metadata`/`apply_demo_metadata` and the execution-profile identity, or
artifact validation must reject the run.

**How big should `--steps-per-frame` be?** Size it *above* the game's peak
per-frame work, never toward the average. A budget below the real per-frame cost
isn't just a mid-work cut: the original ASM notices it isn't completing a logic
tick per frame and engages *its own* lag compensation, so the game plays
differently — still deterministic, but not original pacing (`pre2_port` warns
below chunk 20000, reaching natural pacing only from ~40000). This matters
*more* once you add a tick-wait park: the park makes the budget a **ceiling** the
common frame never reaches, so its value is set entirely by the rare heavy
frames — size it to peak + headroom (SkyRoads: measured peak 37.3k steps →
budget 48k). Because `steps_per_frame` lives in artifact metadata, playback
restores the recorded value.

**A hook tier safe enough to record demos over (`recording-safe override profile`).**
→ Classify hooks by WRITE-SET: render/audio-owned hooks (their writes cannot
touch the gameplay state a recording certifies) plus input-closed asset
decoders proven byte-identical over the whole asset set. Running only that
tier keeps the wall-clock playable for fluent human demo recording while
every gameplay byte still comes from original ASM. Worked example:
`pre2.checkpoints.SAFE_ORACLE_HOOKS` + the `recording-safe override profile` help text in
`pre2_port/scripts/play.py`.

**In-viewer verify/trace modes (`verification profile`, `instrumented development profile`).**
→ P2 runs the ASM as oracle and diffs each recovered replacement at its
contract boundary live, with a periodic per-hook summary (`--verify-verbose`,
`--full-verify` = whole-machine diff); Overkill adds a strict headless hook
verifier and a differential frame verifier behind the same flag family
(`--verify-frames`, budgets, PNG dumps). Worked examples:
`pre2_port/scripts/play.py`, `overkill_port/scripts/play.py` +
`overkill/verification.py`, `overkill/headless_verification.py`.

**Non-VGA video (CGA / Tandy / EGA pages / text) in the viewer.**
→ The library's default decoder covers VGA mode 13h + the 320x200 planar
path. Overkill's viewer decodes CGA `B800h` (palette-selectable) and Tandy,
publishing immutable VRAM snapshots to the UI thread. Worked example:
`overkill_port/scripts/play.py` (`--video cga|ega|tandy`, `--palette`).

**Viewer audio.**
→ Faithful OPL/AdLib: forward the VM's register stream to the vendored
Nuked-OPL3 backend (`dos_re.dos.set_adlib_callback` + the OPL3 backend; worked
examples: `overkill_port/scripts/play.py --adlib-audio`,
`ancient_port/scripts/play.py`). Digital SB-DMA games: pump the emulated DMA
blocks to the host mixer (worked example: P2 `--audio adlib`, and
`docs/pre2/audio_architecture.md` for the enhanced SDL_mixer path).

**Convenience fast-paths behind flags (never silent).**
→ Recovered accelerators the human toggles: P2's `--fast-song-load`
(byte-exact MOD-loader fast-forward) and `--fast-adlib` (mute the hot AdLib thunk to reach
graphics fastest). The pattern: default them by MODE, record them in demo
metadata, include them in the execution-profile identity, and document the
byte-exactness status in the help text. Worked
example: `pre2_port/scripts/play.py`.

## Progress and process machinery

**Measured progress reporting (interpreted vs native %, per-island).**
→ **Now a framework engine**: `dos_re/coverage.py` (`CoverageCollector` on
`cpu.coverage_telemetry`; the adapter supplies only the address→island
classifier — wire-up snippet in dos_re's `docs/agent_toolbox.md` §13). The
richer game-side build-out — region grouping, category rollups, dashboards,
oversized-file flags — remains the worked example: `overkill/coverage.py`,
`overkill/scripts/source_port_status.py`, `scripts/audit_islands.py`.

**Documenting a subsystem campaign so the next session can continue it.**
→ The *island doc* pattern: per-subsystem markdown with a truth table (facts /
guesses / frontiers), gap list, and merge plan. Worked examples:
`pre2_port/docs/pre2/renderer_island.md`, `player_fsm_island.md`,
`object_system_island.md`; the evidence-table format is
`docs/pre2/symbol_ledger.md`.

**Running the recovery loop unattended overnight.**
→ **Shipped in this repo**: `scripts/overnight_loop.sh` (the relaunch
harness — a fresh agent against a standing goal brief whenever one stops;
all state in git + the ledgers, so nothing is lost) + the goal-brief
template `examples/ledgers/overnight_goal.md` (preconditions checklist,
done-condition, gates, work-queue buckets; run_status's frontier statement
overrides the queue). Deploy it in the MIDDLE of the port — the hook/lift
grind after the game is fully runnable and the corpus spans gameplay
(ideally e2e cold-start demos); the corpus is what makes unattended commits
safe. Bring-up and the flip's design decisions stay attended.
Worked examples of a long campaign's brief evolving:
`overkill_port/scripts/overnight_loop.sh`,
`docs/overkill/overnight_endgame_execution.md`, `loop_blockers.md`. The
invariants are already in `START_HERE.md`; this is the harness that
enforced them for months.

**Shipping the finished detached product.**
→ Deployment pattern: copy the import-closure of the product entry point into a
standalone folder, prove every import resolves *inside* that tree, smoke-run
it, optionally wrap with PyInstaller. Worked example:
`pre2_port/scripts/deploy_native.py`.

## Presentation over a verified authoritative seam

**Making it look/feel modern without touching gameplay.**
→ [`enhancements.md`](enhancements.md) has the rules. The enhancement may
attach before the whole game is memoryless, but its authoritative input must
already be verified and read-only. Worked examples are
P2's enhanced layer: render-intent model (`pre2_port/docs/pre2/render_model.md`,
`enhanced_renderer_design.md`), frame interpolation over a two-snapshot rolling
window (`pre2/bridge/frame_capture.py`), smooth transitions
(`pre2/enhanced/transition_controller.py`), and the F10 overlay menu
(`pre2_port/scripts/overlay_menu.py`, pure pygame-surface UI).
