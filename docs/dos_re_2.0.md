# DOS_RE 2.0 — the automatic staged recovery pipeline

**Status: the canonical architecture (owner-ratified, 2026-07-17).  This
document supersedes any older doc language that gates native-graph assembly on
per-function proof.  The Lemmings pilot (`dos-re-2.0` branch) is the proving
ground.**

DOS_RE 2.0 is an explicit architecture reset, not an extension of the
conservative hook workflow.  1.x recovered games one proven routine at a time;
2.0 builds a **recovery machine**: deterministic tooling that takes an original
binary and mechanically assembles it into a native runtime, with AI intervening
only where the tooling is blocked, and the original executable remaining the
final judge of correctness throughout.

```
binary
→ automatic CPU-less lifting
→ structurally linked VM-less graph
→ automatically generated native shell
→ oracle-guided convergence
→ play_native
→ automatic memory-structure recovery
→ generated native↔historical-state verification bridge
→ clean source port
```

This framework is **from AI, for AI**: the operator is an autonomous agent,
and every rule below is written to keep that agent building the machine
instead of hand-porting the game.

---

## 0. The Ten Principles

> **Do not port the game.  Build the machine that ports the game.**
> Every blocker must improve the toolchain, not create another manual patch.

1. **Do not port the game.  Build the machine that ports the game.**
2. **The original executable is the oracle.**  Generated code is correct only
   when its observable behavior matches the original.
3. **Deterministic tooling performs the transformation.  AI only removes
   blockers.**  AI improves the decoder, IR, analyses, emitters, adapters,
   verification tools, or explicit recovery facts.  It does not manually
   rewrite hundreds of routines.
4. **Every completed stage has a hard execution wall.**  VMless cannot
   interpret instructions.  CPUless cannot access the CPU carrier.  True
   native cannot access the historical DOS memory layout.
5. **Unsupported behavior must fail loudly.**  No silent emulation, hidden
   interpreter fallback, guessed semantics, or patches that merely make one
   demo pass.
6. **Generated code is disposable.**  Delete every generated artifact and
   reproduce it from the original binary, configuration, recovery facts, and
   the DOS_RE toolchain.
7. **Game-specific knowledge must be explicit and minimal.**  Exceptional
   facts belong in structured, evidence-backed declarations — not in opaque
   edits to generated code.
8. **Assemble early, compare end to end, and converge.**  Build the largest
   supported execution graph, run it against the oracle, locate the first
   divergence, improve the system, regenerate, repeat.
9. **Recover effects and meaning, not the original machine forever.**  DOS
   services, video, input, audio, files, and timing become reusable platform
   adapters.  Registers, flags, offsets, and byte images are intermediate
   representations — not the final architecture.
10. **Every solved blocker must improve the future.**  A fix becomes a
    reusable DOS_RE capability or a documented recovery fact.  Each recovered
    game trains the recovery machine for the next one.

*The scripts perform the transformations.  AI removes the obstacles.  The
oracle decides what is correct.*

---

## 1. Canonical vocabulary: the five execution stages

The word "native" is banned as a bare term — it has meant three different
things.  Every doc, tool, status message, and ledger entry must use the stage
names below.

### Stage 0 — INTERPRETED ORACLE

The original DOS program runs inside the instruction-level interpreter.
x86 instructions are fetched, decoded, and executed dynamically; the full
emulated CPU state (registers, flags, stack, CS:IP, interrupts) and the
historical DOS memory are authoritative.  **This is the source of truth for
all differential verification.**  Call it: *oracle*, *interpreted oracle*,
*interpreter runtime*.  Never call it native.

```
original machine code → instruction interpreter → emulated CPU → historical DOS memory
```

### Stage 1 — VMLESS LIFTED

Machine instructions are translated into directly executable Python functions.
No fetch/decode/execute loop runs for lifted code; lifted functions call each
other directly.  **But the generated functions still operate on a CPU-shaped
carrier**: `cpu.s.ax`, flags, the emulated stack, segment:offset semantics,
and the historical DOS memory all still exist, and unsupported paths still
fall back (loudly) to the interpreter.

Call it: *VMless lifted runtime*, *VMless runtime*, *linked lifted runtime*.
**Do not call this "native"** — that is the ambiguity this vocabulary kills.
The stage exists to prove that the complete reachable machine-code graph
executes without instruction interpretation while preserving behavior.

