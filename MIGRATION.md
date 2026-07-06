# MIGRATION.md — how this repo was extracted

This framework was extracted (2026-07) from two working game-port repositories:

- **`pre2_port`** (Prehistorik 2) — *primary source of truth*: the newer,
  more evolved framework (`dos_re` core superset, PIC + Sound Blaster, richer
  VGA model, DOSBox-savestate import, LZEXE accelerator).
- **`overkill_port`** (Overkill) — *earlier sibling*: contributed features the
  pre2 line never needed, plus the vendored OPL3 backend and several tools.

Both repos already enforced the same hard boundary (`dos_re` must not know the
game), which is why the extraction is mostly verbatim: the core was analyzed
file-by-file and found free of game imports and game constants (comments
mentioning "PRE2 does X" were kept — they document *why* a hardware behaviour
is modeled and reference the oracle evidence).

## Copied from pre2_port (verbatim unless noted)

| Destination | Source | Notes |
|---|---|---|
| `dos_re/cpu.py`, `memory.py`, `mz.py`, `dos.py`, `runtime.py`, `hooks.py`, `interrupts.py`, `keyboard.py`, `pic.py`, `sblaster.py`, `snapshot.py`, `verification.py`, `frame_verify.py`, `repro_artifacts.py`, `testing.py`, `dosbox_savestate.py`, `bootstrap_lzexe.py`, `__init__.py` | `pre2_port/dos_re/*` | byte-identical copies |
| `dos_re/input_demo.py` | **overkill_port** version adopted (see below) — it is a strict superset of pre2's | one docstring word generalized |
| `docs/ai_porting_charter.md` | `pre2_port/dos_re/AI_PORTING_CHARTER.md` | Prehistorik-2 framing generalized (3 small edits) |
| `docs/methodology.md` | `pre2_port/docs/dos_re/source_port_methodology.md` | pre2 references generalized |
| `docs/state_mirrors.md` | generalized from `pre2_port/docs/pre2/state_view_layer.md` | game specifics removed; pattern kept |
| `dos_re/islands.py` | promoted from `pre2_port/pre2/islands.py` (an equivalent registry existed in overkill_port) | `@oracle_link` metadata + manifest generation; package list/manifest path parameterized; the six-level status ladder kept verbatim |
| `tools/gen_island_manifest.py` | generalized from `pre2_port/scripts/gen_island_manifest.py` | packages + output path became CLI arguments |
| `docs/lifecycle.md` late-stage content | synthesized from `pre2_port/docs/pre2/recovery_lifecycle.md`, `source_port_plan.md`, `renderer_goal.md` | per-subsystem equivalence contracts, the heartbeat rule, coastline shortening, one-leaf-many-adapters, boot constants, tick-equivalence harness, verification switch |
| `dos_re/gaps.py` | generalized from `pre2_port/pre2/gaps.py` | `HybridGap` fail-loud exception + the transition-signal subclass pattern + `HookVerifyStats`/`HookTraceStats`/`report`; the six Pre2 signal subclasses stayed behind as the worked example |
| `dos_re/state_view.py` | promoted from `pre2_port/pre2/bridge/dgroup_view.py` (the generic half: backends, descriptors, view bases) | `DGROUP_BASE` parameterized into `ByteBackend(source, base)` / `coerce_backend(source, base)`; descriptor names made public (`U8`/`U16`/...); the game layout views stayed behind |
| `tools/display.py` | copied from `pre2_port/scripts/display.py` | GPU-accelerated pygame presenter; zero game knowledge — only the window title changed |
| `docs/pitfalls.md` (P2 entries) | distilled from `pre2_port` docs: `camera_fidelity_bug.md`, `renderer_bug_table.md`, `live_view_timing_design.md`, `timing_hook_design.md`, `faithful_visual_layer.md`, `run_status.md` | each entry cites its consequence and the rule that fixed it |
| `docs/architecture.md` | new text, synthesized from `pre2_port/ARCHITECTURE.md` + `docs/architecture/package_boundary.md` + `third_party.md` | |
| `AGENTS.md`, `README.md` | new text, synthesized from both repos' `AGENTS.md`/`README.md`/`ARCHITECTURE.md` | |
| `tools/clean.py`, `tools/run_tests.py` | `pre2_port/scripts/` | artifact globs generalized; lint path updated |
| `tools/lint.py` | rewritten from `pre2_port/scripts/lint.py` | rule strengthened: core must be stdlib-only, not just "no pre2 import" |
| `tests/test_core.py` | `pre2_port/tests/` | one pre2-only test (bootstrap-to-segment-1030) removed |
| `tests/test_dos_re_smoke.py`, `test_input_demo.py`, `test_repro_artifacts.py`, `test_sblaster_snapshot.py`, `tests/__init__.py` | `pre2_port/tests/` | verbatim (input-demo tests extended, see below) |
| `tests/test_no_undefined_names.py` | `pre2_port/tests/` | retargeted from `pre2/` layers to `dos_re/` + `examples/` |
| `pyproject.toml`, `.gitignore` | adapted from pre2_port's | renamed `dos-re`, pre2 entries dropped |

