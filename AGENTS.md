# AGENTS.md — dos_re framework repository

These instructions apply to the repository. Start with
[`README.md`](README.md), [`docs/getting_started.md`](docs/getting_started.md),
and [`docs/architecture.md`](docs/architecture.md).

## Architectural rule

Extend an existing authority; do not create a parallel one. Recovery operations
are optional and composable, but shared artifacts meet through stable
identities, explicit provenance, and conservative uncertainty.

- Recovery IR owns retained static decoded facts.
- ReplayArtifact is the only persistent deterministic replay format.
- Explicit fact documents own reviewed manual evidence.
- The Atlas is a materialized query/index projection over cited evidence.
- ImplementationCatalog is the only available-implementation inventory.
- ExecutionConfiguration is the only composition and policy input.
- The planner is the only implementation-selection and dependency-closure
  authority.
- Player and backend adapters consume a validated plan without fallback.
- Verification compares oracle and candidate selections; it never selects code.
- Export packages one exact package-ready plan.

No tool is a mandatory global stage. Interpreted, VMless, CPUless,
ABI-recovered, DOS-memory-backed, memoryless, and native are per-implementation
properties. Use “hook” only for low-level interception, “override” for an
authored catalog implementation, and “replay” for ReplayArtifact operations.

## Framework rules

- Keep `dos_re/` game-agnostic. Game addresses, filenames, assets, formats, and
  recovery declarations belong in a port.
- Generated code is reproducible output. Fix a generic generator or an
  evidence-backed project declaration, then regenerate.
- Fail loud. Unsupported code, devices, coverage, bootstrap, and execution
  behavior must not acquire a plausible fallback.
- Correctness claims require cited evidence. Faithful changes need focused
  tests and oracle/replay verification proportional to their scope.
- Observed absence is never proof of unreachability.
- Deterministic execution is the default proof path; wall-clock behavior is an
  explicit service.
- Keep pygame in frontend/backend adapters. `import dos_re` must not import UI
  dependencies.
- Update active documentation and `tools/README.md` with public API or CLI
  changes. Put obsolete design history under `docs/history/`.
- Do not keep compatibility shims for disposable downstream scripts, generated
  artifacts, or recordings.

## Where changes go

| Need | Owner |
|---|---|
| Stable cross-artifact identity | `dos_re/identity.py` |
| Retained static decoded fact | `dos_re/lift/ir.py` and its producers |
| Recorded event/state/visit/transfer | `dos_re/replay.py` and replay adapters |
| Evidence projection, graph query, inverse replay index | `dos_re/atlas.py` |
| Coverage, catalogs, configuration, planning, bootstrap declarations | `dos_re/execution.py` |
| Bootstrap artifact consumption | `dos_re/bootstrap_runtime.py` |
| Machine-specific construction or I/O | backend adapter such as `dos_re/pm_backend.py` |
| CPU, DOS, or device behavior | owning runtime model plus focused test |
| Reusable comparison | `dos_re/verification.py`, `dos_re/frame_verify.py`, and replay verification |
| Closed-world packaging | `dos_re/export.py` and export tools |
| Deterministic diagnosis | a generic documented command under `tools/` |

Low-level `hooks.py` is interception machinery, not an implementation
registry. Real-mode and protected-mode event adapters normalize into the same
ReplayArtifact. There is one `player.py`.

## Required validation

```bash
python -m pytest -q
python tools/lint.py
python tools/check_undefined_names.py
python tools/check_doc_links.py
python examples/minimal_adapter/example.py
python examples/tiny_frame_game/walkthrough.py
```

Add targeted tests before the full suite. Preserve unrelated user changes and
generated artifacts.
