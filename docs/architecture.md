# dos_re architecture

`dos_re` is a reusable, oracle-driven DOS game recovery framework. The framework
runs an original DOS binary inside a deterministic real-mode VM, lets you replace
one original routine at a time with native code, and proves every replacement
byte-exact against the original — until the recovered code can stand alone as a
native source port and the VM is demoted to an offline proof harness.

## The package boundary (the one hard rule)

```text
dos_re/       the reusable, game-agnostic core: VM + verification engines.
              Stdlib + numpy.  Knows NOTHING about any specific game's addresses,
              filenames, video layout, or data formats.  Enforced by tools/lint.py.

<your game>/  the per-game adapter you create at your porting repo's root,
              next to its dos_re/ submodule (the expected workflow — see
              a port repo, which wires this framework in that way):
              hooks, continuation metadata, frame boundaries, input-wait
              registry, asset codecs, recovered logic, state views.
              See the Lemmings pilot (lemmings_port) and
              START_HERE.md step 2.

```

If a piece of code mentions a concrete address, video mode, or file format, it
belongs in the adapter — never in `dos_re`. This boundary is what makes the VM a
reusable oracle instead of part of one game.

## Framework module map

All modules live flat in the `dos_re/` package (they are tightly coupled by
design — cpu ⇄ memory ⇄ dos ⇄ runtime — and the flat layout is the proven one
from the source projects). Grouped by concern:

### The machine