## Copied from overkill_port

| Destination | Source | Notes |
|---|---|---|
| `nuked_opl3/` | `overkill_port/nuked_opl3/` | verbatim; fully generic cffi binding to Nuked-OPL3 (also referenced, but not present, in pre2_port) |
| `dos_re/input_demo.py` | `overkill_port/dos_re/input_demo.py` | overkill's version = pre2's **plus** cold-start demos (`write_start_snapshot=False`, `is_cold_start`) and `single=True` per-call event delivery for menu poll waits. Backward compatible with pre2-style demos. |
| `dos_re/asm.py` | `overkill_port/overkill/asm.py` | **promoted into the core**: game-neutral 8086 flag/register/string-op helpers for lifted routines. Docstring rewritten; imports made relative; `_ega_next_scanline_di` kept with an origin caveat (its interleave constants are the classic idiom but were verified on Overkill only). |
| `dos_re/hook_taxonomy.py` | `overkill_port/overkill/hook_taxonomy.py` | **generalized**: the 4-category classification kept verbatim in spirit; Overkill's hard-coded address tables became adapter-supplied `HookTaxonomy(checkpoints=..., env_waits=...)`. |
| `tools/lindis.py` | `overkill_port/scripts/lindis.py` | Overkill snapshot loader → generic `dos_re.snapshot.load_snapshot` + `exe` argument. Verified working. |
| `tools/profile_hotspots.py` | `overkill_port/scripts/profile_hotspots.py` | Overkill runtime loader / video-sound command tails / present-hook table → CLI arguments. Verified working. |
| `tools/audit_hook_oracle.py` | `overkill_port/scripts/audit_hook_oracle.py` | package path parameterized; Overkill-specific constants and the tandy-5A36 special check removed. |
| `tools/check_undefined_names.py` | `overkill_port/scripts/check_undefined_names.py` | target package parameterized (default `dos_re`). |
| `tests/test_frame_verify.py` | `overkill_port/tests/` | `overkill.frame_verify` WIDTH/HEIGHT import → local 320×200 constants. |
| `tests/test_nuked_opl3_vendor.py` | `overkill_port/tests/` | verbatim. |
| Docs content | `overkill_port` docs (`source_port_methodology.md`, `game_recovery_lifecycle.md`, `hook_naming_audit.md`, AGENTS.md) | concepts folded into `docs/hooks_and_verification.md` and `docs/architecture.md` where they added something pre2's newer docs lacked. |
| `dos_re/checkpoints.py` | generalized from `overkill/checkpoints.py` | checkpoint table parameterized instead of imported from the game package |
| `dos_re/frontier.py` | generalized from `overkill/frontier_manifest.py` | category enum + entry dataclass + summary; Overkill's 25-entry manifest stayed behind as game knowledge |
| signature guards in `dos_re/hooks.py` | generalized from `overkill/hook_wrappers/common.py` | `signature_matches`/`code_matches`/`self_disable_if_patched`/`interpret_current_instruction_without_hook`; the runtime-code staticization enforcement |
| `docs/pitfalls.md` (OK entries) + the loop protocol in `START_HERE.md` | distilled from `overkill_port` docs: `rescue_refactor.md`, `hook_naming_audit.md`, `refactor_plan.md`, `performance_investigation.md`, `runtime_code_staticization.md`, `loop_plan.md`, `loop_blockers.md`, `overnight_loop.sh` | the autonomous-loop invariants (smallest slice, never commit red, revert+log blockers, never weaken oracles) come from Overkill's overnight-loop practice |

