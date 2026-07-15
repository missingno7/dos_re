# Glossary

The project's vocabulary in one place. Terms link to the doc that owns them.

**The five execution stages ([`dos_re_2.0.md`](dos_re_2.0.md) §1 is the
authority; "native" is banned as a bare term):**

| Stage term | Meaning |
|---|---|
| **Interpreted oracle** | Stage 0: the original program running in the instruction-level interpreter — full emulated CPU state + historical DOS memory are authoritative. The source of truth for all differential verification. |
| **VMless lifted runtime** | Stage 1: lifted Python functions execute directly (no fetch/decode/execute loop for lifted code) but still on the CPU-shaped carrier (`cpu.s`, flags, emulated stack, seg:off, DOS memory). NOT "native". |
| **CPUless lifted runtime** | Stage 2: the CPU carrier is removed — args/returns/locals/explicit state instead of registers/flags/push-pop. May still address the historical DOS memory image by raw offset. The first stage that may be called native code execution, always qualified CPUless. |
| **DOS-layout-less native** | Stage 3: the historical DOS memory *layout* is dissolved into native structures (objects, typed arrays, named fields). Memory doesn't disappear; the dependency on the original layout does. |
| **Semantic clean port** | Stage 4: named concepts, domain models, structured flow, clean platform APIs — the human-readable source port. |
| **The three detachments** | VM detachment (remove interpretation) → CPU detachment (remove the CPU-shaped model) → memory-model detachment (remove the DOS layout). "What remains is the game itself." |
| **Oracle-guided convergence** | The 2.0 risk model: assemble the largest supported graph mechanically and early; fail loud on known-unsupported; the end-to-end oracle finds silent mistakes; auto-bisect localizes; AI repairs only the concrete gap. Per-function proofs are metadata, never an assembly gate. |
| **Platform adapter** | A reusable generic dos_re capability binding a recognized machine effect (file access, input, page flip, OPL write, timer wait) to a native interface (`platform.video.present(...)`). Oracle-compatible / faithful / enhanced / per-OS implementations behind one interface. |
| **Recovery fact** | The smallest explicit, evidence-backed, versioned declaration of game-specific knowledge (jump table here, input-wait boundary there) fed into the generic pipeline — the alternative to hand-patching generated output. |
| **Verification bridge** | Generated serializer reconstructing historical DOS machine state from the native object model so the oracle stays reachable after the CPU and memory model are gone. Depends on the native implementation, never vice versa; not shipped. |
| **Hard execution wall** | The enforced, mechanically checkable property that defines a completed stage: VMless output cannot interpret instructions (zero `interp_one` sites on the declared corpus), CPUless output cannot access the CPU carrier, native output cannot access the DOS memory model. Fixes runner names: `play_vmless.py` / `play_cpuless.py` / `play_native.py` — a runner may not carry a name whose wall its artifacts do not satisfy. |
| **Recovery IR** | The shared intermediate representation all analyses/transformations operate on; every stage artifact (VMless, CPUless, native, bridge, diagnostics) is an EMITTER PROJECTION of it. The system is never built around parsing generated Python from one stage into the next. |
| **True native** | The ultimate target: no interpreter, no CPU carrier, no DOS-layout dependency, host-language control flow + data structures + platform adapters, oracle-verifiable via the optional bridge. The staged path (VMless → CPUless → DOS-layout-less) is the engineering strategy; direct binary→true-native emission is the goal. |

| Term | Meaning |
|---|---|
| **Oracle** | The original DOS executable running interpreted in the VM — the single source of truth for all behaviour. Never guessed around, never retired until a piece is CANONICAL. (template_dos_port's `docs/lifecycle.md`) |
| **VM / the microscope** | The `dos_re` 8086 + DOS + hardware interpreter. A controlled execution environment for observing and proving — not necessarily the final runtime. |
| **Hook** | A native handler installed at an original CS:IP. Scaffolding, never architecture; classified by role (checkpoint / env_wait / debug_probe / glue). ([`hooks_and_verification.md`](hooks_and_verification.md)) |
| **Island** | One coherent recovered unit with its own verification contract: pure logic + thin adapter + verifier. Tagged with `@oracle_link`. (template_dos_port's `docs/lifecycle.md`) |
| **Golden** | A recorded oracle fixture turned into a test: captured inputs/outputs/memory effects the recovered island must reproduce forever. |
| **Coastline** | The total surface where recovered code borders interpreted ASM. Progress = the coastline moving upward (fewer, larger contact points). |
| **Coastline shortening** | Calling a verified recovered callee directly instead of returning to ASM between two recovered islands. |
| **Archipelago / Continent** | The geography metaphor's middle and end states: islands → archipelagos (islands connected into a *subsystem*) → continents (complete native systems) → the recovered mainland (VM-less port). "Subsystem" and "archipelago" name the same thing at different altitudes. |
| **Glue** | A hook-taxonomy role: accidental ASM-boundary plumbing (tails, helpers, per-row scan steps) that exists only because a hook landed there — the collapse target when islands merge. Not an architectural layer. |
| **Parity gate** | The enhanced layer's standing proof: at its neutral settings the enhanced game must be pixel- and state-identical to the faithful game, so "enhanced" can never silently mean "diverged". (template_dos_port's `docs/enhancements.md`) |
| **Hybrid runtime** | The workbench: the VM running the original game with recovered islands hooked live over it. |
| **Native runtime** | DEPRECATED as a bare term — it conflated stages 1–4. Use the stage vocabulary above: a runtime is *VMless lifted*, *CPUless lifted*, *DOS-layout-less native*, or a *semantic clean port*. The shipped product is stage 3+ (no interpreter, no CPU carrier, no DOS layout, no bridge). |
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
| **Status ladder** | GUESS → OBSERVED → RECOVERED → ASM_MATCHED → VERIFIED → CANONICAL — the only way names earn confidence (`dos_re/islands.py`, template_dos_port's `docs/methodology.md`). |
| **Faithful core / enhanced layer** | The verified game vs the presentation-only comfort layer that reads state and writes none. The enhanced layer is built LAST — lifecycle Stage 6, after the faithful game is complete. (template_dos_port's `docs/enhancements.md`) |
| **Cyborgization** | The deprecated early-P2 experiment of growing faithful/enhanced viewer backends alongside recovery, before the native game was complete. Retrospective verdict: not recommended (pitfall #24). |
| **Crystallization** | Letting higher-level meaning *emerge* from verified lower-level facts instead of naming by guess. (template_dos_port's `docs/methodology.md`) |
| **Staticization** | The discipline for runtime-patched code: observed live bytes → named variant → signature guard → explicit static Python. |
| **Adapter** | The per-game package holding everything that knows the game: addresses, formats, hooks, views, recovered logic. The framework core never learns a game. |
