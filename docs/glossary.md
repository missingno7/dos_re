# Glossary

The project's vocabulary in one place. Terms link to the doc that owns them.

| Term | Meaning |
|---|---|
| **Oracle** | The original DOS executable running interpreted in the VM — the single source of truth for all behaviour. Never guessed around, never retired until a piece is CANONICAL. ([`lifecycle.md`](lifecycle.md)) |
| **VM / the microscope** | The `dos_re` 8086 + DOS + hardware interpreter. A controlled execution environment for observing and proving — not necessarily the final runtime. |
| **Hook** | A native handler installed at an original CS:IP. Scaffolding, never architecture; classified by role (checkpoint / env_wait / debug_probe / glue). ([`hooks_and_verification.md`](hooks_and_verification.md)) |
| **Island** | One coherent recovered unit with its own verification contract: pure logic + thin adapter + verifier. Tagged with `@oracle_link`. ([`lifecycle.md`](lifecycle.md)) |
| **Golden** | A recorded oracle fixture turned into a test: captured inputs/outputs/memory effects the recovered island must reproduce forever. |
| **Coastline** | The total surface where recovered code borders interpreted ASM. Progress = the coastline moving upward (fewer, larger contact points). |
| **Coastline shortening** | Calling a verified recovered callee directly instead of returning to ASM between two recovered islands. |
| **Continent** | The geography metaphor's end state: islands → archipelagos (subsystems) → continents (native systems) → the recovered mainland (VM-less port). |
| **Hybrid runtime** | The workbench: the VM running the original game with recovered islands hooked live over it. |
| **Native runtime** | The product: recovered source only — no VM, no EXE, no interpreted instruction in the hot path. |
| **Demo** | A deterministic **input recording** (never a video): VM-visible key events keyed to the emulated boundary clock, plus metadata. Replays identically under every driver. ([`demos_and_snapshots.md`](demos_and_snapshots.md)) |
| **Snapshot** | A save-state-like repro artifact: full memory + CPU + DOS/hardware state. Makes bugs local ("resume here, run 4 frames, compare"). |
| **Boundary clock** | The emulated counter demo events are keyed to. All drivers must agree on what increments it, or demo proofs are void. |
| **Input-wait registry** | The one shared table of boundary-less keyboard-poll loops every driver treats as boundaries. |
| **Hook oracle** | The differential per-hook verifier: clone, run original ASM to the continuation, run the hook, diff registers + flags + full memory. |
| **Frame oracle** | The lockstep frame verifier: reference (pure ASM) vs candidate (hooked/native) diffed at frame boundaries. |
| **Continuation / HookStop** | A hook's declared legitimate end (near RET, far RET, IRET, fixed IP, computed dispatch). |
| **Strict mode** | Auto-continuation verification: no metadata; the hook's final address becomes the only accepted target. |
| **State mirror / bridge** | Human-named typed views over the byte-exact original memory layout; offsets quarantined in one module, `memcmp` verification preserved. ([`state_mirrors.md`](state_mirrors.md)) |
| **Boot constants** | The post-bootstrap initialized state extracted into native data, so the native game cold-boots with no EXE and no snapshot. |
| **Heartbeat** | The game's fixed tick cadence, preserved explicitly in the native port — as opposed to the DOS waiting machinery (busy-waits, retrace polls), which is never ported. |
| **Env wait** | A hardware wait (PIT tick, CRT retrace) the interpreter must keep hooked so the oracle doesn't spin on a flag a real IRQ would clear. |
| **Frontier** | The residue of never-hooked addresses late in a port, each explicitly triaged (`dos_re/frontier.py`). |
| **Fail loud / HybridGap** | The no-silent-fallback rule made executable: unrecovered behaviour raises with precise context; it is never faked and never silently handed back to ASM. |
| **Transition signal** | A `HybridGap` subclass that is a control-flow signal, not an error: the per-frame step reached a multi-frame sequence (respawn, level end) the flow driver must drive. |
| **Status ladder** | GUESS → OBSERVED → RECOVERED → ASM_MATCHED → VERIFIED → CANONICAL — the only way names earn confidence (`dos_re/islands.py`, [`methodology.md`](methodology.md)). |
| **Faithful core / enhanced layer** | The verified game vs the presentation-only comfort layer that reads state and writes none. ([`enhancements.md`](enhancements.md)) |
| **Crystallization** | Letting higher-level meaning *emerge* from verified lower-level facts instead of naming by guess. ([`methodology.md`](methodology.md)) |
| **Staticization** | The discipline for runtime-patched code: observed live bytes → named variant → signature guard → explicit static Python. |
| **Adapter** | The per-game package holding everything that knows the game: addresses, formats, hooks, views, recovered logic. The framework core never learns a game. |