| Module | What it is |
|---|---|
| `cpu.py` | The 8086/real-mode interpreter (`CPU8086`, `CPUState`): step/run loop, flags, replacement-hook dispatch, hook-verifier routing, per-instruction trace, coverage telemetry. Includes the 80186 ops and the 386-probe paths real games exercised. |
| `memory.py` | Flat 1 MB address space + segment helpers (`rb/rw/wb/ww`, `block`, `linear`), PSP creation, MZ program loading, BIOS-ROM write protection, and the EGA planar aperture (4 shadow bitplanes behind A000h with map-mask / read-plane / write-mode / latch semantics). |
| `mz.py` | MZ executable parser: header, relocations, load module, overlay. |
| `dos.py` | `DOSMachine`: INT 21h file/memory/console services, INT 10h video BIOS, INT 16h keyboard, INT 67h/XMS probes, and the port-level hardware models — VGA DAC + CRTC + attribute/sequencer/graphics controllers + retrace status, PIT channels 0 and 2, PC-speaker gate, AdLib/OPL2 register file with timer status, DMA/SB port routing. |
| `runtime.py` | `Runtime` = program + cpu + dos; `create_runtime()` boots an EXE into a power-on BIOS environment; `enable_sound_blaster()` attaches SB + PIC. |
| `pic.py` | 8259 PIC model (IRR/ISR/IMR, priority, EOI). |
| `sblaster.py` | Sound Blaster DSP + 8237 DMA channel model: detection, sample-rate/block programming, block-completion IRQs, snapshot/restore, and a detection-only stub mode. |
| `interrupts.py` | Synchronous interrupt delivery: read IVT vectors, run a handler to IRET, `deliver_scancode` (port 60h + the game's own INT 09h ISR). |
| `keyboard.py` | `KeyDispatcher`: holds each key ≥ 1 polled frame so same-frame make+break taps are never lost. |
| `bootstrap_lzexe.py` | Target-neutral LZEXE 0.91 unpacker-loop accelerator (bootstrap = extraction, not gameplay). |
| `le.py` | LE (Linear Executable) loader for DOS/4GW-bound titles: MZ-stub + `LE` parsing, object page mapping into a flat image, internal fixups (off32/sel16/ptr/selfrel), optional rebase (the runtime loads +0x100000 like real DOS/4G — link base collides the heap with the VGA aperture). |
| `cpu386.py` | The flat 386 protected-mode interpreter (`CPU386`, `FlatMemory`): 32-bit registers, default-32 operand/address sizes, ModRM+SIB, x87 subset, selector-base mini-descriptor table (flat by default; DPMI DOS blocks resolve base+offset), linear-EIP replacement-hook dispatch, IRQ delivery through the program-installed IDT, and the A000h aperture routing to the planar VGA model while unchained. |
| `dos4gw.py` | `DOS4GWHost`: the extender's protected-mode service layer — INT 21h/2Fh/10h/31h/33h grown from observed calls, DPMI DOS-memory allocation, the 8042 KBC (per-byte IRQ1 scancode queue), VGA DAC/sequencer/GC/CRTC port decode incl. `VGASequencer` (Mode X planes, write-mode-1 latched copies), `seed_low_memory` (1:1 real-mode IVT + BIOS data area, narrowly seeded), `render_pm_frame` (chained 13h / Mode X to RGB). |
| `asm.py` | Shared 8086 semantics helpers for *lifted* routines (INC/DEC preserving CF, REP string fast paths that respect the EGA aperture, …) so adapters don't re-derive flag behaviour per hook. |

### The proof engines

| Module | What it is |
|---|---|
| `hooks.py` | `HookRegistry` (`@registry.replace(cs, ip, name)`), duplicate-registration fail-fast, env-var hook disabling, the verifier-visible composition helpers `call_installed_hook_like_near_call` / `jump_installed_hook_boundary`, and the live-code signature guards (`self_disable_if_patched`, `code_matches`) for runtime-patched routines. |
| `gaps.py` | `HybridGap` — the fail-loud "not yet recovered" exception, plus the transition-signal subclass pattern for multi-frame sequences, and the `HookVerifyStats`/`HookTraceStats` bookkeeping. |
| `state_view.py` | The state-mirror machinery: typed views (`StructView`, `StructArray`, `U8/U16/S8/S16`) over swappable backends (byte image / segment / overlay contract / width contract) — the generic half of `docs/state_mirrors.md`. |
| `checkpoints.py` | VM-until-checkpoint stepping: run the oracle to the next adapter-declared phase boundary (frame/render/object-update/input), filterable by kind. |
| `step_probe.py` | Per-instance CPU step observers with an address-set TRAP — the cheap demo-scale probe primitive (`install_step_observer` / `step_observer`). The observer lives on ONE cpu instance (the other side stays JIT-hot) and with `trap` fires only at the given `(cs, ip)` entries, so a two-address probe over a 120M-instruction cold boot costs a few thousand Python calls, not 120M. Entry semantics (callback fires before the trapped instruction executes); observers nest and unwind LIFO. Promoted from the first completed port's probe harness; the enabler for the §12c audio gates and cheap flow probes. |
| `frontier.py` | Cold-start frontier triage: classify the last unhooked addresses (hook candidate / bootstrap / bounded rare branch / harmless tail) so coverage reports stay precise to the end. |
| `verification.py` | The differential **hook oracle**: clone the runtime, run the original ASM to the hook's continuation, run the hook, diff registers + flags + full memory. Metadata mode (`GenericHookStop` per address) or strict auto-continuation mode (no metadata). `OK_TRACE_HOOK=CS:IP` prints the ASM oracle trace on divergence. |
| `frame_verify.py` | The **semantic/frame oracle**: step a reference (pure ASM) and a candidate (hooked/native) runtime to adapter-defined frame boundaries, build `FrameSample`s, diff raw VRAM + rendered RGB, dump PNG/report artifacts on divergence. |
| `snapshot.py` | Full machine freeze/thaw (`write_snapshot` / `load_snapshot`): memory image + CPU + DOS + program metadata. Snapshots pin reproducible starting points and skip slow bootstraps. |
| `input_demo.py` | Deterministic input demos: record VM-visible key events keyed to an emulated boundary counter; replay into one or more runtimes. Supports snapshot-anchored demos and cold-start demos (boot fresh, replay from boundary 0), suffix extraction, and single-event delivery for menu poll waits. |
| `tick_demo.py` | The **endgame equivalence engine**: game-tick-keyed demos that replay identically in every mode — pure-ASM, hybrid, and VM-less native (the input demo's instruction clock is mode-dependent; the game tick is not). `TickDemo` (seed + per-tick consumed keys + gameplay digests + named u16 sidebands for record-and-inject state like PIT-fed idle timers), `masked_digest` (the ownership-mask fingerprint), `record_ticks` (step-hook recorder over adapter seam addresses; capture-at-consumption-point refine pattern), `verify_ticks` (inject → one native tick → digest compare). All ticks matching = the VM-less core provably reproduced the oracle over the whole recording. Generalized from the first completed port's `game_tick_demo`. |
| `frontend_timeline.py` | The **non-gameplay counterpart** to `tick_demo.py`: prove the VM-less front end (intro / title / menu / attract / map — the screens with NO game tick, where a port drifts undetected) behaves like the original, without a shared frame clock. The FOUR-GATE proof (toolbox §12b): `capture`/`collapse`/`filter_runs`/`diff_sequence` ([1] the logical screen ORDER), `pack_fields`/`diff_fields` ([2] an adapter-declared DECISION-STATE witness byte-compared at every screen transition), `diff_offsets` ([3] gameplay-entry equality outside an OWNED byte set — sound-driver data / load-layout / scene scratch a VM-less product legitimately owns differently), and `spread_beyond` ([4] the owned set proven INERT: dual-replay the recorded gameplay from both entry states; no tick may diverge outside the owned set). Input honesty: capture the raw key-flag window the VM's front end sampled per frame; `input_segments` + `SegmentedInput` feed it to the candidate CAUSALLY per screen, so presses land on the same screen at the same relative moment. `diff_pixels`/`rgb_sha` remain for cadence-aligned captures. Generalized from the first completed port's `verify_native_frontend`. |
| `pm_snapshot.py` | PM freeze/thaw/clone with ONE shared state list (capture/apply): `save_pm_snapshot`/`load_pm_snapshot` (directory form) and `clone_pm_runtime` (in-memory, fully independent — the PM verifier's oracle clones; open files reopen by path+position). |
| `pm_verification.py` | The PM differential hook oracle: strict auto-continuation over `clone_pm_runtime` — the handler's final EIP is the only acceptable continuation, the hook-stripped clone interprets the original there, then a full-machine diff (regs/flags/segments/x87/whole flat memory/VGA planes). Samples cap retires proven hooks to the passthrough fast path. |
| `repro_artifacts.py` | Divergence/crash repro capture: detached runtime clones + manifest. |
| `hook_taxonomy.py` | Role-based hook classification (checkpoint / env_wait / debug_probe / glue) with adapter-supplied address sets. |
| `runtime_code.py` | Polyvariant runtime-patched-code support: `RuntimeCodeSlot`/`RuntimeCodeVariant`/`RuntimeCodeStaticization` for addresses where the executable's live bytes aren't a single static routine — variant identification against a caller-supplied slot table (distinguishes "the hook's own body", "a known-but-wrong body", and "unknown bytes"), a staticization-readiness gate, and an opt-in `RuntimeCodeWriteTracer` for discovering installers. The richer, multi-variant sibling of `hooks.py`'s single-variant `self_disable_if_patched`/`code_matches` guards. |
| `islands.py` | `@oracle_link` recovered-island metadata (boundary, contract, confidence status, merge target) + auto-discovery and manifest generation — the generated progress ledger both source ports were steered by. |
| `coverage.py` | The measured **native-%** collector for `cpu.coverage_telemetry`: adapter-supplied address→island classifier, hooked work measured in verifier-reported ASM-equivalent instructions (estimated from a JSON cache on unverified runs, loudly *unmeasured* otherwise — never guessed into the %), `bounded_original` spans for oracle-side reference runs, per-island report. Promoted generic core of overkill's `coverage.py`. |
| `dosbox_savestate.py` | Import a DOSBox-X save state (memory + registers) as an alternative evidence source. |
| `testing.py` | Stdlib-only test discovery/runner (pytest fallback for constrained sandboxes). |

### The lifter (the VMless-stage pipeline — docs/dos_re_2.0.md)

| Module | What it is |
|---|---|
| `lift/install.py` | The two install tiers: `install_vmless_graph` (2.0 assembly — install EVERY emitted module; correctness judged end-to-end by the tick-boundary oracle, divergences localized by `tools/hook_bisect.py`) and `install_passing_lifts` (the hybrid tier — only ORACLE_PASSING modules, fingerprinted for demo determinism). `resolve_links` binds liftlink's cross-module `LINKS` tables. `tools/liftemit.py` batch-emits the census; `tools/liftlink.py` structurally links it. |
| `lift/decode.py` | Static 16-bit x86 decoder (lengths, control-flow class, branch targets) for the lifter — deliberately NOT a second semantic model; every non-transfer length is cross-checked against the interpreter (IP-delta probe) and disagreement refuses the function. OS-free (extractable for a future win16_re). |
| `lift/cfg.py` | Function-region discovery from an entry offset: reachable instructions, basic-block leaders, exits (ret/retf/iret/far-jmp), call/INT dependencies, and the structured refusal taxonomy (indirect-jump, unsupported-opcode, no-exit, region-budget, decoder-mismatch). `tools/liftgen.py` is the census + `--emit` CLI. See docs/lifting_design.md. |
| `lift/emit.py` | The emitter (M1): a `FunctionScan` → a self-contained Python module defining one literal hook — architectural state at every instruction boundary, a basic-block dispatch loop, per-line disassembly comments, the fail-loud SMC entry guard. Faithful by reuse: ALU/flags/shifts/string-ops call the interpreter's own helpers; unknown opcodes emit an exact single-instruction fallback. 95.4% native over 269 real overkill functions; the lifted `4537` passes its hand-hook's 300-case fuzz byte-exact. |
| `lift/runtime.py` | Support imported by generated hooks: `emulate_call`/`emulate_far_call`/`emulate_int` run callees and ISRs through the VM (hooks compose; lifting order is irrelevant), and `interp_one` executes one instruction through the interpreter for the emitter's fallback tier. 2.0 WALL NOTE: on a strict-VMless corpus the emitter emits ZERO `interp_one` sites (`liftemit --require-vmless-wall`) and the runner poisons interpretation outright (`cpu.interp_forbidden`) -- the fail-loud frontier lives OUTSIDE the corpus, never inside it (docs/dos_re_2.0.md section 1a). |
| `lift/decode32.py` + `lift/cfg32.py` | The 32-bit (flat CPU386) decoder + function scan: default-32 sizes, 0x66/0x67, ModRM+SIB, the 0F map; x87 classifies SEQ (the emitter falls back per line instead of refusing the function). Lengths cross-check against `CPU386`'s fetch stream. |
| `lift/emit32.py` + `lift/runtime32.py` | The 32-bit emitter (same faithful/total/refactorable contract over the CPU386 model: `_flags_*`/`_shift`/`_string` reuse, segment-base-aware flat addressing) and its delegation primitives (`emulate_call32`/`emulate_int32`/`interp_one32`, `check_signature`). `tools/pmlift.py` is census+emit+in-situ-verify in one CLI. |
| `lift/manifest.py` | The lifter's own proof ledger (`LiftManifest`/`LiftRecord`, JSON-backed): per-function status on the `LIFTED → ORACLE_PASSING → INSTALLED → REFACTORED` ladder, with call/verify/coverage counts. Deliberately disjoint from `islands.STATUSES` — lifted ≠ recovered (the metrics-honesty rule). `tools/liftverify.py` is the in-situ verify driver that installs lifted hooks with the strict auto-continuation verifier, runs the VM, diffs each call against the ASM oracle, and writes this ledger. |

### The frontend ring (the optional viewer — the human owner's window)

| Module | What it is |
|---|---|
| `player.py` | The game-agnostic core of every port's `scripts/play.py`: the STANDARD unified CLI (viewer by default, `--headless` to disable; `--snapshot`/`--save-snapshot`; `--record-demo`/`--play-demo`/`--demo-continue`; the four hook-mode flags `--no-replacements`/`--safe-hooks`/`--verify-hooks`/`--trace-hooks`, failing loud where a port has no such tier; pacing + presentation knobs), the live pygame viewer loop with the standard hotkeys (F10 screenshot, F11 demo-record toggle, F12 snapshot), headless demo replay, gap-snapshot-on-crash, and the `GameFrontend` adapter class a port subclasses. numpy/pygame imports stay lazy — importing the module and headless replay need neither. Worked example: the Lemmings pilot's runners (`lemmings_port/scripts/`). |
| `display.py` | GPU-accelerated (SDL2 streaming texture) window/present backend with a software fallback, aspect-correct letterboxing (DOS 4:3 `par`), overlays and fullscreen. Imported only when a window opens; together with `player.py` and `audio_sink.py` it forms the lint's declared FRONTEND_RING (the only package files allowed to use pygame). |
| `overlay_menu.py` | The NATIVE product's in-game settings menu (POST-ENDGAME widget — see [`post_endgame.md`](post_endgame.md)): tabbed modal overlay, pygame-INJECTED (importing needs nothing), items-as-data closures over host settings, structural determinism firewall (the caller freezes the tick while open; nothing reaches game input). Callers follow the accuracy taxonomy: presentation tabs (read-only, parity-gated) / an **Experimental** tab quarantining anything accuracy-affecting / debug-gated cheats. |
| `pm_player.py` | The PM (DOS/4GW) play runner: live viewer over `render_pm_frame`, set-1 KBC scancodes (E0 extended pairs), INT 33h mouse, wall-clock vsync pacing via `dos.time_source`, blocking console reads pump real keys, F10/F12, `--snapshot` resume, headless runs. `main()` is the CLI a port's `scripts/play.py` wraps; `tools/pm_view.py` is the zero-setup form. pygame stays lazy. |
| `audio_sink.py` | `AdlibSpeakerSink`: observer-only viewer audio — the VM's AdLib register stream through Nuked-OPL3 plus the PC-speaker square wave, mixed into one pygame channel with a jitter lead. Never writes game state, so demos replay identically with audio on or off. Wired by `player.py`'s `--audio adlib`; promoted from ancient_port's viewer. |
| `opl3_fast.py` | The CPython playback backend: a fast APPROXIMATE OPL3 (numpy block renderer, ~50x real-time on real game music, ~290x busy synthetic). Perceptually matched against the exact core — exact pitch math, calibrated ADSR, real stepped LFO patterns, the chip's actual rhythm phase-bit recipe, serial high-feedback recurrence — verified by `tests/test_opl3_fast.py` tolerance checks and an 80s real-music A/B. `dos_re.audio_sink.load_opl3()` defaults to `opl3_fast`; the EXTERNAL `pynuked_opl3` package (not a dos_re submodule) is an opt-in bit-exact upgrade via `DOSRE_OPL3_BACKEND=nuked`. The bit-exact pure-Python core was retired from the runtime (too slow at ~1x real-time) and now lives, dormant, in `graveyard/opl3_exact.py` — the calibration/golden reference for `opl3_fast`, never imported by the package or selected at runtime. |

### Repo layout

```text
dos_re/       the framework package (above)
docs/         framework reference docs (start at docs/README.md)
examples/     minimal_adapter/ (runnable end-to-end demo), tiny_frame_game/
              (full-stack demo) — new ports scaffold via tools/new_project.py
              (the retired 1.0 adapter_skeleton template is gone)
tests/        framework test suite (no game assets needed)
tools/        lint, test runner, cleaner, linear disassembler, hotspot profiler,
              hook-composition audit, pure-layer VM-leak audit, undefined-name
              guard, island-manifest generator, snapshot→PNG frame renderer,
              view.py (generic any-EXE runner over dos_re.player; display.py is
              a back-compat shim — both now live in the package)
```

## Execution modes (no silent fallbacks)

Every game port built on this framework runs in one of four explicit modes:

| Mode | What runs | Use |
|------|-----------|-----|
| **oracle / original** | pure original ASM in the VM | reference, observation, capturing oracles |
| **hybrid (workbench)** | recovered native replacements over the VM | preparing/recording new islands against the live ASM |
| **verify** | ASM oracle + recovered logic, diffed at contract boundaries | offline proof against recorded demos/snapshots |
| **native (product)** | recovered source only, NO VM | the standalone source port; shipping |

**No silent fallbacks.** If the hybrid runtime reaches unrecovered behaviour it
must fail loud with a precise gap report, turning the gap into the next task
instead of hiding it. An unrecovered path is never silently faked and never
silently falls back to ASM.

**2.0 stage runners are stricter still** (docs/dos_re_2.0.md section 1a): a
strict-VMless runner does not merely avoid interpretation -- it makes it
IMPOSSIBLE (`cpu.interp_forbidden` raises on any uncovered address), and the
EXE-independent runner boots from a generated data-only image with the
original binary physically absent (section 1a').

## Layering inside a game adapter

High = closest to ASM, low = closest to pure source. Dependencies point down
only; the pure layer never imports the VM.

| Layer | Role | May depend on |
|-------|------|---------------|
| **vm / orchestration** | `dos_re`: interpreter, verifiers, snapshots, demos | anything |
| **hook_boundary** | thin `@registry.replace` wrappers — no game logic | lifted, bridge, pure, vm |
| **lifted** | VM-aware Python reproducing an original routine byte/flag-exact | bridge, pure, vm |
| **backend** | rendering / sound / file I/O implementations | pure, bridge, vm |
| **bridge** | typed views projecting VM/DOS memory ⇄ named fields | pure, vm |
| **pure** | portable, VM-free game logic and data records | pure only |

See [`state_mirrors.md`](state_mirrors.md) for the bridge/view seam and
the retired 1.0 starter's methodology docs (historical) for the naming/altitude discipline that
keeps each layer honest.

## Third-party code and dependencies

The `dos_re` core is stdlib + numpy — numpy is the one first-class third-party
dependency (bulk pixel/array work in proof engines, renderers, digests; keep the
interpreter's per-instruction scalar path numpy-free — AGENTS.md has the measured
why). `tools/lint.py` enforces the boundary. Optional extras (`pyproject.toml`):
`pygame` for interactive viewers, `pytest` for the test suite.  OPL3 FM
synthesis is built in (`dos_re/opl3_fast.py`, numpy — the default when the
optional external pynuked_opl3 is not installed).
