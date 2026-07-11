# tools/ — the mechanical helpers

One entry per tool: the command and when to reach for it. The task-oriented
view (which tool for which recovery step, with context) is
[`docs/agent_toolbox.md`](../docs/agent_toolbox.md).

## Run / see / read

| Tool | Command | When |
|---|---|---|
| `view.py` | `python tools/view.py --exe assets/GAME.EXE` | Watch any EXE run, zero setup — the standard player CLI (`--headless`, `--snapshot`, `--record-demo`, `--play-demo`; F10/F11/F12). Your port's `scripts/play.py` supersedes it once an adapter exists. |
| `render_frame.py` | `python tools/render_frame.py <snapshot_dir>` | Day-0 "see output": snapshot (or `--exe` + `--steps`) → PNG. VGA 13h + EGA/VGA planar. |
| `lindis.py` | `python tools/lindis.py <exe> <snapshot_dir> <CS> <START> <END>` | Read code: linear disassembly at a snapshot (static lengths, interpreter-rendered text). |
| `profile_hotspots.py` | `python tools/profile_hotspots.py <exe> <steps> --snapshot <snap> --top 40` | FIRST, before manual tracing: hot routines, tight backward edges (= wait loops / frame boundaries), boundary crossings. |

## Lift / verify

| Tool | Command | When |
|---|---|---|
| `liftgen.py` | `python tools/liftgen.py --exe <exe> --snapshot <snap> --entries-file <txt>` | Census: which function entries are mechanically liftable, and the refusal reason for the rest. `--emit` writes the literal hooks. |
| `liftverify.py` | `python tools/liftverify.py --exe <exe> --snapshot <snap> --entry CS:IP --steps N --emit-dir <game>/lifted` | Lift + prove in situ: every call diffed against the ASM oracle; writes the `LIFTED → ORACLE_PASSING` proof ledger. Never hand-translate a first draft. |
| `gen_island_manifest.py` | `python tools/gen_island_manifest.py <pkg>… -o docs/recovered_islands.md` | Regenerate the recovered-island ledger from `@oracle_link` tags. Generated, never hand-edited. |
| `tick_demo_info.py` | `python tools/tick_demo_info.py <demo.bin>` | Inspect an endgame tick-demo recording (ticks, key record, sidebands, seed) before trusting it — corpus census, stale-file diagnosis. |

## Guardrails (run with every change)

| Tool | Command | Catches |
|---|---|---|
| `lint.py` | `python tools/lint.py` | Game knowledge or third-party imports leaking into the `dos_re/` core; syntax errors. |
| `audit_layers.py` | `python tools/audit_layers.py <game>/recovered` | VM imports creeping into the pure recovered layer (the mistake that makes logic unmigratable). |
| `audit_hook_oracle.py` | `python tools/audit_hook_oracle.py <game>` | Parent hooks calling child hooks' Python directly — hiding the child from verification. |
| `check_undefined_names.py` | `python tools/check_undefined_names.py [pkg]` | Latent NameErrors (F821) on paths tests didn't reach. |
| `check_doc_links.py` | `python tools/check_doc_links.py [root …] [--exclude NAME]` | Broken relative markdown links — run after any doc edit; porting repos run it as `python dos_re/tools/check_doc_links.py . --exclude dos_re`. |
| `run_tests.py` | `python tools/run_tests.py` | Pytest-free fallback test runner for constrained sandboxes. |
| `clean.py` | `python tools/clean.py [--artifacts]` | Generated junk; `--artifacts` also drops regenerable artifact families (promoted evidence stays). |

`display.py` is a back-compat shim over `dos_re.display` (kept so old
`from display import Display` imports keep working); use the package module
in new code.
