# Command-line tools

All active commands are listed here. Run `python tools/NAME.py --help` for
arguments. Commands are independent evidence, generation, verification, and
packaging operations; this table is not a mandatory recovery sequence.
Recovery IR, ReplayArtifact, Atlas, catalogs/configuration, planner, player,
and exporter remain the authorities.

| Commands | Purpose |
|---|---|
| `view.py`, `le_info.py`, `lindis.py`, `codemap.py`, `ea_census.py` | Inspect programs and produce static or observed evidence |
| `irgen.py` | Generate reproducible Recovery IR from declared inputs |
| `liftgen.py`, `liftemit.py`, `liftlink.py`, `pmlift.py` | Analyze, emit, and link generated implementations |
| `contract_census.py`, `cpuless_census.py`, `cpuless_closure.py`, `cpuless_promote.py` | Recover and validate CPUless/ABI contracts and closure evidence |
| `abi_blockers.py`, `abi_core_verify.py`, `abi_gate.py`, `abi_promote.py` | ABI-recovery evidence and differential gates |
| `liftverify.py` | Verify generated implementation behavior |
| `atlas.py` | Build, enrich, validate, and query the Execution Atlas |
| `replay_info.py` | Inspect a ReplayArtifact |
| `profile_hotspots.py` | Measure observed hot execution |
| `render_frame.py` | Render a captured frame for diagnosis |
| `audit_boot_image.py`, `pm_boot.py` | Materialize or audit declared bootstrap inputs |
| `audit_layers.py` | Audit source directories that declare machine-runtime independence |
| `lint.py`, `lint_cpuless.py`, `lint_independence.py` | Enforce framework and supporting recovery boundaries |
| `check_undefined_names.py`, `check_doc_links.py` | Static name and active-document validation |
| `export.py`, `verify_export.py` | Closed-world release export and hermetic verification |
| `new_project.py` | Scaffold a port repository |
| `run_tests.py` | Run the framework test policy |
| `clean.py` | Remove declared generated clutter |

Examples:

```bash
python tools/atlas.py create artifacts/atlas --program my-game:1
python tools/atlas.py ingest-replay artifacts/atlas artifacts/replays/gameplay --json
python tools/atlas.py ingest-facts artifacts/atlas atlas_facts.json
python tools/atlas.py ingest-ir artifacts/atlas --ir recovery_ir.json \
  --program my-game:1 --image-label GAME.EXE --image-sha256 SHA256 \
  --root FUNCTION_ID --product-profile game
python tools/atlas.py coverage artifacts/atlas game

python tools/export.py --factory project.release:build_export --output dist/game
python tools/verify_export.py --artifact dist/game -- python launch.py
```

The Atlas commands can ingest whatever static, replay, transfer, provenance,
and manual evidence exists. Generating a new implementation does not require
the entire program to pass through the same representation first.

Replay recording and playback are player operations, not separate tool formats:

```bash
python scripts/play.py --record-replay artifacts/replays/gameplay
python scripts/play.py --play-replay artifacts/replays/gameplay
```
