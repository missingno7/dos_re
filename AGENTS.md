# AGENTS.md — dos_re framework repository

These instructions apply to the whole repository. They are written for AI
agents and humans working on the **framework itself**. (If you are using the
framework to port a game, start at [`START_HERE.md`](START_HERE.md) — your
game work happens in your adapter package, and you touch `dos_re/` only under
the rules below.)

## What this repository is

The reusable, game-agnostic core of an oracle-driven DOS recovery method:
a real-mode VM, differential hook verification, frame comparison, deterministic
demos/snapshots, and the documented methodology. It was extracted from two real
recovery projects — Prehistorik 2 (primary source; the method's completed
VM-less proof) and Overkill (the earlier pilot; endgame still in progress);
[`MIGRATION.md`](MIGRATION.md) records the provenance of every part.

## Working principles

Correctness beats speed. Traceability beats cleverness. Small verified progress
beats large intuitive rewrites.

- **`dos_re/` must stay game-agnostic and stdlib-only.** No game addresses,
  filenames, formats, or third-party imports in the core. `tools/lint.py`
  enforces this; run it before finishing any change.
- **Do not make the emulator more general than a real target requires.** New
  CPU/DOS/hardware behaviour is added only when a concrete program exercises it,
  with the observed register/flag contract documented and a focused test added.
  Datasheet-driven completeness is scope creep here.
- **Behaviour changes need tests.** The suite (`python -m pytest tests -q` or
  `python tools/run_tests.py`) must pass; `tools/check_undefined_names.py` and
  `tools/lint.py` must stay clean. The runnable example
  (`python examples/minimal_adapter/example.py`) is part of the contract.
- **Fail loud, never fall back silently.** This applies to the framework too:
  an unsupported opcode or service raises with precise context; it does not
  guess.
- **Determinism is a feature.** The deterministic default paths (no wall clock,
  no async IRQs unless opted in) must stay deterministic; anything time-driven
  is opt-in and clearly marked.
- **Don't break the boundary from the docs side either:** examples and docs may
  *mention* the source games as worked examples, but framework behaviour must
  never be specified in terms of one game.

## Where things live

```text
dos_re/       the framework package — see docs/architecture.md for the module map
nuked_opl3/   vendored third-party OPL backend; must stay independent of dos_re
docs/         the method; docs/README.md is the index
examples/     minimal_adapter (runnable), adapter_skeleton (template)
tests/        framework tests; game-free by construction
tools/        lint.py, run_tests.py, clean.py, lindis.py, profile_hotspots.py,
              audit_hook_oracle.py, audit_layers.py, check_undefined_names.py,
              gen_island_manifest.py, render_frame.py,
              display.py (optional-deps viewer backend)
```

## Standard commands

```bash
python tools/lint.py                          # boundary + syntax lint
python -m pytest tests -q                     # test suite (or tools/run_tests.py)
python tools/check_undefined_names.py         # latent-NameError guard
python examples/minimal_adapter/example.py    # end-to-end smoke of the whole loop
python tools/clean.py [--artifacts]           # remove generated junk
```

## Things not to do

- Do not let `dos_re/` learn anything about a specific game.
- Do not add third-party dependencies to the core (optional extras only).
- Do not replace fail-fast paths with guessed fallbacks to keep something
  running.
- Do not "clean up" original-behaviour quirks (flag shapes, wrap semantics)
  without oracle evidence from a real program — they are load-bearing.
- Do not treat performance as proof of correctness.
