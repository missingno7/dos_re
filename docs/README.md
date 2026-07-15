# dos_re documentation

**Audience: AI agents.** This is the framework's operating manual and
reference — the mechanisms `dos_re/` ships (the VM, the proof engines, the
lifter, the state-view seam) and their honest status. The only human-facing
document in this repo is the top-level [README](../README.md).

If you are the agent about to port a game, the *method* lives in
[`template_dos_port`](https://github.com/missingno7/template_dos_port) (start
at its `AGENTS.md`/`START_HERE.md`); come back here for the machinery. The
fastest route into this repo is
[`agent_toolbox.md`](agent_toolbox.md) — task → tool → command for every
recurring recovery job.

Reading order for the framework itself: the repo [README](../README.md) →
`dos_re_2.0.md` → `agent_toolbox.md` → `architecture.md` →
`hooks_and_verification.md` → `demos_and_snapshots.md` → `state_mirrors.md` →
`hardware_support.md`.

| Doc | What it covers |
|---|---|
| [`dos_re_2.0.md`](dos_re_2.0.md) | **The canonical architecture (read first)**: the staged recovery pipeline (interpreted oracle → VMless → CPUless → DOS-layout-less → semantic port), the oracle-guided-convergence risk model, the automation principle (tooling does the labor, AI unblocks), platform adapters, recovery facts, the verification bridge, milestones M1–M6. Supersedes older proof-before-integration language everywhere. |
| [`agent_toolbox.md`](agent_toolbox.md) | **The task index**: boot an EXE, diagnose fail-louds, snapshots, traces, frame boundaries, wait loops, demos, profiling, hooks, oracle verification, the lifter, LIFTED-vs-RECOVERED, progress metrics, guardrails — each with the command and when to use it. |
| [`architecture.md`](architecture.md) | The package boundary, the framework module map, execution modes, adapter layering, dependencies. |
| [`hooks_and_verification.md`](hooks_and_verification.md) | Hook registration and return mechanics, the differential hook oracle (metadata + strict modes), the frame oracle, hook taxonomy. |
| [`demos_and_snapshots.md`](demos_and_snapshots.md) | Snapshots, repro artifacts, deterministic input demos (snapshot-anchored + cold-start), the boundary-clock invariant that keeps demo proofs valid, tick demos — the mode-independent endgame clock (`dos_re.tick_demo`) — and front-end timelines (`dos_re.frontend_timeline`), the per-frame proof for the non-gameplay screens. |
| [`state_mirrors.md`](state_mirrors.md) | The state-view seam: human-named views over the DOS memory image with swappable backends, without weakening byte-exact verification. |
| [`hardware_support.md`](hardware_support.md) | Honest, status-legend-based matrix of the video/audio/timing/DOS models, the unmodeled-I/O policy, and the rule for extending them. |
| [`performance.md`](performance.md) | How to run dos_re fast: PyPy for headless workloads (~13-17x interpretation), pytest-xdist for suites, and the byte-exact equivalence-gate method required for any interpreter optimization. |
| [`lifting_design.md`](lifting_design.md) | The automatic lifter (LANDED): ASM function → generated literal Python hook → in-situ oracle verification. Design, failure policy, the proof ledger. Read its 2.0 supersession note first: per-function proof gates only the hybrid tier, never VMless graph assembly. |
| [`glossary.md`](glossary.md) | Every project term (oracle, island, coastline, golden, heartbeat, …) in one table — shared vocabulary with `template_dos_port`'s methodology docs. |

Related, outside `docs/`:

- [`../tools/README.md`](../tools/README.md) — one entry per CLI tool:
  command + when to reach for it.
- [`../examples/minimal_adapter/example.py`](../examples/minimal_adapter/example.py)
  — runnable 5-minute demo of the hook/verify/snapshot loop on a synthetic EXE.
- [`../examples/tiny_frame_game/`](../examples/tiny_frame_game/README.md) —
  the whole lifecycle in ten minutes: a synthetic frame-loop game through
  oracle boot, cold-start demos, snapshots, both verification oracles, and a
  state mirror.
- [`../AGENTS.md`](../AGENTS.md) — the rules for extending this framework
  (including the missing-behaviour → extension recipes).

Porting methodology, the adapter template, and the file-provenance ledger
(`MIGRATION.md`) live in `template_dos_port`, not here.
