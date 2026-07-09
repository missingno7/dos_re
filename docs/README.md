# dos_re documentation

This is the framework's own reference manual — the mechanisms `dos_re/`
ships (the VM, the proof engines, the state-view seam) and their honest
status. It assumes you already know *why* you're here.

If you are the agent about to port a game, you want
[`template_dos_port`](https://github.com/missingno7/template_dos_port) instead: start
at its `START_HERE.md` (the operational boot sequence) and its `docs/` (the
method — lifecycle, charter, pitfalls, the porting checklist). `template_dos_port`
consumes this repo as its `dos_re/` submodule, and its docs link back into
this reference manual where relevant.

Reading order for a newcomer to the framework itself: the repo
[README](../README.md) → `architecture.md` → `hooks_and_verification.md` →
`demos_and_snapshots.md` → `state_mirrors.md` → `hardware_support.md`.

| Doc | What it covers |
|---|---|
| [`architecture.md`](architecture.md) | The package boundary, the framework module map, execution modes, adapter layering, dependencies. |
| [`hooks_and_verification.md`](hooks_and_verification.md) | Hook registration and return mechanics, the differential hook oracle (metadata + strict modes), the frame oracle, hook taxonomy. |
| [`demos_and_snapshots.md`](demos_and_snapshots.md) | Snapshots, repro artifacts, deterministic input demos (snapshot-anchored + cold-start), and the boundary-clock invariant that keeps demo proofs valid. |
| [`state_mirrors.md`](state_mirrors.md) | The state-view seam: human-named views over the DOS memory image with swappable backends, without weakening byte-exact verification. |
| [`hardware_support.md`](hardware_support.md) | Honest, status-legend-based matrix of the video/audio/timing/DOS models, the unmodeled-I/O policy, and the rule for extending them. |
| [`performance.md`](performance.md) | How to run dos_re fast: PyPy for headless workloads (~13-17x interpretation), pytest-xdist for suites, and the byte-exact equivalence-gate method required for any interpreter optimization. |
| [`glossary.md`](glossary.md) | Every project term (oracle, island, coastline, golden, heartbeat, …) in one table — shared vocabulary with `template_dos_port`'s methodology docs. |

Related, outside `docs/`:

- [`../examples/minimal_adapter/example.py`](../examples/minimal_adapter/example.py)
  — runnable 5-minute demo of the hook/verify/snapshot loop on a synthetic EXE.
- [`../examples/tiny_frame_game/`](../examples/tiny_frame_game/README.md) —
  the whole lifecycle in ten minutes: a synthetic frame-loop game through
  oracle boot, cold-start demos, snapshots, both verification oracles, and a
  state mirror.
- [`../AGENTS.md`](../AGENTS.md) — working rules for agents/humans contributing
  to this framework repo itself.

Porting methodology, the adapter template, and the file-provenance ledger
(`MIGRATION.md`) live in `template_dos_port`, not here.
