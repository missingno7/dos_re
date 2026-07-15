# dos_re — an oracle-driven DOS game recovery framework

A reusable **recovery machine** for turning an original DOS game (16-bit real
mode or 32-bit DOS/4GW protected mode) into a verified source port.  **It is a
recovery laboratory, not an emulator**: the original executable runs as the
*oracle*, deterministic tooling mechanically lifts, links, and assembles the
program into staged native runtimes, and every stage is verified against the
original execution.  The key idea is not "AI writes code" — it is
*deterministic tooling does the labor, AI unblocks it where it is stuck, and
the original game remains executable truth.*

**DOS_RE 2.0** ([`docs/dos_re_2.0.md`](docs/dos_re_2.0.md) — the canonical
architecture) is a staged pipeline of three mechanical detachments, each
oracle-verified:

```text
binary → automatic CPU-less lifting → structurally linked VM-less graph
→ automatically generated native shell → oracle-guided convergence → play_native
→ automatic memory-structure recovery → generated verification bridge
→ clean source port
```

```text
interpreted oracle → VMless lifted runtime → CPUless lifted runtime
→ DOS-layout-less native runtime → semantic clean source port
```

The largest supported graph is assembled mechanically and early; known-
unsupported constructs fail loudly; the end-to-end oracle finds silent
mistakes and auto-bisection localizes them; AI resolves only the concrete
gaps.  Per-function proofs are metadata (hybrid installs, diagnostics,
regression), never a gate on graph assembly.

> **Do not port the game.  Build the machine that ports the game.**
> Every blocker must improve the toolchain, not create another manual patch.
> (The Ten Principles: `docs/dos_re_2.0.md` §0.)  The staged pipeline is the
> engineering strategy; the ultimate goal is direct — *original binary +
> recovery facts → automated recovery tool → true native implementation* —
> with intermediate stages as verification projections of one shared
> recovery IR.

**This is infrastructure for AI agents, not a library for end users.** The
expected operator is an autonomous AI agent handed a porting repo (this
framework plugged in as a submodule) plus a game's files. A human's role in
that workflow is to supply the game and occasionally play it; nobody is
expected to drive these internals by hand. This README is the one
human-facing document here — everything else in the repo is the agent's
operating manual.

