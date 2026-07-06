# Roadmap

The framework is a living organism: it grows when a real game needs it to,
under the rules in [`AGENTS.md`](../AGENTS.md) (evidence-driven, stdlib-only
core, no invention without an oracle). This file separates "next", "when a
port needs it", and "long-term shape" so agents don't re-litigate scope every
session. Items graduate off this list into `MIGRATION.md` when they land.

## Short-term (framework-side, no game required)

- ~~tiny_frame_game example~~ **Done** (`examples/tiny_frame_game/`): a
  synthetic frame-loop game driven through oracle boot, cold-start demo
  record/replay, snapshot restore, hook oracle (wrong + correct), frame
  verifier lockstep, and a state mirror — also serving as the repo's only
  full-stack integration test.
- **Opt-in strict-ports mode** — today, reads from unmodeled ports return 0
  silently (proven behaviour for both source games; changing the default
  would invalidate their runs). Add an opt-in flag that logs or raises on
  unmodeled port I/O for recovery/audit sessions, plus a `tools/`
  audit-fallbacks scan.
- **Diagram pass** — the oracle loop and recovery-geography diagrams exist in
  ASCII; consider rendered versions if the docs ever get a site.

## When the next port needs it (parameterize-and-promote candidates)

These exist as proven, game-entangled code in the source repos and should be
promoted the moment a second game wants them — not before. **The
problem-indexed guide to all of them (symptom → technique → worked-example
path) is [`cookbook.md`](cookbook.md)**; MIGRATION.md §"documented-as-pattern"
is the provenance record. The shortlist:

- **Timing fast-forward** engine (closed-form wait collapsing; needs a
  per-game loop classification + clock model).
- **Tick-demo equivalence harness** (seed + per-tick keys + state digest).
- **Coverage-telemetry classifier** (the generic engine inside
  `overkill/coverage.py`, island taxonomy parameterized).
- **Headless verification driver** (pluggable runtime factory).
- **Probe harness + walk-shadow cache** (delta-encoded per-frame state cache).
- **Runtime-code staticization registry** (`RuntimeCodeSlot`/variant guards —
  the signature-guard primitives already live in `dos_re/hooks.py`).
- **Report generator / progress dashboard** (hooks by status, demo pass rate,
  interpreted-vs-native %, maturity counts, divergence list — the
  `source_port_status.py` pattern).
- **State-view extensions** — U32/pointer/bitfield/fixed-point descriptors,
  native↔byte sync helpers: add when a real game's layout demands them.
- **Boundary-clock abstraction** — today the boundary/clock contract is
  documented discipline (charter §6) + the adapter's input-wait registry;
  promote a shared `BoundaryClock` type if a second game's drivers start
  drifting despite the docs.

## Longer-term shape

- **CLI** (`dos-re init/run/record-demo/replay/verify-hook/verify-frames/
  report ...`) — a standard ritual so every port stops reinventing runner
  scripts. Design it *after* the tiny_frame_game example exists, so the CLI
  wraps a demonstrated workflow instead of speculation.
- **Two recovery styles as first-class guidance** — code-heavy procedural
  games (Overkill-like: handler zoos, dispatch tables, runtime-patched
  choreography) need handler-classification and routine-family tooling;
  data-driven games (script/bytecode interpreters) need loader/format/opcode
  verification tooling. The framework should serve both; today the docs note
  the distinction ([`porting_new_game.md`](porting_new_game.md)) and the
  Overkill repo holds the worked procedural example.
- **Issue templates / contribution scaffolding** — worth adding when a second
  contributor (human or agent fleet) actually shows up.
- **Paper/talk material** — the methodology docs are written to be
  distillable into a blog post / talk / paper; keep them that way.

## Non-goals (decided, don't reopen without new evidence)

- **General-purpose emulation** (DOSBox replacement, broad hardware matrices
  beyond what oracles demand).
- **Deep package restructure** of the flat `dos_re/` core into
  `cpu/ memory/ video/...` subpackages — the flat, tightly-coupled core is
  the proven shape from both source projects; cosmetic nesting adds churn
  and import risk without capability. Revisit only if the core grows well
  beyond its current ~25 modules.
- **Silently-degrading compatibility modes** of any kind.
