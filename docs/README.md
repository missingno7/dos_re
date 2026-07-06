# dos_re documentation

Start here — or, if you are the agent about to port a game, start at
[`../START_HERE.md`](../START_HERE.md) (the operational boot sequence).
Reading order for a newcomer: the repo [README](../README.md) →
`lifecycle.md` → `architecture.md` → `ai_porting_charter.md` → `pitfalls.md` →
`porting_new_game.md`.

| Doc | What it covers |
|---|---|
| [`pitfalls.md`](pitfalls.md) | **The 20 real mistakes** the source ports made — naming, hook bloat, verification narrowing, state-capture timing, determinism traps, SMC, layering — each with the consequence and the rule that fixed it. |
| [`lifecycle.md`](lifecycle.md) | **The story in order**: EXE-in-VM → hot-path islands → gameplay recovery → islands merge into subsystems → VM-less native port with the verification bridge → VM retires into the oracle seat. Defines the shared vocabulary (oracle, island, golden, hybrid, native). |
| [`architecture.md`](architecture.md) | The package boundary, the framework module map, execution modes, adapter layering, dependencies. |
| [`ai_porting_charter.md`](ai_porting_charter.md) | **The method, complete.** VM-as-oracle, the two invariants, the lifting loop, the proof spine, the determinism trap, the phased roadmap, the rules of engagement. Written for an AI agent (or human) given this framework and a DOS game. |
| [`methodology.md`](methodology.md) | The naming/altitude discipline: evidence ladder, status ladder (GUESS → CANONICAL), crystallization pyramid, hook lifecycle, fail-fast over guessed fallback. |
| [`hooks_and_verification.md`](hooks_and_verification.md) | Hook registration and return mechanics, the differential hook oracle (metadata + strict modes), the frame oracle, hook taxonomy. |
| [`demos_and_snapshots.md`](demos_and_snapshots.md) | Snapshots, repro artifacts, deterministic input demos (snapshot-anchored + cold-start), and the boundary-clock invariant that keeps demo proofs valid. |
| [`state_mirrors.md`](state_mirrors.md) | The state-view seam: human-named views over the DOS memory image with swappable backends, without weakening byte-exact verification. |
| [`porting_new_game.md`](porting_new_game.md) | The concrete bring-up checklist for a new game, step 0 → the lifting loop. |
| [`hardware_support.md`](hardware_support.md) | Honest status of the video/audio/timing/DOS models, and the rule for extending them. |

Related, outside `docs/`:

- [`../MIGRATION.md`](../MIGRATION.md) — where every file in this repo came from
  (pre2_port vs overkill_port), what was deliberately excluded, and what still
  needs cleanup.
- [`../examples/minimal_adapter/example.py`](../examples/minimal_adapter/example.py)
  — runnable 5-minute demo of the whole loop on a synthetic EXE.
- [`../examples/adapter_skeleton/`](../examples/adapter_skeleton/README.md) —
  the template for a new game adapter.
- [`../AGENTS.md`](../AGENTS.md) — working rules for agents/humans contributing
  to this framework repo itself.
