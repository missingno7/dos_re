# Porting a new DOS game — the bring-up checklist

This is the concrete path from "an original EXE + its data files" to "the
oracle-driven recovery loop is running". The full method is in
[`ai_porting_charter.md`](ai_porting_charter.md); this guide is the ordered
to-do list with the framework touchpoints named. If you are the porting agent,
[`START_HERE.md`](../START_HERE.md) is your boot sequence (it routes here) and
[`pitfalls.md`](pitfalls.md) is the list of mistakes already made for you.

## Know your game's style first

The two source projects span the spectrum, and the recovery emphasis differs:

- **Code-heavy procedural games** (Overkill): behaviour lives in handler zoos,
  dispatch tables, runtime-patched routines, hardcoded choreography. Expect to
  invest in handler classification, shared-primitive detection, routine-family
  grouping, and staticizing patched code — the goal is recovering the implicit
  actor/choreography model hiding inside the spaghetti, not rewriting each
  handler forever. The Overkill repo is the worked example of these tools.
- **Data-driven games** (script/bytecode-interpreter engines): behaviour lives
  in data the engine interprets. Expect to invest in loaders, format decoders,
  and verifying the interpreter's opcodes one by one — round-trip decode tests
  carry more of the proof there.

Most games mix both. Classify early (a few hours of tracing tells you), and
let it steer where the first islands go.

## 0. Set up the adapter

Copy the shape of [`examples/adapter_skeleton/`](../examples/adapter_skeleton/README.md)
into your own package (`mygame/`). From day one, enforce the boundaries:
`dos_re` never learns your game; your `recovered/` layer never imports `dos_re`.
Extend `tools/lint.py`'s pattern with those rules.

## 1. Load & run

```python
from dos_re.runtime import create_runtime
rt = create_runtime("assets/MYGAME.EXE", command_tail=b"")
rt.cpu.run(1_000_000)
print(rt.cpu.addr(), rt.cpu.instruction_count)
```

- If the EXE is packed (LZEXE etc.): the unpacker is *bootstrap, not gameplay*.
  Run it once (`dos_re/bootstrap_lzexe.py` accelerates the LZEXE 0.91 loop; a
  different packer needs its own accelerator or patience), then
  `write_snapshot` past it and work from the unpacked image.
- When the interpreter hits an unsupported opcode or DOS/BIOS call: decode the
  exact instruction, implement only the required behaviour, match flags for the
  observed use, add a focused test in `tests/test_core.py` style. Don't
  generalize beyond what the executable proves it needs.
- Add a snapshot point after init/first playable state.

## 2. See output, wire input

- See the screen: `python tools/render_frame.py <snapshot_dir>` renders a
  snapshot's video memory to PNG (VGA mode 13h and EGA/VGA planar, using the
  saved DAC palette and display start). If your game uses a mode it doesn't
  cover (CGA, Tandy, text), your adapter grows a rasterizer — the tool is the
  template.
- Deliver keys via `dos_re.interrupts.deliver_scancode` and confirm the game's
  key-state table updates (most action games poll their own INT 09h ISR state,
  not BIOS).

## 3. Find the frame boundaries

Identify, in the original code: the PIT/timer wait, the CRT retrace wait, and
the present/blit routine(s). `tools/profile_hotspots.py` (tight backward edges
= wait loops) and `tools/lindis.py` (read the code at a snapshot) are the
workhorses here. These addresses become your frame-verify boundary hooks and
`reference_env_hooks`.

## 4. Stand up the frame verifier

Adapter `frame_verify.py`: boundaries + a `sample_builder` (framebuffer +
visible VRAM first). Confirm a no-op candidate (no hooks) matches the oracle
frame-for-frame before trusting anything else.

## 5. Build the input-wait registry (before any demo)

Find the boundary-less poll loops (title/menu/"press fire") and register their
canonical head addresses in the adapter's `input_waits.py`, consumed by every
driver. Read [`demos_and_snapshots.md`](demos_and_snapshots.md) — recording
demos before this step produces proofs that freeze or lie.