## New in this repo (not copied)

| File | Why |
|---|---|
| `examples/minimal_adapter/example.py` | runnable end-to-end demo on a synthetic MZ EXE (oracle run → wrong hook caught → verified hook → snapshot determinism). Built from the proven patterns in `tests/test_dos_re_smoke.py`. Verified: runs green. |
| `examples/adapter_skeleton/` | template of the adapter shape both source ports converged on (runtime/hooks/verification/frame_verify/input_waits). |
| `tests/test_asm_and_taxonomy.py` | smoke coverage for the two modules promoted from overkill. |
| cold-start + `single=True` tests in `tests/test_input_demo.py` | the two merged overkill features had no dedicated tests in either repo. |
| `tests/test_islands.py` | framework-level coverage for the promoted island registry (the source repos' tests were bound to their game packages). |
| `docs/methodology.md` status-ladder fix | the copied doc's 5-level ladder predated the code; aligned to the proven 6-level ladder in `dos_re.islands.STATUSES` (adds RECOVERED). |
| `docs/hooks_and_verification.md`, `docs/demos_and_snapshots.md`, `docs/porting_new_game.md`, `docs/hardware_support.md`, `docs/README.md`, `MIGRATION.md` | new documentation consolidating both repos' methodology docs. |

## Deliberately excluded

- **All game logic**: `pre2_port/pre2/**` (~250 files) and
  `overkill_port/overkill/**` gameplay/recovered/bridge/native/checkpoints/
  probes layers. That *is* the per-game adapter; the skeleton documents its shape.
- **Game assets and executables**: nothing from either `assets/` was copied;
  `.gitignore` keeps `assets/` out permanently.
- **Overkill's CGA/EGA/Tandy renderers** (`overkill/rendering/*.py`): analyzed
  and found to be game-specific *lifted hooks* (hard-coded 1010:xxxx addresses,
  Overkill pixel-pair tables), not reusable hardware models. The truly generic
  part — the EGA planar aperture — was already in the core `memory.py`/`dos.py`.
  See `docs/hardware_support.md` for what this means for a CGA/Tandy game.
- **Overkill's sound drivers** (`sounds/adlib_driver.py`, `pc_speaker.py`,
  `timing.py`): lifted game driver code (segment 2032, DS offsets BEFF/BFxx).
  The generic OPL2/PIT/speaker *port models* they sit on are in the core `dos.py`.
- **Game-specific tools**: `trace.py`, `find_demo_divergence.py`,
  `capture_demo_snapshot.py`, `diag_video.py` (overkill_port) and `play.py`/
  `play_native.py`/`sdl_view.py`/`render_frame.py`/`deploy_native.py`
  (pre2_port) — all depend on the game adapter's runtime/frame config. Their
  *patterns* are described in the docs; write yours in your adapter (they are
  small: the divergence bisector is ~40 lines over `dos_re.frame_verify`).
- **Game-specific tests, fixtures, docs**: `symbols.json`, symbol ledgers,
  island docs, run-status logs, campaign docs, generated artifacts, `.pyc`
  caches, IDE folders.
