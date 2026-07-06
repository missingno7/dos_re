# Cookbook — proven techniques that live in the source repos

Not everything the two ports invented could be promoted into this framework:
some mechanisms are inseparably welded to their game's addresses and layouts.
But every one of them **solved a problem your game will likely also have**,
and the worked example is sitting on disk. This is the problem-indexed map.

The worked examples live in the sibling repositories:
`D:\Games\DOS\pre2_port` (P2 — also github.com/missingno7/pre2_port) and
`D:\Games\DOS\overkill_port` (OK). Paths below are relative to those repos.
If neither is available on your machine, the entries below still carry the
essential shape of each technique — enough to re-derive it against your own
oracle; treat the missing example as lost convenience, not lost method.
When you re-implement one of these for a new game, read the original first —
each encodes debugging that took days — and if your version comes out generic,
promote it (see `roadmap.md`, "parameterize-and-promote").

## Timing and speed

**Headless runs crawl because the game busy-waits (retrace/PIT spins).**
→ *Deterministic timing fast-forward*: collapse provably-identical poll
iterations in closed form, re-emitting every due IRQ at its emulated-time
point, never skipping across a pump boundary. The emulated timeline stays
byte-identical — demos don't re-record. Worked examples:
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
→ *Walk-shadow cache*: record a demo's per-frame states once (delta-encoded;
~10–20 MB for an 8000-frame demo), replay in seconds, keyed by the demo file's
hash so it can't go stale. Worked example: `overkill/probes/_shadow_cache.py`;
the probe scaffolding around it is `overkill/probes/_harness.py`.

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
music, and is recorded in demo manifests so replays stay compatible. Worked
example: `pre2/bootstrap_hooks.py` (`install_fast_adlib_service`).

**You need to hear/verify FM music without the game running.**
→ Capture the OPL register stream via `dos.adlib_callback`, render offline
through `nuked_opl3`. Worked example: `pre2_port/scripts/render_music.py`.

## Verification depth (the endgame)

**Proving the native port equals the VM, tick by tick, over whole playthroughs.**
→ *Tick-demo harness*: record seed + per-tick input + a gameplay-state digest
(render-only ranges masked out — ONE digest definition shared with the forward
oracle), replay through both, compare per tick. Includes the non-obvious
lesson: state derived from instruction count (P2's idle-fidget timer) must be
*recorded and injected*, since the native port has no instruction count.
Worked examples: `pre2/native/game_tick_demo.py`,
`pre2_port/scripts/verify_finish_demo.py`, `scripts/verify_native_tick_demo.py`.

**A divergence appears 10 minutes into a demo.**
→ *Suffix repro*: `InputDemoPlayback.write_suffix` (already in the core)
carves a snapshot + rebased event tail at the divergence boundary — "resume
here, run 4 frames" instead of "replay everything". The frame verifier's
repro-artifact capture (`dos_re/repro_artifacts.py`) pairs with it.

**Tracking what the demo corpus actually covers.**
→ Corpus census: enumerate demos, their length, and which
levels/scenes/behaviours each reaches; blind spots are reported risks. Worked
examples: `pre2/probes/demo_census.py`, `pre2_port/docs/pre2/demo_manifest.md`.

## Progress and process machinery

**Measured progress reporting (interpreted vs native %, per-island).**
→ Coverage classifier + dashboards: the CPU's `coverage_telemetry` feeds an
island classifier; scripts render per-layer/per-island reports and flag
oversized files. Worked examples: `overkill/coverage.py`,
`overkill/scripts/source_port_status.py`, `scripts/audit_islands.py`.

**Documenting a subsystem campaign so the next session can continue it.**
→ The *island doc* pattern: per-subsystem markdown with a truth table (facts /
guesses / frontiers), gap list, and merge plan. Worked examples:
`pre2_port/docs/pre2/renderer_island.md`, `player_fsm_island.md`,
`object_system_island.md`; the evidence-table format is
`docs/pre2/symbol_ledger.md`.

**Running the recovery loop unattended overnight.**
→ The relaunch harness: a shell loop that re-starts a fresh agent against a
standing `/goal` brief whenever one stops (context limit, crash); all state
lives in git + the blocker file, so nothing is lost. Worked examples:
`overkill_port/scripts/overnight_loop.sh`,
`docs/overkill/overnight_endgame_execution.md` (the goal-brief shape),
`loop_blockers.md` (the blocker-ledger shape). The invariants are already in
`START_HERE.md`; this is the harness that enforced them for months.

**Shipping the finished native port.**
→ Deployment pattern: copy the import-closure of the native entry point into a
standalone folder, prove every import resolves *inside* that tree, smoke-run
it, optionally wrap with PyInstaller. Worked example:
`pre2_port/scripts/deploy_native.py`.

## Presentation (Stage 6 — only after the faithful game is complete)

**Making it look/feel modern without touching gameplay.**
→ [`enhancements.md`](enhancements.md) has the rules; the worked examples are
P2's enhanced layer: render-intent model (`pre2_port/docs/pre2/render_model.md`,
`enhanced_renderer_design.md`), frame interpolation over a two-snapshot rolling
window (`pre2/bridge/frame_capture.py`), smooth transitions
(`pre2/enhanced/transition_controller.py`), and the F10 overlay menu
(`pre2_port/scripts/overlay_menu.py`, pure pygame-surface UI).