The framework grew out of two real recovery projects: **Prehistorik 2**, where
the method reached a complete, playable, VM-less native source port, and
**Overkill**, the earlier pilot that stress-tested the same ideas on a far
more chaotic codebase. It packages the machinery they shared — and it keeps
growing: every new game exercises behaviour the last one didn't, and agents
extend the framework (under [`AGENTS.md`](AGENTS.md)'s rules) as part of the
job.

## What it is

- **Two VMs built for reverse engineering** — an 8086 real-mode interpreter
  and a flat 386 protected-mode interpreter (`cpu386.py` + DOS/4GW host
  `dos4gw.py` + LE loader `le.py`) — each an interpreter,
  DOS/BIOS services, and hardware models (VGA/EGA planar video, PIT, PIC,
  keyboard, PC speaker, AdLib/OPL2, Sound Blaster + DMA), all pure Python
  (stdlib + numpy), all deterministic by default.
- **Two proof engines** — a per-hook differential verifier that diffs a
  replacement against the interpreted original ASM (registers + flags + full
  memory) at every call, and a frame verifier that lockstep-diffs whole frames
  between an ASM oracle and a hooked/native candidate.
- **A determinism substrate** — full machine snapshots and input demos keyed
  to an emulated boundary clock, so every finding is replayable and every
  claim of equivalence is checkable.
- **An automatic lifter** — a static decoder + emitter that turns a function
  entry into a literal, per-instruction Python hook and proves it against the
  oracle on every call, so recovery refactors a *verified* artifact instead of
  hand-translating ASM.

## What it is not

- **Not DOSBox** and not a general-purpose emulator: it models exactly what
  recovered games proved they need, favours determinism over completeness, and
  is not a way to *play* games.
- **Not magic AI prompts and not video-to-code.** Recovery is evidence-driven:
  the original executable runs in the VM and every recovered routine is diffed
  against what the original actually did. Nothing is inferred from screenshots
  or from "how DOS games usually work".
- **Not a remake kit.** The output of the method is a faithful source port,
  byte-exact against the original's observable behaviour.

## The core principle

**The original DOS binary is the oracle — the single source of truth.** A clean
native routine is a hypothesis until it is diffed against the original ASM.
Never guess; trace what the original did and match it. And no silent fallbacks:
an unrecovered path fails loud and becomes the next task, it is never quietly
faked or quietly handed back to the emulator.

## How recovery works

```text
original EXE ──▶ dos_re VM (the oracle) ──▶ traces / snapshots / demos
                     │                            │
        hook a routine at its CS:IP        deterministic replay
                     ▼                            ▼
     native recovered routine  ◀── verified ──  differential oracles
        (pure rule + thin adapter)     (registers, flags, memory, ports, frames)
                     │
                     ▼
   recovered systems gradually separate into a native source port;
   the VM stays behind as the offline proof harness
```

1. The original EXE runs in a controlled VM; demos replay deterministic input.
2. Routines are hooked at their original addresses — mechanically lifted or
   hand-recovered as source (a pure rule behind a thin VM adapter).
3. The framework diffs memory, registers, flags, ports, state, and frames
   against the interpreted original, on every call.
4. Verified islands merge into subsystems; higher-level meaning is earned from
   evidence, never invented.
5. The game separates from the VM into a native source port; the VM retires
   into the oracle seat: testing, replay, debugging, proof.

## Where the work happens

Game-specific work does **not** happen in this repo, and does not happen by a
person driving these APIs by hand. It happens in a **porting repo** —
[`template_dos_port`](https://github.com/missingno7/template_dos_port) is the
starting point — where an AI agent follows the documented method with this
framework wired in as the `dos_re/` submodule. The hard boundary, enforced by
lint: `dos_re/` never learns any game's addresses, filenames, or formats;
everything game-specific lives in the port's adapter package.

## Sanity check

```bash
git clone --recurse-submodules <this repo>
cd dos_re
python examples/minimal_adapter/example.py       # the hook/verify/snapshot loop, 5 minutes
python examples/tiny_frame_game/walkthrough.py   # the whole lifecycle on a synthetic game
python -m pytest tests -q                        # framework suite (no game assets needed)
```

## Who reads what

| Audience | Read |
|---|---|
| A human wondering what this is | this README — you're done |
| Anyone touching architecture, terminology, or the roadmap | [`docs/dos_re_2.0.md`](docs/dos_re_2.0.md) — the canonical staged-recovery pipeline, vocabulary, risk model, milestones |
| The agent porting a game | `template_dos_port`'s `AGENTS.md`/`START_HERE.md` (the method), then [`docs/agent_toolbox.md`](docs/agent_toolbox.md) (task → tool → command here) |
| The agent extending this framework | [`AGENTS.md`](AGENTS.md) (the rules), [`docs/architecture.md`](docs/architecture.md) (the module map) |
| Mechanism reference | [`docs/README.md`](docs/README.md) (hooks/verification, demos/snapshots, state mirrors, hardware status, lifting, performance, glossary) |

## Repository layout

```text
dos_re/       the framework package (VM + proof engines + lifter) — stdlib + numpy
docs/         reference docs + the agent toolbox   → docs/README.md
examples/     runnable demos (deletable; nothing imports them)
tests/        framework tests (no game assets needed)
tools/        run/see/read, lift/verify, and guardrail CLIs → tools/README.md
```

## Requirements

Python 3.11+. The core has **zero dependencies**. Optional: `pytest` (tests),
`cffi` (build the OPL3 backend), `numpy`+`pygame` (interactive viewers).
Headless workloads run unchanged — and much faster — under PyPy.

## Provenance & honesty

Extracted from `pre2_port` (primary) and `overkill_port` (earlier pilot);
`template_dos_port`'s `MIGRATION.md` records exactly what came from where.
[`docs/hardware_support.md`](docs/hardware_support.md) is the honest status of
the hardware models — including what is *not* modeled.

No game code, assets, or executables are included. Bring your own legally
owned game to port.

## License

MIT ([LICENSE](LICENSE)), except the vendored [`pynuked_opl3/`](pynuked_opl3/)
submodule and `graveyard/opl3_exact.py` (a pure-Python translation of
[Nuked-OPL3](https://github.com/nukeykt/Nuked-OPL3)) — both
LGPL-2.1-or-later; self-contained and separable (see LICENSE).

The framework's openness never extends to game IP: no game assets or
executables are ever included here or in adapter repos; ports require a
legally owned original copy; and any official/commercial packaging of a
recovered port requires the rights holder's agreement. Framework code and
game IP stay strictly separate.
