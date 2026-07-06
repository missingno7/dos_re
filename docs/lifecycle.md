# The life of a port — from original EXE to native source port

The other docs each cover one mechanism in depth. This one tells the whole
story in order, so you can see where each mechanism belongs. The detailed
phase-by-phase treatment with exit criteria is
[`ai_porting_charter.md`](ai_porting_charter.md) §7; this is the narrative map.

The geography of recovery, in one picture:

```text
  raw ASM ocean          →   first verified islands   →   archipelagos        →   continents           →   recovered mainland
  (the original binary,      (hooked routines, each       (islands merged         (native subsystems:      (the VM-less source
   interpreted, opaque)       byte-exact vs oracle)        into subsystems)        renderer, gameplay)      port; VM = oracle)
```

## Vocabulary used throughout the docs

- **Oracle** — the original DOS executable running interpreted in the `dos_re`
  VM. The single source of truth for all behaviour.
- **Island** — one coherent recovered unit (a routine, then later a whole
  subsystem) with its own verification contract: pure recovered logic + a thin
  hook adapter + a verifier. Recovery grows island-by-island.
- **Golden** — a recorded oracle fixture turned into a test: the captured
  inputs/outputs/memory-effects of the original routine, which the recovered
  island must reproduce exactly, forever.
- **Coastline** — the total surface where recovered code still borders
  interpreted ASM (every hook boundary). Progress = the coastline moving
  *upward*: fewer, higher-level contact points, not more hooks.
- **Hybrid runtime** — the workbench: the VM running the original game with
  recovered islands hooked over it, live.
- **Native runtime** — the product: recovered source only, no VM, no EXE.

## Stage 0 — the original game runs in the VM

The project starts by getting the original EXE to boot and run inside the VM:
MZ loading, the packer bootstrap (run once, snapshot past it), DOS/BIOS
services, video output you can decode into an image, keyboard delivery the
game's own ISR sees, and deterministic input demos that replay a session
byte-for-byte. At this stage the original game **is** the game: it boots,
runs, accepts input, renders frames, and every run is replayable evidence.
([`porting_new_game.md`](porting_new_game.md) steps 1–6.)

Nothing is recovered yet — but the microscope works: you can trace any
routine, snapshot any state, and replay any moment.

## Stage 1 — first islands: the hot, well-bounded leaf routines

The first recovered pieces are usually the obvious low-level hot paths: asset
decompression and decoders, blitters, tile/sprite drawing, palette handling —
expensive or cleanly isolated routines. They are the right first targets for
three reasons: their boundaries are easy to verify (clear inputs/outputs, a
clean RET), replacing them makes the interpreted VM dramatically faster, and
each one makes the system more observable (decoded assets and framebuffers you
can look at). Both source ports started exactly here.

Each replacement goes through the same loop (one routine, one verification,
per slice): trace → snapshot fixture → thin hook over a pure rule → verify
against the interpreted original — registers, flags, full memory, and for
visual paths, frames. ([`hooks_and_verification.md`](hooks_and_verification.md).)

**Start the island ledger on the first island.** Every recovered function
carries `@oracle_link(boundary, contract, status, merge_target)`
(`dos_re.islands`), and the manifest is *generated* from the code
(`tools/gen_island_manifest.py` + a drift test), never hand-maintained. The
Prehistorik 2 port was steered by this ledger (~100+ islands at the end); the
`merge_target` field is what keeps Stage 3 honest — every island declares,
from birth, which larger subsystem it will dissolve into.

## Stage 2 — recovery moves inward: gameplay rules

With the workbench fast and observable, recovery moves into the game itself:
object update, collision, spawning, input decoding, menus, the level state
machine. Islands multiply — one decodes assets, another draws sprites, another
updates objects, another handles collisions. Each is still an isolated,
verified unit hooked at its original address; the VM still owns the loop and
the memory. (Charter Phase 1.)

## Stage 3 — islands connect into subsystems

