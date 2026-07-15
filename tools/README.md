# tools/ — the mechanical helpers

One entry per tool: the command and when to reach for it. The task-oriented
view (which tool for which recovery step, with context) is
[`docs/agent_toolbox.md`](../docs/agent_toolbox.md).

## Run / see / read

| Tool | Command | When |
|---|---|---|
| `view.py` | `python tools/view.py --exe assets/GAME.EXE` | Watch any EXE run, zero setup — the standard player CLI (`--headless`, `--snapshot`, `--record-demo`, `--play-demo`; F10/F11/F12). Your port's `scripts/play.py` supersedes it once an adapter exists. |
| `pm_view.py` | `python tools/pm_view.py --exe assets/GAME.EXE` | Watch any DOS/4GW (MZ+LE) EXE run, zero setup — live viewer over the flat 386 runtime (KBC keys, INT 33h mouse, wall-clock vsync, F10/F12, `--snapshot` resume, `--headless`). A port's `scripts/play.py` (thin wrapper over `dos_re.pm_player.main`) supersedes it. |
| `render_frame.py` | `python tools/render_frame.py <snapshot_dir>` | Day-0 "see output": snapshot (or `--exe` + `--steps`) → PNG. VGA 13h + EGA/VGA planar. |
| `lindis.py` | `python tools/lindis.py <exe> <snapshot_dir> <CS> <START> <END>` | Read code: linear disassembly at a snapshot (static lengths, interpreter-rendered text). |
| `profile_hotspots.py` | `python tools/profile_hotspots.py <exe> <steps> --snapshot <snap> --top 40` | FIRST, before manual tracing: hot routines, tight backward edges (= wait loops / frame boundaries), boundary crossings. |
| `le_info.py` | `python tools/le_info.py assets/GAME.EXE` | Day-0 for a DOS/4GW (MZ+LE) title: object table, entry/stack, fixup census, entry disassembly. `--rebase 0x100000` prints addresses where the runtime loads them. |
| `pm_boot.py` | `python tools/pm_boot.py --exe assets/GAME.EXE --png frame.png` | The protected-mode bring-up loop: run an LE on the flat 386 runtime to the fail-loud frontier; stop reason + recent/hot EIPs + unmodeled ports + screen render (13h or Mode X). `--keys`/`--scancodes --at N` drive input. |
| `pmlift.py` | `python tools/pmlift.py --exe GAME.EXE --auto-entries 300 --census` / `--verify --steps N` | The 32-bit liftgen+liftverify: census entries (static scan over decode32, `--auto-entries` sweeps direct call targets), emit literal Python hooks, install under the strict PM differential verifier, report ORACLE_PASSING / DIVERGED / NOT_REACHED per hook (samples cap retires proven hooks). |

## The 2.0 assembly pipeline (docs/dos_re_2.0.md)

`codemap → liftemit → liftlink → install_vmless_graph (in the port's play_native) → end-to-end oracle → hook_bisect` — assemble the largest supported VMless graph early, judge it end-to-end, localize divergences automatically.

| Tool | Command | When |
|---|---|---|
| `codemap.py` | `python tools/codemap.py …` | Observed-execution census: the entry list the whole pipeline consumes. |
| `liftemit.py` | `python tools/liftemit.py --exe <exe> --snapshot <snap> --entries-file <txt> --emit-dir <game>/lifted` | Batch-emit the whole census to VMless lifted modules in one pass (byte-identical to liftverify's emit recipe). The bulk-emission step. |
| `liftlink.py` | `python tools/liftlink.py --exe <exe> --snapshot <snap> --entries-file <txt> --emit-dir <game>/lifted` | Structural linking (default): near-CALL edges between lifted census entries with all-near-ret exits become direct Python calls. `--proven-edges` restores the 1.x ORACLE_PASSING gate (hybrid/debug only). |
| `hook_bisect.py` | `python dos_re/tools/hook_bisect.py --driver <game>.bisect_driver:Driver --boundaries N` | When the assembled graph diverges from the oracle: binary-search the installed set to the smallest responsible function. Run from the port root. |

## Lift / verify (per-function diagnostics + the hybrid tier)

| Tool | Command | When |
|---|---|---|
| `liftgen.py` | `python tools/liftgen.py --exe <exe> --snapshot <snap> --entries-file <txt>` | Census: which function entries are mechanically liftable, and the refusal reason for the rest. `--emit` writes the literal hooks. |
| `liftverify.py` | `python tools/liftverify.py --exe <exe> --snapshot <snap> --entry CS:IP --steps N --emit-dir <game>/lifted` | Lift + prove in situ: every call diffed against the ASM oracle; writes the `LIFTED → ORACLE_PASSING` proof ledger. Feeds the hybrid auto-install tier and per-function diagnostics — NOT a gate on VMless graph assembly. |
| `gen_island_manifest.py` | `python tools/gen_island_manifest.py <pkg>… -o docs/recovered_islands.md` | Regenerate the recovered-island ledger from `@oracle_link` tags. Generated, never hand-edited. |
| `tick_demo_info.py` | `python tools/tick_demo_info.py <demo.bin>` | Inspect an endgame tick-demo recording (ticks, key record, sidebands, seed) before trusting it — corpus census, stale-file diagnosis. |
| `pm_verify_demo.py` | `python dos_re/tools/pm_verify_demo.py --exe <exe> --demo <bundle> --install pkg.mod:install_hooks [--focus 0xADDR]` | The PM recovery proof loop: replay an input-demo bundle with `PMHookVerifier` diffing every hooked call against the interpreted original. `--focus` = fast loop while recovering one routine; unfocused = the pre-commit full pass. Run from the port root. |
| `pm_census.py` | `python dos_re/tools/pm_census.py --exe <exe> --demo <bundle> --install … --region 0x110000:0x120000 [--leaf-only]` | "What do I recover next": rank the demo's hot `E8` call targets, statically profiled (ins/calls/INT/port-I/O, HOOKED tag). The top un-hooked pure LEAF in the game's code region is usually the next slice. |

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
