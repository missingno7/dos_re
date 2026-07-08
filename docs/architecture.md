# dos_re architecture

`dos_re` is a reusable, oracle-driven DOS game recovery framework. The framework
runs an original DOS binary inside a deterministic real-mode VM, lets you replace
one original routine at a time with native code, and proves every replacement
byte-exact against the original — until the recovered code can stand alone as a
native source port and the VM is demoted to an offline proof harness.

## The package boundary (the one hard rule)

```text
dos_re/       the reusable, game-agnostic core: VM + verification engines.
              Stdlib-only.  Knows NOTHING about any specific game's addresses,
              filenames, video layout, or data formats.  Enforced by tools/lint.py.

<your game>/  the per-game adapter you create at your porting repo's root,
              next to its dos_re/ submodule (the expected workflow — see
              template_dos_port, which wires this framework in that way):
              hooks, continuation metadata, frame boundaries, input-wait
              registry, asset codecs, recovered logic, state views.
              See template_dos_port's examples/adapter_skeleton/ and
              START_HERE.md step 2.

pynuked_opl3/   submodule: optional OPL2/OPL3 FM-synthesis backend (cffi binding
              to Nuked-OPL3).  Independent of dos_re and of any game.
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
| `asm.py` | Shared 8086 semantics helpers for *lifted* routines (INC/DEC preserving CF, REP string fast paths that respect the EGA aperture, …) so adapters don't re-derive flag behaviour per hook. |

### The proof engines

| Module | What it is |
|---|---|
| `hooks.py` | `HookRegistry` (`@registry.replace(cs, ip, name)`), duplicate-registration fail-fast, env-var hook disabling, the verifier-visible composition helpers `call_installed_hook_like_near_call` / `jump_installed_hook_boundary`, and the live-code signature guards (`self_disable_if_patched`, `code_matches`) for runtime-patched routines. |
| `gaps.py` | `HybridGap` — the fail-loud "not yet recovered" exception, plus the transition-signal subclass pattern for multi-frame sequences, and the `HookVerifyStats`/`HookTraceStats` bookkeeping. |
| `state_view.py` | The state-mirror machinery: typed views (`StructView`, `StructArray`, `U8/U16/S8/S16`) over swappable backends (byte image / segment / overlay contract / width contract) — the generic half of `docs/state_mirrors.md`. |
| `checkpoints.py` | VM-until-checkpoint stepping: run the oracle to the next adapter-declared phase boundary (frame/render/object-update/input), filterable by kind. |
| `frontier.py` | Cold-start frontier triage: classify the last unhooked addresses (hook candidate / bootstrap / bounded rare branch / harmless tail) so coverage reports stay precise to the end. |
| `verification.py` | The differential **hook oracle**: clone the runtime, run the original ASM to the hook's continuation, run the hook, diff registers + flags + full memory. Metadata mode (`GenericHookStop` per address) or strict auto-continuation mode (no metadata). `OK_TRACE_HOOK=CS:IP` prints the ASM oracle trace on divergence. |
| `frame_verify.py` | The **semantic/frame oracle**: step a reference (pure ASM) and a candidate (hooked/native) runtime to adapter-defined frame boundaries, build `FrameSample`s, diff raw VRAM + rendered RGB, dump PNG/report artifacts on divergence. |
| `snapshot.py` | Full machine freeze/thaw (`write_snapshot` / `load_snapshot`): memory image + CPU + DOS + program metadata. Snapshots pin reproducible starting points and skip slow bootstraps. |
| `input_demo.py` | Deterministic input demos: record VM-visible key events keyed to an emulated boundary counter; replay into one or more runtimes. Supports snapshot-anchored demos and cold-start demos (boot fresh, replay from boundary 0), suffix extraction, and single-event delivery for menu poll waits. |
| `repro_artifacts.py` | Divergence/crash repro capture: detached runtime clones + manifest. |
| `hook_taxonomy.py` | Role-based hook classification (checkpoint / env_wait / debug_probe / glue) with adapter-supplied address sets. |
| `islands.py` | `@oracle_link` recovered-island metadata (boundary, contract, confidence status, merge target) + auto-discovery and manifest generation — the generated progress ledger both source ports were steered by. |
| `dosbox_savestate.py` | Import a DOSBox-X save state (memory + registers) as an alternative evidence source. |
| `testing.py` | Stdlib-only test discovery/runner (pytest fallback for constrained sandboxes). |

### Repo layout

```text
dos_re/       the framework package (above)
pynuked_opl3/   submodule: OPL2/OPL3 backend (optional, cffi)
docs/         framework reference docs (start at docs/README.md)
examples/     minimal_adapter/ (runnable end-to-end demo), tiny_frame_game/
              (full-stack demo) — the adapter_skeleton/ template now lives in
              template_dos_port
tests/        framework test suite (no game assets needed)
tools/        lint, test runner, cleaner, linear disassembler, hotspot profiler,
              hook-composition audit, pure-layer VM-leak audit, undefined-name
              guard, island-manifest generator, snapshot→PNG frame renderer,
              live interactive oracle viewer (view.py) and its GPU frame
              presenter (display.py) — the last two need numpy+pygame
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
template_dos_port's `docs/methodology.md` for the naming/altitude discipline that
keeps each layer honest.

## Third-party code and dependencies

The `dos_re` core is stdlib-only — this is enforced by `tools/lint.py`. Optional
extras (`pyproject.toml`): `numpy`/`pygame` for interactive viewers,
`cffi` to build the vendored `pynuked_opl3` backend, `pytest` for the test suite.
`pynuked_opl3` must remain independent of `dos_re` and of any game.