### Stage 2 — CPULESS LIFTED

The CPU-shaped carrier is removed.  Generated functions no longer communicate
through emulated registers, flags, CS:IP, SP, push/pop, or machine CALL/RET —
they use function arguments, return values, locals, explicit state objects,
direct calls, and explicit control flow.  An early CPUless function may still
look mechanical (`def func_536c(mem, arg0, arg1): ...`) and may still address
the historical DOS memory image by raw offset.

Call it: *CPUless lifted runtime*, *CPUless native graph*, *CPUless generated
implementation*.  This is the first stage that may reasonably be called native
code execution — **always qualified as CPUless** while the DOS memory model
remains.

The VMless→CPUless transformation is driven by shared IR and deterministic
analysis — register liveness, flag liveness, stack-effect analysis, call
input/output inference, memory-access analysis, direct-call reconstruction,
control-flow reconstruction — **never by parsing generated or manually
refactored Python**.  The passes operate on the recovery IR; each emitted
form is a projection of that IR:

```
original binary → shared recovery IR → analyses/transformations → selected emitter
                                        (VMless literal | CPUless | DOS-layout-less
                                         | oracle bridge | diagnostic/verification)
```

Literal generated files are regeneratable compiler artifacts and must not be
hand-refactored.

### Stage 3 — DOS-LAYOUT-LESS NATIVE

The historical DOS memory model is removed.  Game state is no longer a flat
byte image of DGROUP offsets, segment:offset pointers, fixed binary structures
and overlapping scratch buffers — it is native structures: dataclasses, typed
arrays, enums, references, named fields, explicit ownership.

```
before:  x = mem.read_u16(LEMMING_BASE + index * 32 + 4)
after:   x = game.lemmings[index].x
```

Call it: *DOS-layout-less native* (the preferred formal term),
*memory-model-less native*, *dissolved native model*, *clean native object
model*.  ("DOS-memoryless" is acceptable informally; memory does not
disappear — only the dependency on the original DOS memory **layout** does.)

### Stage 4 — SEMANTIC CLEAN PORT

After the three mechanical detachments, semantic cleanup: naming functions and
fields, recovering game concepts, domain models, structured control flow,
subsystem boundaries, clean renderer/audio/input/timing/filesystem APIs,
enhancements built above the recovered logic.  The final human-readable source
port.

### The three detachments

```
interpreted oracle
→ VMless lifted runtime        (1. VM detachment — remove instruction interpretation)
→ CPUless lifted runtime       (2. CPU detachment — remove the CPU-shaped execution model)
→ DOS-layout-less native       (3. memory-model detachment — remove the DOS memory layout)
→ semantic clean source port
```

> First remove the interpreter.  Then remove the CPU model.  Then remove the
> DOS memory model.  What remains is the game itself.

> VMless means the instructions are no longer interpreted.  CPUless means the
> program no longer thinks in registers and flags.  DOS-layout-less means the
> game no longer thinks in offsets and byte images.

Hard rules:

- "VMless" is NOT synonymous with "CPUless".
- "CPUless" is NOT synonymous with a clean source port.
- A VMless runtime may still be strongly CPU-shaped.
- A CPUless runtime may still use the complete historical DOS memory image.
- A DOS-layout-less runtime may still contain ugly generated names and flow.
- Semantic cleanliness is a separate, later stage.

Message wording: prefer *full VMless lifted graph*, *VMless lifted candidate*,
*CPUless candidate*, *DOS-layout-less candidate*, *semantic native
implementation* — never *full native runtime* / *native assembly* / *native
hooks* for a merely-VMless artifact.

### 1a. Hard execution walls, and the runner naming contract

The stage names are not labels on effort — each completed stage is defined by
an **enforced, mechanically checkable execution wall**:

- **VMless wall**: the emitted output contains no instruction-interpretation
  fallback on the declared corpus (zero ``interp_one`` call sites; the
  interpreter survives only OUTSIDE the declared corpus as the enumerated,
  fail-loud frontier — never silently inside it).
- **CPUless wall**: the emitted output cannot access the CPU carrier — no
  ``cpu.s`` registers, no flags, no emulated stack, no machine CALL/RET.