- **`pre2_port/scripts/overlay_menu.py` + `display.py`**: generic-looking UI
  helpers, but they only serve a game viewer; left for adapters to copy from
  the source repos if wanted.

## Known cleanup / TODO (honest list)

1. **`dos_re/asm.py::_ega_next_scanline_di`** — generic *idiom*, Overkill-verified
   constants. Marked in its docstring; verify against your oracle before reuse.
2. **`bootstrap_lzexe.py`** is LZEXE-0.91-specific by design (signature +
   fixed exit offset). Other packers need their own accelerator; the module
   documents the pattern. Not generalized because no second packer has been
   through an oracle yet.
3. **`tools/audit_hook_oracle.py`** assumes the adapter keeps `HookStop`
   metadata in one `verification.py` dict — true of both source adapters, but a
   convention, not a mechanism. Parameterize further when a third adapter
   diverges.
4. **No generic CGA/Tandy rasterization or present model** (see
   `docs/hardware_support.md`). A CGA/Tandy target will write its own present
   path, as Overkill did.
5. **Comments referencing PRE2/OVERKILL remain in core modules** (e.g. dos.py's
   VGA notes). Kept deliberately as oracle-evidence citations; a purist sweep
   could rename them to neutral phrasing at the cost of losing the "why".
6. ~~`docs/state_mirrors.md` describes adapter code that does not ship here~~
   **Resolved**: the generic machinery was promoted as `dos_re/state_view.py`
   (second extraction pass); only the game layout tables remain adapter code.
7. **Input-demo cold-start replay is recorder/manifest-level tested**, but no
   end-to-end cold-start demo has been replayed inside *this* repo (both source
   repos did it against real games). Exercise it during your first port
   bring-up.
8. **`dos_re/bootstrap_lzexe.py` keeps private copies** of `code_matches` and
   `interpret_current_instruction_without_hook` that predate the canonical
   signature-guard versions in `dos_re/hooks.py` (which add variant tuples and
   telemetry). Left untouched because the bootstrap versions are proven and
   subtly different (they also restore `hook_names`); dedupe when either next
   changes for a real reason.
9. **Documented-as-pattern, deliberately not promoted** (game-entangled;
   the source repos hold the worked examples): timing fast-forward
   (`pre2/bridge/timing_fastforward.py`, `overkill/timing_fastforward.py` —
   see pitfalls #12–14), the tick-demo equivalence harness
   (`pre2/native/game_tick_demo.py`, `scripts/verify_finish_demo.py`), the
   coverage-telemetry classifier (`overkill/coverage.py`), headless
   verification (`overkill/headless_verification.py`), the probe harness +
   walk-shadow cache (`overkill/probes/_harness.py`, `_shadow_cache.py`), the
   runtime-code staticization registry (`overkill/runtime_code.py` +
   `static_runtime_bundle.py`), the frame-interpolation rolling capture
   (`pre2/bridge/frame_capture.py`), and the layer-audit / status dashboards
   (`scripts/audit_recovered_layers.py`, `source_port_status.py`). Each is a
   parameterize-and-promote candidate the moment a second game needs it.

## Missing / uncertain features (inherited state, not regressions)

- Roland MPU-401, GUS, Covox: never modeled in either source repo.
- OPL3-specific register features: the VM port model tracks OPL2-level state
  (enough for detection + register capture); `nuked_opl3` synthesizes OPL3 but
  nothing in the core exercises dual-register-set banks.
- Joystick (INT 15h / port 201h): not modeled; neither game used it.
- EGA write modes: 0 and 1 are implemented (plus read modes 0 and 1, including
  color-compare). Write modes 2–3 are **not** implemented and currently fall
  through to mode-0 semantics *silently* — a genuine violation of the fail-loud
  rule inherited from the source repos; worth a guard if your game sets them.
- Self-modifying code is handled by the interpreter naturally (it always reads
  live bytes), but *static* lifting of runtime-patched routines is methodology
  (see charter §8), not framework code.