## 6. Record the first demo

Drive menus into gameplay; confirm the demo replays identically under every
driver (interactive, headless, frame verifier). This demo is your first
regression asset.

## 7. Start the lifting loop

Start with the hot, well-bounded **leaf** routines — asset decompression and
decoders, blitters, tile/sprite drawing, palette handling. They have clean
verifiable boundaries, they make the interpreted VM dramatically faster, and
each one makes the system more observable (both source ports started exactly
here). Then move inward to the densest gameplay routines the profiler shows.

For each slice: trace → snapshot fixture → thin hook over a pure recovered
rule → declare its `HookStop` (or use strict mode) → verify against the ASM
oracle → document. One routine, one verification, per slice. See
[`hooks_and_verification.md`](hooks_and_verification.md), and
[`lifecycle.md`](lifecycle.md) for where this stage sits in the whole arc.

Tag every recovered function with `@oracle_link(boundary, contract, status,
merge_target)` from `dos_re.islands`, and generate your island manifest from
the code (`python tools/gen_island_manifest.py mygame.codecs mygame.recovered
-o docs/recovered_islands.md`) with a drift test. The ledger — not vibes — is
what tells you how far the port is.

## 8. Stand up coverage telemetry

`cpu.coverage_telemetry` records interpreted vs hooked instruction counts; map
addresses to islands (subsystems) in the adapter so "% of per-frame
instructions running native" becomes your headline progress metric.

## Then: the phased roadmap

Phase 1 lift rules → Phase 2 collapse understood chains → Phase 3 decode all
game data natively → Phase 4 earn the native world model → Phase 5 native
backends → Phase 6 flip the engine, keep the VM as oracle. Details and exit
criteria per phase: [`ai_porting_charter.md`](ai_porting_charter.md) §7.

## The endgame — what "flip the engine" concretely takes

These are the pieces the Prehistorik 2 port needed to go from "hybrid plays"
to "a standalone native game ships" (full rationale in
[`lifecycle.md`](lifecycle.md) stages 4–5):

1. **Boot constants.** Extract the post-bootstrap initialized state (the
   tables the EXE builds before the first frame) into native data, so the
   native game cold-boots from the data files alone — no EXE, no snapshot at
   runtime.
2. **A native state + tick driver.** A byte-backed game state (the state
   mirror) plus a fixed-step frame driver at the original tick cadence, with
   explicit pacing. Do **not** port the busy-wait/retrace machinery; preserve
   the heartbeat, not the spin.
3. **Per-subsystem equivalence contracts.** Gameplay byte-exact; rendering
   pixel-exact but mechanism-flexible; audio event-exact but mixer-flexible;
   input semantic-exact. Write these down for your game before flipping, or
   you will argue every divergence twice.
4. **The tick-equivalence harness.** Replay a recorded demo through the ASM
   oracle and the native core tick by tick and compare the data-segment image
   byte-exact. This — over a demo corpus that reaches death, respawn,
   level-end, and game-over — is the proof the flip changed nothing.
5. **A verification switch.** ON: the oracle runs beside the native game and
   diffs at boundaries. OFF: no VM starts. The shipped build contains no VM,
   no EXE, no fallback.

**Audio deserves its own plan** (usually the longest pole, charter §8).
Recover it in layers, with the emulated Sound Blaster/OPL + the original ASM
driver as the *oracle path*, never the final architecture: asset decode → a
typed data model → the sequencer/tracker → the mixer (verify: same state +
events + timing → same PCM block against the emulated device's output) → then
detach the native game from the ASM audio path entirely.

## What is game-specific (yours to write)

Boot constants and EXE identity/signatures; command-tail policy; asset codecs;
DGROUP layout + state views ([`state_mirrors.md`](state_mirrors.md)); hook
registrations + continuation metadata; frame boundaries + sample builder;
input-wait registry; island/coverage classification; the recovered logic
itself. The framework gives you the machine, the proof engines, and the
method — the knowledge of *your* game is earned from *your* oracle.
