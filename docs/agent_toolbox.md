# Agent toolbox

This is the task-to-command index for the current dos_re 3.0 lifecycle.
Tool-specific arguments are documented by `python tools/NAME.py --help`.

## Validate the framework

```bash
python -m pytest -q
python tools/lint.py
python tools/check_undefined_names.py
python tools/check_doc_links.py
python examples/tiny_frame_game/walkthrough.py
```

`tools/run_tests.py` runs the framework test policy. `tools/clean.py` removes
declared generated clutter.

## Inspect an original program

```bash
python tools/view.py GAME.EXE
python tools/le_info.py GAME.EXE
python tools/lindis.py GAME.EXE SNAPSHOT CS START END
python tools/codemap.py ...
python tools/ea_census.py ...
```

Use `view.py` for interactive real/protected-mode execution through the unified
player. `le_info.py` inspects LE/DOS4GW images. `lindis.py` disassembles against
captured memory. `codemap.py` and `ea_census.py` gather recovery evidence.

## Retain Recovery IR and generate implementations

```bash
python tools/irgen.py ...
python tools/liftgen.py ...
python tools/liftemit.py ...
python tools/liftlink.py ...
python tools/pmlift.py ...
```

`irgen.py` creates canonical Recovery IR. The lift tools analyze, emit, and
link generated implementations; `pmlift.py` is the protected-mode pipeline.
Do not parse generated Python to reconstruct facts already owned by IR.

Supporting recovery commands are `contract_census.py`, `cpuless_census.py`,
`cpuless_closure.py`, `cpuless_promote.py`, `abi_blockers.py`,
`abi_core_verify.py`, `abi_gate.py`, and `abi_promote.py`. They produce or
validate per-implementation recovery evidence; they do not select a player.
`gen_island_manifest.py` and `audit_layers.py` inspect recovered dependency
islands and layer boundaries.

## Record and inspect replays

Port launchers pass replay operations to the unified player:

```bash
python scripts/play.py --record-replay artifacts/replays/gameplay
python scripts/play.py --play-replay artifacts/replays/gameplay
python scripts/play.py --replay-continue artifacts/replays/gameplay
python tools/replay_info.py artifacts/replays/gameplay
```

Real-mode and protected-mode adapters normalize inputs into ReplayArtifact.
No suffix replay, standalone repro, or separate snapshot recording format is a
current authority.

## Build and query the Execution Atlas

```bash
python tools/atlas.py build artifacts/atlas --ir recovery_ir.json \
  --program my-game:1 --image-label GAME.EXE --image-sha256 SHA256 \
  --root FUNCTION_ID --product-profile game
python tools/atlas.py ingest-replay artifacts/atlas artifacts/replays/gameplay
python tools/atlas.py validate artifacts/atlas
python tools/atlas.py coverage artifacts/atlas game
python tools/atlas.py show artifacts/atlas FUNCTION_ID
```

The Atlas consumes IR and replay evidence and returns conservative coverage.
It does not install implementations.

## Verify candidates

```bash
python tools/liftverify.py ...
python tools/abi_core_verify.py ...
python tools/render_frame.py ...
python tools/profile_hotspots.py ...
```

Use stable replay intervals and cached boundaries for oracle/candidate
comparison. Compare full continuation state when representations match or an
agreed canonical authoritative projection when they do not. `render_frame.py`
is a visual diagnostic, not an equivalence proof. `profile_hotspots.py`
measures execution without becoming coverage authority.

## Bootstrap, planning, and release

```bash
python tools/audit_boot_image.py ...
python tools/pm_boot.py ...
python tools/lint_independence.py ...
python tools/lint_cpuless.py ...
python tools/export.py --factory project.release:build_export --output dist/game
python tools/verify_export.py --artifact dist/game -- python launch.py
```

Bootstrap commands materialize or audit the provider selected by the execution
configuration. Independence/CPUless linters are supporting evidence, not
detachment authorities. `export.py` accepts only a package-ready release plan;
`verify_export.py` validates the exact artifact in a scrubbed environment.

## Project scaffolding

```bash
python tools/new_project.py my_game_port
```

The scaffold is a port-side adapter and artifact layout, not a fork of the
planner, replay system, or Atlas.
