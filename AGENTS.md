# AGENTS.md — dos_re framework repository

These instructions apply to the whole repository. Start with
[`README.md`](README.md), then [`docs/getting_started.md`](docs/getting_started.md)
and [`docs/architecture.md`](docs/architecture.md).

## Architectural rule

Extend the existing authority; never create a parallel one.

```text
Recovery IR + ReplayArtifact
          -> Execution Atlas -> ProgramCoverage
          -> ImplementationCatalog + RuntimeServiceCatalog
          -> ExecutionConfiguration
          -> ExecutionPlan + DetachmentReport
          -> unified player / verification / closed-world export
```

- Recovery IR owns retained static facts.
- Stable types in `dos_re.identity` cross artifact boundaries.
- ReplayArtifact is the only persistent deterministic replay format.
- Atlas owns evidence aggregation and navigation, not runtime dispatch.
- ImplementationCatalog is the only implementation inventory.
- ExecutionConfiguration is the only composition and policy input.
- The planner is the only selection and dependency-closure authority.
- Player and backend adapters consume a validated plan without fallback.
- Verification compares oracle and candidate; it never selects code.
- Export consumes one exact package-ready release plan.

Interpreted, VMless, CPUless, ABI-recovered, DOS-memory-backed, memoryless, and
native are per-implementation properties, never player or product modes.
Use “hook” only for actual low-level interception and “replay” for the current
record/replay architecture.

## Framework rules

- Keep `dos_re/` game-agnostic. Addresses, filenames, formats, assets, and
  recovery declarations specific to one game belong in its port repository.
- Generated code is reproducible output. Fix a generic pipeline capability or
  add the smallest evidence-backed project fact, then regenerate; do not
  hand-patch generated corpora.
- Fail loud. Unsupported CPU, DOS, device, coverage, bootstrap, or execution
  behavior must not acquire a plausible fallback.
- Correctness claims require evidence. Faithful changes need focused tests and
  oracle/replay verification proportional to their scope.
- Deterministic execution remains the default proof path. Wall-clock and
  asynchronous behavior are explicit opt-ins.
- Add only machine behavior exercised by a concrete target, with its observed
  contract and a focused framework test.
- Keep pygame in the frontend/backend-adapter ring. `import dos_re` must not
  import optional UI dependencies. NumPy is a core dependency but should stay
  out of scalar interpreter hot paths where it harms CPython/PyPy performance.
- Update active docs and `tools/README.md` with every public mechanism or CLI
  change. Historical material belongs under `docs/history/`.

## Where changes go

| Need | Owner |
|---|---|
| Stable cross-artifact name | `identity.py` |
| Static recovered fact | Recovery IR (`lift/ir.py`, `recovery_ir.py`) |
| Recorded event/state/visit/transfer | `replay.py` and replay adapters |
| Evidence aggregation/query/coverage | `atlas.py` |
| Implementation, service, profile, dependency closure | `execution.py` |
| Authored override contract | `overrides.py` |
| Initial state declaration/materialization | `bootstrap.py`, `bootstrap_runtime.py` |
| Machine-specific construction or I/O | backend adapter (`pm_backend.py` or port adapter) |
| CPU/DOS/device behavior | owning runtime model plus focused test |
| Reusable comparison | verification modules |
| Product packaging | `export.py` and export tools |
| Deterministic diagnosis | a generic `tools/` command, documented in both tool indexes |

Low-level `hooks.py` remains valid interception machinery; it is not a second
implementation registry. Real-mode and protected-mode event adapters normalize
into the same ReplayArtifact. There is one `player.py`.

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
generated artifacts. Do not add compatibility shims solely for disposable
downstream scripts or recordings.
