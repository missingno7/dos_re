# AGENTS.md — dos_re framework repository

These instructions apply to the whole repository. `dos_re` is an internal,
agent-facing toolkit — the mechanical/oracle/lifting toolbox that AI agents
use to build verified DOS source ports. Two roles arrive here:

- **Using the toolbox to port a game** → your method and workspace live in
  `template_dos_port` (its `AGENTS.md`/`START_HERE.md`); the task → tool →
  command index for THIS repo's machinery is
  [`docs/agent_toolbox.md`](docs/agent_toolbox.md). You touch `dos_re/` only
  under the extension rules below — and when your game needs behaviour the VM
  lacks, extending it here (with tests) is the job; hacking around it in the
  port is not.
- **Extending the framework itself** → this file is your rulebook;
  [`docs/architecture.md`](docs/architecture.md) is the module map.

## What this repository is

The reusable, game-agnostic core of an oracle-driven DOS recovery method: a
real-mode VM, differential hook verification, frame comparison, deterministic
demos/snapshots, and the automatic lifter. Extracted from two real recovery
projects — Prehistorik 2 (primary; the method's completed VM-less proof) and
Overkill (the earlier pilot); `template_dos_port`'s `MIGRATION.md` records the
provenance of every part.

## Working principles

Correctness beats speed. Traceability beats cleverness. Small verified progress
beats large intuitive rewrites.

- **`dos_re/` must stay game-agnostic; numpy is first-class, pygame is
  viewer-only.** No game addresses, filenames, or formats in the core. numpy
  is a real dependency (`pyproject` `dependencies`) — use it anywhere it
  actually wins: bulk pixel/array work in proof engines, renderers, digests.
  The ONE measured exception (judgment, not lint): the interpreter's
  per-instruction path is *scalar* arithmetic, where numpy is slower than
  plain ints/bytearray on CPython and poisons the JIT under PyPy (which gives
  the core its biggest speedup, 13-17x — see docs/performance.md); keep that
  path numpy-free. pygame/pynuked_opl3 remain optional extras confined to the
  FRONTEND RING (`player.py`/`display.py`/`audio_sink.py`/`overlay_menu.py`),
  and `import dos_re` must never pull them in. `tools/lint.py` enforces the
  boundary; run it before finishing any change.
- **Do not make the emulator more general than a real target requires.** New
  CPU/DOS/hardware behaviour is added only when a concrete program exercises it,
  with the observed register/flag contract documented and a focused test added.
  Datasheet-driven completeness is scope creep here.
- **Behaviour changes need tests.** The suite (`python -m pytest tests -q` or
  `python tools/run_tests.py`) must pass; `tools/check_undefined_names.py` and
  `tools/lint.py` must stay clean. The runnable example
  (`python examples/minimal_adapter/example.py`) is part of the contract.
- **Fail loud, never fall back silently.** An unsupported opcode or service
  raises with precise context; it does not guess. Never replace a fail-fast
  path with a plausible default to keep something running.
- **No unverified equivalence claims.** Anything that claims to match the
  original — an interpreter optimization, a lifted hook, a "faster path" —
  carries an oracle proof (the equivalence gate in docs/performance.md, the
  lift proof ledger, a differential test). Performance is never evidence of
  correctness.
- **Determinism is a feature.** The deterministic default paths (no wall clock,
  no async IRQs unless opted in) must stay deterministic; anything time-driven
  is opt-in and clearly marked.
- **Don't break the boundary from the docs side either:** examples and docs may
  *mention* the source games as worked examples, but framework behaviour must
  never be specified in terms of one game.

## Extension recipes (missing behaviour → where it goes)

A game port that meets a framework gap extends the framework — never patches
around it locally. The next game hits the same gap.

| Gap | Do this |
|---|---|
| **Missing/incomplete CPU instruction** | Implement the *observed* behaviour in `cpu.py` (flags matched to the observed use). If the static decoder doesn't know the encoding, teach `lift/decode.py` too — the lifter cross-checks lengths against the interpreter, so they must agree. Focused test in `tests/` (test_core style: assemble the bytes, run, assert registers/flags/memory). |
| **Missing DOS/BIOS service or interrupt behaviour** | `dos.py` (INT 21h/10h/16h/…), with the observed register contract in a comment + a test. Never stub "return success". |
| **Missing hardware/port behaviour (VGA/PIT/PIC/SB/…)** | The owning model in `dos.py`/`memory.py`/`pic.py`/`sblaster.py`; update the honest status row in `docs/hardware_support.md`. Unmodeled port reads stay recorded (`dos.unmodeled_port_reads`) and loud under `strict_ports` — never silently modeled-as-zero. |
| **A verifier/proof capability a port needs** | Reusable machinery in the package (`verification.py`/`frame_verify.py`/`lift/`) or a `tools/` CLI — parameterized, not a one-off script buried in a port. If it must start life game-side, note it as a promotion candidate. |
| **A repetitive diagnosis you're doing by hand** | Make it a tool (`tools/`, with a docstring stating when to use it) and add it to `tools/README.md` + `docs/agent_toolbox.md`. If it's deterministic, it should be a tool. |
| **A mechanism the next game would reuse, currently in a port** | Promote it here with an origin note — but only once it is game-agnostic; if it knows addresses or formats, it stays in the adapter. |

## Where things live

```text
dos_re/         the framework package — docs/architecture.md is the module map
  cpu.py memory.py mz.py dos.py runtime.py pic.py sblaster.py interrupts.py
  keyboard.py bootstrap_lzexe.py asm.py            ← the machine
  hooks.py gaps.py verification.py frame_verify.py snapshot.py input_demo.py
  repro_artifacts.py hook_taxonomy.py runtime_code.py islands.py state_view.py
  checkpoints.py frontier.py dosbox_savestate.py   ← the proof engines
  lift/                                            ← the automatic lifter
  player.py display.py audio_sink.py               ← the frontend ring (numpy/pygame allowed)
pynuked_opl3/   submodule: third-party OPL backend; independent of dos_re
docs/           reference docs; docs/README.md is the index, agent_toolbox.md the task index
examples/       minimal_adapter (runnable), tiny_frame_game (full-stack demo)
tests/          framework tests; game-free by construction — new behaviour lands with one
tools/          run/see/read, lift/verify, guardrail CLIs — tools/README.md
```

## Standard commands

```bash
python tools/lint.py                          # boundary + syntax lint
python -m pytest tests -q                     # test suite (or tools/run_tests.py)
pypy -m pytest tests -q                       # same suite ~4x faster (docs/performance.md)
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
- Do not solve a framework gap inside a game port — extend the framework,
  with tests, and keep the port's adapter clean.