- **Native wall**: the emitted output cannot access the historical DOS memory
  model in normal gameplay.

A milestone is not complete because working code exists; it is complete only
when the stage regenerates automatically AND its wall is enforced by tooling
(a grep-class static check on the emitted artifacts at minimum, a runtime
audit where static checking cannot see it).

The walls fix the runner names in every port:

```
play_vmless.py    output of the automated VMless pipeline
                  (lift → link → install the graph; NOT a hand-assembled hook set)
play_cpuless.py   output of the automated CPUless transformation
                  (NOT a manually refactored copy of play_vmless.py)
play_native.py    output of the automated DOS-layout dissolution pipeline,
                  plus only optional oracle-verifiable semantic cleanup
```

A runner may not carry a name whose wall its artifacts do not satisfy.

### 1b. The staged pipeline is a STRATEGY, not the required final architecture

The sequence oracle → VMless → CPUless → DOS-layout-less exists to divide one
extremely difficult transformation into small, measurable, independently
verifiable detachments.  It is the current engineering plan — **not a mandate
that the toolchain forever materialize every intermediate stage for every
game**.

The ultimate goal is direct:

```
original binary + recovery facts → automated recovery tool → true native implementation
```

where **true native** means: no instruction interpreter, no emulated CPU
carrier, no dependency on the historical DOS memory layout, normal
host-language control flow, native data structures, native platform
interfaces — with oracle verification retained through the optional generated
bridge.

Consequences for how the system is built:

- **Do not build the system around transforming arbitrary generated Python
  from one stage into the next.**  Build around a shared recovery IR,
  reusable analysis passes, and *selected emitters*; every stage artifact is
  a projection of the same IR, and a sufficiently capable pipeline may go
  ``assembly → IR → dataflow/structure recovery → true native`` without ever
  writing a persistent VMless file.
- Intermediate emissions (VMless, CPUless) remain available as **diagnostic
  and verification projections** — checkpoints for bisection and proof — and
  the toolchain may internally combine detachments when it can do so safely.
- The hard walls (§1a) still define the meaning of whatever IS emitted; they
  constrain artifacts, not the route taken to produce them.

The final success criterion is NOT "we completed VMless, then CPUless, then
native, each by hand."  It is:

> **The automated toolchain can regenerate a true native implementation from
> canonical inputs and verify it against the original oracle.**

If the direct transformation becomes reliable, prefer it; keep the
intermediate forms as projections of the same recovery IR.

---

## 2. The risk model: oracle-guided convergence

Two INDEPENDENT safety mechanisms, not one conservative gate:

- **Fail-loud** protects against KNOWN-unsupported constructs: the lifter
  refuses what it cannot represent, unknown call targets raise, uncovered
  paths raise `HybridGap` with a snapshot and repro command.  Nothing is
  silently faked or silently handed back to hidden emulation.
- **End-to-end oracle comparison** protects against UNKNOWN silent mistakes
  (translator, linker, scheduler, platform).  Full guest state is diffed
  against the interpreted oracle at every tick boundary over demos/drives.
  **This is the authoritative gate.**

Consequences:

- **Assemble the largest supported graph as early as possible.**  Mechanical
  integration is optimistic within the declared supported subset.
- **Per-function ORACLE_PASSING does not gate linking or inclusion in
  `play_native`.**  Per-function proofs remain useful metadata — hybrid
  auto-install, diagnostics, regression tests, later refactoring — but they
  are not a precondition for graph assembly.
- **Divergences are localized automatically** (`tools/hook_bisect.py`
  binary-searches the installed set to the smallest responsible function),
  then AI repairs only that concrete gap and the pipeline reruns.
- Verification must remain available through ALL stages (see §5, the bridge).

The operating loop:

```
run recovery pipeline
→ observe first unsupported or divergent case
→ minimize the case
→ identify the missing capability or fact
→ improve tooling or add the fact
→ regenerate all affected artifacts
→ compare against oracle
→ repeat
```