A pile of low-level hooks is scaffolding, not the goal. As neighbouring
islands are proven, the glue between them (tails, helpers, per-row scan steps
— `glue` in the [hook taxonomy](hooks_and_verification.md#hook-roles-and-lifetimes-dos_rehook_taxonomypy))
collapses into single native chains, and verified behaviour is lifted into
higher-level representations: objects instead of slot bytes, render states,
level data, input state, audio command streams, animation state, game rules.
Higher-level names are *earned* from evidence, never invented up front
([`methodology.md`](methodology.md) — the crystallization pyramid). Correctness
during each collapse is protected by the frame/state verifier, not by
preserving historical hook boundaries. (Charter Phases 2–4.)

Two working rules from the source ports govern this stage:

- **Coastline shortening.** Where a recovered island returns to ASM but the
  callee is itself a verified recovered function whose contract covers the
  side effects, call it directly: grow the recovered↔recovered surface, shrink
  the ASM boundary. Collapse leaves into larger islands **only** with evidence
  from the real original call graph — never into a modern invented design.
- **One recovered leaf, many adapters.** Each recovered function is the
  *single* implementation; the hybrid replacement hook, the verify checkpoint,
  and (later) the native runtime are thin adapters over that one function —
  never a second copy. Duplicating logic between the hook and the native
  backend is how ports silently fork from their own proof.

The end-state shape of a subsystem is a **standalone, replaceable unit**: e.g.
a `render_frame(state)` that composes all recovered render leaves and, given a
state captured from a snapshot, reproduces that frame's VRAM byte-exact
*without stepping the VM*. Prove that with a committed test — it is the
demonstration that the subsystem is a clean, VM-independent, drop-in unit, and
the per-hook coastline for it collapses to one entry hook plus the state
bridge.

One trap to respect: some original state is **history-dependent** (a circular
scroll-page ring, a self-copying buffer). A from-scratch rebuild of such a
buffer is wrong even if a single frame matches — it needs the real stateful
model, recovered like everything else. Mark such pieces *blocked* rather than
guessing.

## Stage 4 — continents: the game separates from the VM

When the recovered subsystems cover the frame loop, data decoding, and the
backends (video/audio/input/timing), the game can run **without** the VM: a
native source port that cold-boots from the original data files plus recovered
boot constants — the post-bootstrap initialized state extracted once into
native data, so the shipped game needs **no EXE and no snapshot** at runtime
(snapshots and demos are recovery *evidence*, never a runtime dependency).
No emulator, no interpreted instruction in the hot path. (Charter Phases 5–6.)

This stage does not wait for Stage 3 to finish everywhere — convergence is
**bidirectional**. The native runtime starts as a backend *composing the same
recovered leaves* the hybrid runtime hooks (one leaf, many adapters), and an
unrecovered piece is a loud gap in native, never a silent fallback to the VM
or a guess from screenshots. Top-down pressure ("native needs X") is answered
by recovering X at the source layer, bottom-up — never by inventing it in the
backend.

**Verification is not lost at this step — that is the point of the state
mirror.** The native port keeps an address-shaped compatibility bridge: the
game state remains byte-compatible with the original memory layout where
verification needs it, but gameplay code reads it through human-named views
(`player.x`, `slot.sprite`), so readable code and byte-exact comparability
coexist. Offsets are quarantined in one bridge module; the "clean"
representation and the "verifiable" representation are the same bytes.
([`state_mirrors.md`](state_mirrors.md).) The mirror **verifies** the native
game; it does not **power** it — with verification off, no VM starts and no
projection runs.

### What "equivalent" means per subsystem

The VM preserves the original **machine**; the native port preserves the
original **game**. Byte-exactness is the contract only where the game *is*
bytes — insisting on machine-exactness everywhere (or nowhere) are both ways
to fail. The contracts the Prehistorik 2 port converged on:

| Subsystem | Contract |
|---|---|
| **Gameplay simulation** (player, objects, AI, collision, physics, score, **RNG**, timers, level state) | **strict / byte-exact**: same initial state + same input history + same tick → same game state. "Close enough" is not faithful. |
| **Rendering** | **pixel-exact, mechanism-flexible**: same recovered render state → same visible pixels + palette at the same frame boundary. The native renderer need not reproduce bitplanes, latches, or A000h tricks. |
| **Audio** | **event/timing-exact, mixer-flexible**: which sound, when, why, in what priority order. The mixer may be modern; it must never feed back into gameplay. |
| **Input** | **semantic/timing-exact, hardware-path-flexible**: the game-visible input state at tick boundaries must match; the hardware path may differ. |
| **Timing** | **same heartbeat, NOT same waiting machinery** (below). |

### The heartbeat

The native port must **not** reproduce the DOS waiting machinery — busy-waits,
vertical-retrace polling, host-speed delay loops. It preserves the **game tick
cadence**: an explicit, fixed-step heartbeat at the original rate, with the
frame/input/render/audio boundaries named in code. Removing the spin is what
creates the pacing obligation: the standalone runner paces gameplay to the
recovered tick rate — running "as fast as the display" is a bug, not
faithfulness.

## Stage 5 — the VM retires into the oracle seat

At the end, the VM is no longer a runtime dependency. It remains as the
offline proof harness behind a **verification switch**: ON, the oracle runs
beside the native game — a recorded demo replays through the ASM and the
native core **tick by tick** and the full data-segment image is compared
byte-exact (render/async offsets aside); OFF, no VM starts and the native game
runs by itself. The VM is the microscope for regressions, the debugger for
divergences, and the standing proof that the native port still behaves like
the original game. The original binary was the source of truth on day one and
is still the source of truth on the last day — what changed is what *runs*.

**Mantra:** the VM preserves the original machine; the source port preserves
the original game. Hybrid prepares the code; native plugs it in; the oracle
proves it did not drift.

## The two invariants that hold through every stage

1. **Always runnable** — the game is playable/observable at every commit; no
   big-rewrite branch.
2. **Always verified** — nothing replaces original behaviour until it has been
   diffed against the original. An unrecovered path fails loud; it is never
   silently faked and never silently handed back to the ASM.
