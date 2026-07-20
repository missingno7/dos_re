# Command-line tools

All active commands are listed here. Run `python tools/NAME.py --help` for
arguments. These tools support the architecture; Recovery IR, ReplayArtifact,
Atlas, catalogs/configuration, planner, player, and exporter remain the
authorities.

| Commands | Purpose |
|---|---|
| `view.py`, `le_info.py`, `lindis.py`, `codemap.py`, `ea_census.py` | Execute and inspect original programs and recovered addresses |
| `irgen.py` | Generate canonical Recovery IR |
| `liftgen.py`, `liftemit.py`, `liftlink.py`, `pmlift.py` | Analyze, emit, and link generated implementations |
| `contract_census.py`, `cpuless_census.py`, `cpuless_closure.py`, `cpuless_promote.py` | Recover and validate CPUless/ABI contracts and closure evidence |
| `abi_blockers.py`, `abi_core_verify.py`, `abi_gate.py`, `abi_promote.py` | ABI-recovery evidence and differential gates |
| `liftverify.py` | Verify generated implementation behavior |
| `atlas.py` | Build, enrich, validate, and query the Execution Atlas |
| `replay_info.py` | Inspect a ReplayArtifact |
| `profile_hotspots.py` | Measure observed hot execution |
| `render_frame.py` | Render a captured frame for diagnosis |
| `audit_boot_image.py`, `pm_boot.py` | Materialize or audit declared bootstrap inputs |
| `audit_layers.py`, `gen_island_manifest.py` | Inspect dependency layers and recovered islands |
| `lint.py`, `lint_cpuless.py`, `lint_independence.py` | Enforce framework and supporting recovery boundaries |
| `check_undefined_names.py`, `check_doc_links.py` | Static name and active-document validation |
| `export.py`, `verify_export.py` | Closed-world release export and hermetic verification |
| `new_project.py` | Scaffold a port repository |
| `run_tests.py` | Run the framework test policy |
| `clean.py` | Remove declared generated clutter |

Common flows:

```bash
python tools/atlas.py build artifacts/atlas --ir recovery_ir.json \
  --program my-game:1 --image-label GAME.EXE --image-sha256 SHA256 \
  --root FUNCTION_ID --product-profile game
python tools/atlas.py ingest-replay artifacts/atlas artifacts/replays/gameplay
python tools/atlas.py coverage artifacts/atlas game

python tools/export.py --factory project.release:build_export --output dist/game
python tools/verify_export.py --artifact dist/game -- python launch.py
```

Replay recording and playback are player operations, not separate tool formats:

```bash
python scripts/play.py --record-replay artifacts/replays/gameplay
python scripts/play.py --play-replay artifacts/replays/gameplay
```