**The recorded demo is the test.**  When a recorded input demo exists for a
path, it is the authoritative input corpus: replay exactly the same semantic
events on the oracle and on the candidate at the same semantic boundaries
(the framework's lockstep playback), and compare state there.  Never
approximate a demo with synthetic frame-number drives, periodic key spam, or
mouse-hold heuristics — a guessed test verifies a guessed path.  Gate on
compatibility first (clock semantics, hook-topology fingerprint); a stale
demo is migrated or re-recorded ONCE, not approximated.  Input models are
investigated only when the same demo succeeds on the oracle and diverges on
the candidate.

---

## 3. The automation principle (non-negotiable)

**All transformation stages are repeatable, deterministic tooling.  The AI
does not manually port, rewrite, or refactor the game function by function.**

Not the workflow: *agent reads one lifted function → rewrites it CPUless →
manually objectifies memory → repeat hundreds of times.*  That uses AI as an
expensive manual porter and produces no reusable recovery system.

The workflow:

```
automated pipeline runs
→ pipeline reaches a concrete unsupported construct
→ pipeline fails loudly with a minimal reproducible blocker
→ AI investigates that blocker
→ AI improves the generic tooling or records a small explicit recovery fact
→ the entire pipeline is regenerated
→ oracle verification checks the result
```

**AI does not perform the bulk transformation.  AI only unblocks the
automated transformation system.**

Per stage, the transformation is performed by:

1. **VMless**: decoder, CFG recovery, literal lifter, linker, generated call
   bindings, interrupt/platform-effect recognition.  (Not: AI hand-translating
   hundreds of ASM functions.)
2. **CPUless**: shared machine IR; register-liveness, flag-liveness,
   stack-effect, dataflow analyses; call-signature inference; direct-call and
   control-flow reconstruction; the CPUless emitter; generated VM adapters.
   (Not: AI rewriting every CPU-shaped function into args/locals/returns.)
3. **DOS-layout dissolution**: memory-access collection, base-and-stride
   detection, field-offset clustering, access-width and signedness inference,
   pointer/reference analysis, array/structure recovery, generated native
   object models, generated native↔historical-state bridges.  (Not: AI
   manually replacing every raw access with an object field.)
4. **Semantic cleanup**: more AI judgment is allowed — naming, subsystem
   boundaries, domain models, recognizing that an anonymous structure is a
   lemming/sprite/level/channel, resolving ambiguous ownership — but even
   semantic replacements are oracle-checked and never destroy the canonical
   generated implementation.

The AI's roles: blocker investigator, toolchain developer, recovery-system
architect, hypothesis generator, semantic annotator, exception handler.
NOT: primary instruction translator, function-by-function porter, replacement
for compiler passes, unchecked rewriter, or the authority on correctness.
**The oracle is the authority on correctness.**

### Blocker classification

When the pipeline fails, classify the blocker:

**A. Generic capability gap** — the construct may appear in other games
(unsupported instruction semantics, stack switching, cross-function jumps,
indirect jump tables, IVT ownership, self-modifying code, overlays, code/data
aliasing, non-standard calling conventions, unusual flag dependencies,
platform effects with no adapter yet).  Response: implement/improve a generic
DOS_RE capability → focused tests → regenerate → re-verify.

**B. Game-specific recovery fact** — not mechanically derivable, but evidence
pins a small explicit fact about this binary (this range is a jump table;
this address is an input-wait boundary; this function owns this interrupt
vector; this region is intentionally self-modifying; these are alternate
entries of one function; this overlay has this identity; this range aliases
this structure; this routine is the scheduler seam).  Response: record the
smallest evidence-backed fact → feed it into the generic pipeline →
regenerate → re-verify.

The response is **never** to hand-patch generated output until a demo happens
to pass.

### Recovery facts are explicit

Game-specific knowledge lives in structured, reviewable, versioned
declarations — e.g. `recovery_facts.yaml`, `code_map.json`,
`platform_effects.json`, `function_contracts.json` — each with enough
evidence/provenance to explain why the fact exists.  Generated output stays
disposable; every generated file carries:

```
AUTOGENERATED — DO NOT HAND EDIT.
Regenerate from the original binary, machine IR, recovery facts,
and the current DOS_RE toolchain.
```

When AI discovers a fix it modifies: the generic decoder, an analysis pass,
the IR, an emitter, the linker, a platform adapter, a verifier, or a
structured recovery-fact input — normally never the generated output.

### Coverage-guided toolchain evolution

Each game is a training corpus for the recovery system.  New reachable
behavior → new unsupported construct → AI investigates → a generic capability
or a recovery fact lands → the pipeline advances → **future games inherit the
capability**.  For the pilot: *we are not merely porting Lemmings; Lemmings is
training and validating the recovery machine.*

### The core rule

Whenever the agent is about to manually rewrite a generated function, it must
stop and ask: **"What capability is missing from the automated pipeline?"** —
then implement that capability or encode the smallest explicit recovery fact.
Only genuinely semantic, post-recovery refactoring may become an AI-authored
implementation, and even then: the generated CPUless implementation remains
the reference, the replacement has an explicit contract, is oracle-verified,
and can fall back to the generated implementation during development.

### 3a. Automate the 99%, isolate the 1%

Each stage mechanically transforms the overwhelming majority of the reachable
program; what remains is a SMALL exceptional frontier that fails loudly, and
AI investigates ONLY that frontier.  The target metric is never "hundreds of
functions manually repaired"; it is:

```
nearly all functions transformed automatically
+ a small number of generic capability improvements
+ a small number of explicit recovery facts
+ zero hand-edited generated functions
```

Per stage, the expected exceptional frontier:

- **VMless**: interrupt delivery during lifted execution, environment-wait
  loops, scheduler/boundary seams, indirect control flow, overlays,
  self-modifying code, unresolved platform effects.
- **CPUless** (the analyses handle the rest automatically): unusual stack
  switching, cross-call flag dependencies, multiple logical entries,
  non-standard calling conventions.
- **DOS-layout dissolution** (base/stride, field clustering, structure and
  reference recovery handle the rest): unions, overlapping scratch memory,
  aliasing, ownership, intentionally reused buffers.

**The frontier is a queue of unresolved automation gaps, not a resting
state.**  Entries kept interpreted / kept un-promoted are TEMPORARY: a
completed stage may not leave them behind.  An env-wait excluded from the
VMless graph today must eventually become an explicit scheduler-yield effect,
a resumable lifted control-flow construct, or an IRQ-at-loop-boundary runtime
capability — so the function itself returns to the VMless graph.  The
dissolution loop:

```
temporary exclusion/hook  → understand the missing semantic effect
→ encode the effect / recovery fact / capability → regenerate → remove it
```

A manually authored hook is permitted only as an investigative probe or a
behavioral specification — never the permanent implementation of something a
generic capability, fact, adapter, or generated transformation can express.
The long-term target is not "160 lifted functions + 3 permanently handwritten
hooks"; it is "163 automatically promoted functions + a few reusable
capabilities or facts + generated adapters + zero duplicated game logic".

### 3b. Automatic promotion, not manual reimplementation

The staged promotion — VMless → CPUless → DOS-layout-less → true native — is
performed BY THE TOOLING for ~99% of functions.  A normal function moves

```
decoded machine function → VMless implementation → CPUless implementation
→ native-state implementation
```

with NO manual intervention; the toolchain generates whatever lower-stage
compatibility adapters the mixed graph needs along the way.  The agent must
not maintain separate hand-made VMless/CPUless/native versions of ordinary
functions — that would turn the 1% into a manually maintained parallel port.
Each stage consumes the shared recovery IR and the recovery facts, never
hand-modified output of the previous stage; every stage's output remains
regeneratable from the original binary + IR + configuration + facts + the
current toolchain.

The scaling shape: first game — many new capabilities discovered; later games
— most already exist; mature toolchain — nearly the entire program lifts
directly to true native.  **Automation owns the program.  AI owns only the
exceptional frontier.  Every solved exception becomes automation.**

---

## 4. Platform adapters: no monolithic DOS machine in the native output

The native output must not depend on a monolithic DOS machine implementation.
The lifting pipeline recognizes platform/hardware **effects** and binds them
to reusable native **adapters**:

```
machine-level operation → recognized effect → native adapter call
```

| Machine effect | Adapter |
|---|---|
| DOS file access (INT 21h open/read/seek…) | filesystem adapter |
| keyboard / mouse input (INT 16h/33h, port 60h) | input adapter |
| VGA page flip / palette update / mode set | video adapter |
| OPL / Sound Blaster register writes | audio adapter |
| timer, retrace, IRQ wait | scheduler adapter |

Recovered game logic calls a small platform interface:

```
platform.video.present(...)
platform.input.read_state(...)
platform.audio.write_register(...)
platform.files.read(...)
platform.clock.tick(...)
```

Multiple implementations exist behind the same interface: an
**oracle-compatible adapter** (bit-faithful, drives the verification bridge),
a **faithful native adapter**, an **enhanced adapter**, and platform backends
(desktop, Android, web).

Rules:

- Adapters are **generic dos_re capabilities**, never per-game code.  Each new
  game that exposes an unsupported machine effect gets the smallest generic
  adapter or effect-recognizer added *to dos_re*, with tests; all future games
  inherit it.
- **Unknown effects fail loudly** — never a silent fallback to hidden
  emulation.
- Existing reusable material: the `play_native` shells of **pre2_port,
  skyroads_port, overkill_port** already contain adapter-shaped video/input/
  timing code to mine; **`opl3_fast.py` is usable as-is as the audio-adapter
  synth in a native game**.

The progression:

```
binary → automatic lifting → effect recognition → native adapter binding
→ linked CPU-less game graph → generated play_native
```

Long-term: dos_re accumulates a growing adapter library, so each later game
needs less platform work and assembles into a native runtime more
automatically.

---

## 5. Verification through every stage: the bridge

Verification never becomes unavailable.  Once the CPU and the original memory
model are gone, the **generated verification bridge** keeps the oracle
reachable: it reconstructs historical DOS machine state from the native object
model so the original executable can still judge the result.

```
release runtime:        native object model → native gameplay → native platform adapters
verification runtime:   native object model → generated bridge/serializer
                        → reconstructed historical DOS state → oracle comparison
```

- The bridge depends on the native implementation — **never the other way
  around**.
- The shipped game must not require the interpreter, the CPU carrier, the
  historical DOS memory image, or the bridge.  The bridge is an optional
  development/validation component.

The memory-structure recovery that feeds this (arrays, structures, fields,
pointers, state relationships → generated native object model + bridge) is
the next major mechanical stage after the VMless graph converges.

---

## 6. Milestones

- **M1 — Oracle execution.**  The original game runs deterministically in the
  interpreter.
- **M2 — Full VMless graph.**  The required reachable graph executes through
  lifted functions with no instruction-interpreter fallback on the declared
  corpus.
- **M3 — CPUless graph.**  The CPU carrier is removed from the required
  reachable graph.
- **M4 — DOS-layout dissolution.**  Historical memory structures are replaced
  with native objects, oracle verification retained through the generated
  bridge.
- **M5 — Semantic clean port.**  The recovered implementation is
  understandable, maintainable, machine-architecture-independent.
- **M6 — Enhancements.**  Widescreen, smooth rendering, improved audio, modern
  input, new platforms — above the recovered game logic.

The goal is NOT to jump from ASM to clean semantic Python.  It is a staged
pipeline where each step removes one historical dependency while remaining
oracle-testable:

```
original binary
→ behaviorally verified VMless translation
→ behaviorally verified CPUless translation
→ behaviorally verified native memory model
→ semantic refactor
```

### Success criteria (what "milestone complete" means)

A milestone is complete when — and only when:

- the transformation is performed by a repeatable command;
- generated artifacts can be deleted and regenerated;
- the inputs are the original binary, configuration, and explicit recovery
  facts;
- unsupported cases fail loudly;
- the result is verified against the oracle;
- the stage's hard execution wall (§1a) is enforced by tooling;
- the same capability can be reused on later games.

And the program-level success criterion above all of them (§1b): the
automated toolchain can regenerate a **true native** implementation from
canonical inputs and verify it against the original oracle — the staged path
is the plan, direct emission is the goal.

CPUless completion does NOT mean "the agent refactored all reached hooks by
hand"; it means the CPUless analysis and emitter regenerate the required
reachable CPUless graph from canonical inputs.  DOS-layout dissolution does
NOT mean "the agent wrote a Lemming class and patched callers"; it means the
structure-recovery pipeline generates the native representation and its
bridge, with AI resolving only semantic ambiguity and exceptional cases.

---

## 7. Canonical summary

**The scripts perform the transformations.  The AI removes the obstacles.
The oracle decides whether the result is correct.**

```
Deterministic tooling does the labor.
AI provides judgment where automation is blocked.
Oracle verification checks every result.
```
