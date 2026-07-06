# dos_re — an oracle-driven DOS game recovery framework

A reusable framework for turning an original 16-bit DOS game into a verified,
native source port — one proven routine at a time. It grew out of two completed
ports (Overkill, then Prehistorik 2) and packages the machinery and the method
they shared: a deterministic 8086 VM, differential hook verification, frame
comparison, deterministic input demos, snapshots, and the discipline that keeps
recovery honest.

## What it is

- **A real-mode VM built for reverse engineering** — an 8086 interpreter, DOS/
  BIOS services, and hardware models (VGA/EGA planar video, PIT, PIC, keyboard,
  PC speaker, AdLib/OPL2 register file, Sound Blaster + DMA), all stdlib-only
  Python, all deterministic by default.
- **Two proof engines** — a per-hook differential verifier that diffs your
  replacement against the interpreted original ASM (registers + flags + full
  memory) at every call, and a frame verifier that lockstep-diffs whole frames
  between an ASM oracle and your hooked/native candidate.
- **A determinism substrate** — full machine snapshots and input demos keyed to
  an emulated boundary clock, so every finding is replayable and every claim of
  equivalence is checkable.
- **A method** — the [AI Porting Charter](docs/ai_porting_charter.md): the
  lifting loop, the proof spine, the phased roadmap from "hook one routine" to
  "flip the engine and keep the VM as an offline oracle".

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
   At this stage the original game is still the source of truth.
2. Individual routines are hooked at their original addresses — first the hot,
   well-bounded leaf routines (asset decoders, decompression, blitters,
   palette), which are easiest to verify and make the VM faster and more
   observable; then the gameplay logic behind them.
3. AI/human recovery rewrites each routine as source (a pure rule behind a thin
   VM adapter).
4. The framework compares memory, registers, flags, ports, state, timing, and
   frames against the interpreted original.
5. Verified islands merge into subsystems; recovered behaviour is lifted into
   higher-level representations (objects, render state, game rules) — earned
   from evidence, never invented.
6. The game separates from the VM into a native source port. A state-mirror
   bridge keeps the native state byte-comparable with the original memory
   layout (readable code above, exact verification below), and the VM retires
   into the oracle seat: testing, replay, debugging, and proof.

The full arc, stage by stage: [`docs/lifecycle.md`](docs/lifecycle.md).

## Quick start

```bash
git clone <this repo>
cd dos_re
python examples/minimal_adapter/example.py   # the whole loop on a synthetic EXE, no assets needed
python -m pytest tests -q                    # or: python tools/run_tests.py
```

The example builds a tiny MZ executable, runs it as the oracle, installs a
wrong hook (and watches the verifier catch it), installs the correct hook
(verified on every call), and proves snapshot-replay determinism.

To start on a real game, read
[`docs/porting_new_game.md`](docs/porting_new_game.md) and copy
[`examples/adapter_skeleton/`](examples/adapter_skeleton/README.md).

## Adapting it to a new game (the short version)

1. Create a game adapter package (the skeleton shows the shape).
2. Configure EXE loading (packer bootstrap → snapshot past it) and data paths.
3. Wire input delivery and see video output.
4. Find the frame boundaries (timer wait, retrace wait, present) and stand up
   the frame verifier.
5. Build the input-wait registry, then record demos.
6. Identify stable verification points and start replacing small routines —
   one slice, one verification, at a time.
7. Promote hook code into native subsystems as evidence accumulates.

## What is game-specific vs framework

The framework (`dos_re/`) knows the 8086, DOS, the hardware, and the proof
engines — and is enforced game-agnostic (`tools/lint.py`). Everything that
knows *your* game — addresses, formats, boot constants, frame boundaries, state
layout, recovered logic — lives in your adapter. The boundary is documented in
[`docs/architecture.md`](docs/architecture.md).

## Repository layout

```text
dos_re/       the framework package (VM + proof engines) — stdlib-only
nuked_opl3/   vendored Nuked-OPL3 FM synthesis backend (optional, cffi)
docs/         the method + guides            → start at docs/README.md
examples/     runnable demo + adapter template
tests/        framework tests (no game assets needed)
tools/        lint / test runner / disassembler / profiler / audits
```

## Requirements

Python 3.11+. The core has **zero dependencies**. Optional: `pytest` (tests),
`cffi` (build the OPL3 backend), `numpy`+`pygame` (if your adapter builds an
interactive viewer).

## Provenance & honesty

This repo was extracted from `pre2_port` (primary, the newer framework) and
`overkill_port` (older sibling; contributed the cold-start demos, the asm
helper library, the hook taxonomy, several tools, and the vendored OPL3
backend). [`MIGRATION.md`](MIGRATION.md) records exactly what came from where,
what was deliberately left behind (game code, game-specific renderers/sound
drivers), and what still needs cleanup.
[`docs/hardware_support.md`](docs/hardware_support.md) is the honest status of
the hardware models — including what is *not* modeled (no generic CGA/Tandy
rasterizer, no MPU-401/GUS).

No game code, assets, or executables are included. Bring your own legally
owned game to port.

## License

MIT ([LICENSE](LICENSE)), except the vendored [`nuked_opl3/`](nuked_opl3/)
package (Nuked-OPL3 emulator core + binding), which is LGPL-2.1-or-later — see
[`nuked_opl3/LICENSE`](nuked_opl3/LICENSE).
