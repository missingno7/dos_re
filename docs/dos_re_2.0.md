# DOS_RE 2.0 — the automatic staged recovery pipeline

**Status: the canonical architecture (owner-ratified, 2026-07-17).  This
document supersedes any older doc language that gates native-graph assembly on
per-function proof.  The Lemmings pilot (`lemmings_port`) is the reference
implementation: M2 (strict VMless + EXE-independent) and M3 (CPUless via the
automated de-carrier process) are both ACCEPTED and merged.  The next
milestone is M4 (DOS-layout dissolution).**

> **DOS_RE 3.0 executable-model supersession.** The recovery transformations
> and hard walls in this document remain valid implementation properties and
> proof gates. They no longer define separate product types or mandatory
> runner names. Top-level launch has only hybrid and standalone dependency
> modes, and one standalone plan may mix implementations at different recovery
> levels. See [`execution_planner.md`](execution_planner.md) and
> [`override_architecture.md`](override_architecture.md).

> **M3 CPUless — ACCEPTED (2026-07-16).**  The whole reachable graph from the
> game root is CPUless: recovered functions compute over `(mem, plat, *regs)`
> with no CPU carrier, no interpreter, no lifted graph.  `detached profile` runs
> the game standalone from the data-only boot image, and
> `scripts/acceptance_cpuless.py` proves it BYTE-EXACT against the interpreted
> oracle over the whole demo (regs + flags + poison-masked memory at every
> boundary).  The generic machinery that made this automatic is catalogued in
> §CPUless machinery below; everything game-specific stayed in `lemmings_port`
> as recovery facts (entries, dispatch/boundary/vector facts, the recovered
> corpus).

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
→ release profile
→ automatic memory-structure recovery
→ generated native↔historical-state verification bridge
→ clean source port
```

This framework is **from AI, for AI**: the operator is an autonomous agent,
and every rule below is written to keep that agent building the machine
instead of hand-porting the game.

---

## The overarching goal (owner, 2026-07-18) — the north star

Every stage below is a MEANS.  The end is this:

> Use deterministic, automated recovery tools to expose as much of the
> original game as possible in a form that is **explicit, structured, and
> easy for AI to understand**.

VMless, CPUless, ABI recovery, de-stacking, memoryless state recovery,
generated bridges and oracle verification are **not ends in themselves**.
They are successive removals of the historical machinery that HIDES the
actual game: instruction interpretation, registers, calling conventions,
stack mechanics, flat DOS memory, raw addresses, compiler artifacts.

The final recovered program should present the game's real logic, state,
data relationships, rendering boundaries and platform interactions as
directly as possible.  AI should not have to infer behavior from assembly,
anonymous offsets, or machine-state plumbing: it should work with ordinary
functions, explicit parameters, native objects, typed fields, clear
ownership, and separated gameplay / rendering / input / audio / platform
layers.  The better the tooling exposes that structure, the less AI has to
guess, and the more reliably it can do the final semantic pass — naming,
simplifying, organising.

**The measure of success is not that the game runs without an emulator.**
It is that the recovered code is an ideal working representation for AI:
small enough, explicit enough, and understandable enough that substantial
modifications are quick and safe — modern rendering, widescreen/ultrawide,
smooth cameras, high-refresh presentation, better audio, gamepad and touch
input, mobile ports, editors, modding, new gameplay — as ordinary additions
rather than fresh reverse-engineering projects.

Automate the archaeology ONCE, so AI spends its effort understanding and
extending the game instead of fighting the machine it was compiled for.

### What this rules OUT, concretely

A stage is not "done" merely because a wall holds and the oracle is green.
If the emitted form still speaks in machine vocabulary, the stage has moved
the plumbing without exposing the game.  In particular, these are
acceptable as INTERMEDIATE emission detail and are NOT acceptable in the
final representation:

* basic-block dispatch loops (`while True: if bb == 3:`) instead of
  ordinary `if` / loops / early returns;
* register-named locals (`ax`, `si`) where the value has a semantic role;
* per-operation flag computation that nothing observes;
* raw `mem.rw(seg, off)` with historical addresses;
* register-shaped or compat-shaped public signatures.

A stage may legitimately POSTPONE any of these — but the postponement must
be recorded as debt against this goal, never re-labelled as "fine because
the next stage does not need it".  (See `docs/abi_end_state.md` §6, which
was revised after this goal was stated: the M4-need test and the
final-representation test are different tests, and the disposition table
now says so.)

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

### Stage 2b — ABI-RECOVERED CPULESS

Stage 2 removes the CPU *carrier*; its first (mechanical) form still exposes
the historical machine **calling convention** in every public contract:

```
mechanical CPUless      func(mem, ax, bx, ds, si, ss, sp, plat, ...)
ABI-recovered CPUless   func(mem, value, destination, object_index)
DOS-layout-less         func(game_object, value)
```

ABI-recovered CPUless is the second form of stage 2 — a separate milestone
BEFORE any state moves into authoritative native objects.  The recovered
graph keeps the historical memory image authoritative (``mem`` and historical
offsets may remain), but its **public contracts stop being register-shaped**:

- real input parameters are inferred from register/stack live-ins;
- return values are inferred from caller-observed outputs (never "every
  modified register for compatibility");
- stack arguments become ordinary Python parameters; callee-clean vs
  caller-clean is proven, not assumed;
- register pairs (DS:SI …) become pointer-like values or typed **memory
  views** where their role is proven;
- scratch registers become locals; caller-live condition results become
  explicit booleans where meaningful;
- recovered functions call each other through the recovered contracts —
  historical return-address mechanics leave the public graph.

Early **types are encouraged** when they describe contracts without
transferring state authority (FarPointer, BufferView, Rect, view classes
wrapping ``mem`` + historical address).  Every generated type is explicitly
one of: *value type* | *historical memory view* | *native authoritative
model* — and stage 2b may only produce the first two.  A view over ``mem``
is still ABI-recovered CPUless, not DOS-layout-less: the historical image
remains authoritative.

Strict output taxonomy, per function: **semantic inputs/outputs** (the public
recovered API) vs **machine-derived temporaries** (locals) vs **private
compatibility metadata** (exit flags, virtual-time cost, return-address
bytes, stack residue — allowed only in a private generated verification
path).  There is ONE generated algorithmic core per function with two
generated entrypoints: a private compatibility entry preserving the exact
historical ABI for oracle verification, and the public ABI-recovered entry.
No duplicate implementations — the later DOS-layout-less transformation must
be able to swap views for native objects without rewriting the algorithm.

Provenance: the original address is the permanent identity (``1010:4B68``);
recovered names attach as metadata (``format_decimal [1010:4B68]``) through
the naming/recovery-facts table, and are never a physical replacement for
the numeric identity.  Names may start conservative and structural.

The transformation is built over the recovery IR and the completed CPUless
call graph — never by parsing generated Python.  Contract inference is
promoted bottom-up to a fixpoint; conflicting call sites or uncertain
contracts **fail loudly** (no silent fallback to a register-shaped
signature).  The mechanical CPUless graph remains the generated reference:
every promoted contract is differentially verified (returned values,
observable memory effects, caller-live conditions, platform effects, virtual
timing where still required, complete deterministic demo behavior), with the
oracle bridge as final acceptance.

The stage-2b hard wall: **the recovered program no longer exposes or depends
on the historical CPU calling convention, even though its data may still
live in the historical memory layout.**  Do not call this form memoryless.

### Stage 3 — DOS-LAYOUT-LESS NATIVE

The historical DOS memory model is removed.  Game state is no longer a flat
byte image of DGROUP offsets, segment:offset pointers, fixed binary structures
and overlapping scratch buffers — it is native structures: dataclasses, typed
arrays, enums, references, named fields, explicit ownership.

```
before:  x = mem.read_u16(LEMMING_BASE + index * 32 + 4)
after:   x = state.entities[index].field_04
```

**The field is ANONYMOUS on purpose.**  An earlier revision of this section
wrote `game.lemmings[index].x`, which quietly claimed two different things at
once: that the layout was recovered, and that the *meaning* of the field was
known.  Only the first is mechanical.  Stage 3 recovers native values, arrays,
records, fields, indexing and ownership; it does not and cannot recover that
offset 4 is "the x coordinate".  `entities[i].field_04` is a complete,
correct Stage 3 result.  Renaming it to `lemmings[i].x` is Stage 4 work and a
separate claim (see *Two claims, never merged*, below).

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

### Two claims, never merged: behaviour and meaning

Everything this pipeline verifies is a claim about **behaviour**.  Nothing it
verifies is a claim about **meaning**.  They are produced by different
machinery, carry different evidence, and must be reported separately:

| claim | produced by | evidence | example |
|---|---|---|---|
| behavioural | mechanical recovery | oracle differential, byte-exact boundaries | `state.entities[i].field_06 += state.entities[i].field_0A` is what the program does |
| semantic | a later naming pass (Stage 4) | human or AI judgement, reviewable | `field_06` is the lemming's x coordinate |

A mechanically recovered `entity.field_06` is a FINISHED Stage 3 artifact,
not a half-done Stage 4 one.  The structure is proven; the name is absent
because no proof produces names.

The failure mode this rule exists to prevent: a plausible name makes an
unproven structure look verified.  If a field is called `x` and the layout is
wrong, every reader believes the wrong thing twice as fast.  So the pipeline
emits anonymous fields, and a naming pass may later attach names *as a
separate, separately-reviewable claim* — never by editing the recovery output
in place and calling it the same artifact.

Message wording: prefer *full VMless lifted graph*, *VMless lifted candidate*,
*CPUless candidate*, *DOS-layout-less candidate*, *semantic native
implementation* — never *full native runtime* / *native assembly* / *native
hooks* for a merely-VMless artifact.

### 1a. Hard execution walls as implementation properties

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

These walls classify generated or authored implementations and state adapters.
They feed the unified planner's detachment report. They do not require a
uniform recovery level across a game and do not fix runner names. One
profile-driven `play.py` runs development, verification, detached, or release
plans. A separate closed-world exporter produces standalone artifacts;
Recovery stages are implementation properties, not player identities. There is
only one profile-driven player.

There are **two independent walls at the VMless stage**, and both must hold
before M2 is accepted:

- the **VMless execution wall** above (no interpretation inside the corpus); and
- the **EXE-independence wall** below (the runtime never sees the binary).

### 1a′. The EXE-independence wall

> The EXE goes into the recovery pipeline.  Generated host code and data come
> out.  The VMless runtime never sees the EXE again.

The VMless execution wall says the recovered CODE runs as host code.  The
EXE-independence wall says the runtime does not depend on the original
executable *at all* — not for code, not for data, not as a hidden byte source.
It is enforced **physically**, not by convention:

- **Data-only boot image.**  `scripts/build_vmless_boot_image.py` runs the
  loader (the PKLITE self-extractor + the machine-type menu — interpreted, at
  BUILD time) from the EXE to the canonical post-decompression entry, captures
  the memory + machine state, and **poisons the recovered code**: every byte the
  recovery IR decoded as an instruction is ZEROED.  The generated lifted host
  functions are the only executable implementation of those routines; a zeroed
  range trips neither the lifted entry guard (`self_disable_if_patched` treats
  all-zero as "no live signature"; `cpu.code_poisoned` disables it outright) nor
  — behind the armed interpreter poison — any usable interpretation.  Bytes the
  game reads AS DATA (self-checksums, embedded tables) are declared
  `code_as_data` in the recovery facts and preserved.  Output:
  `generated/vmless_boot/{memory_1mb.bin, state.json, manifest.json}`.
- **EXE-free load path.**  `dos_re.runtime_core.create_runtime_from_image` +
  `dos_re.snapshot_runtime.load_snapshot_headless` builds the runtime from the
  image alone (`program.exe is None`).  The EXE loader (`create_runtime` →
  `load_mz_program`) lives in a *different* module that the VMless graph never
  imports.
- **Interpreter poison.**  `cpu.interp_forbidden` is armed from instruction
  zero; any attempt to fetch/decode an original instruction raises.
- **File-access guard.**  `lemmings.vmless_boot.exe_access_guard` refuses to open
  the binary by name OR by content hash (a rename does not launder it), while
  leaving game data readable.

Enforced by tooling: `scripts/lint_vmless_independence.py` (static import-graph
proof the runtime reaches no loader edge), `scripts/audit_vmless_boot_image.py`
(no bundled executable; every recovered code byte poisoned or declared
`code_as_data`), and `tests/test_vmless_cleanroom.py` (boot + run to gameplay in
a temp dir with the EXE physically absent).  The runner prints a DERIVED banner
(`independence_report`) ending `EXE-independence wall: HOLDS`.

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

**Which parts are scaffolding.**  §1b was written before M3b and M4 existed,
so it did not name the machinery those stages introduced.  All of the
following are *scaffolding to reach the highest representation* — verification
and migration apparatus — and none is required to appear in a final game
runtime:

| scaffolding | exists to | belongs in the final runtime? |
|---|---|---|
| VMless / CPUless emissions | bisect and prove one detachment at a time | no — diagnostic projections |
| ABI adapters (`_entry_ip`, compat entrypoints) | let a recovered core be substituted into a still-mechanical graph | no |
| native-state accessors (`NATIVE.rb/wb`) | keep a promoted region mem-shaped while its owners are still mechanical | no — the target is a plain field access |
| generated codecs (import/export) | regenerate historical bytes so the oracle can still compare | no — verification only |
| poison | prove at runtime that no historical reader survives | no — a proof device |
| the M4 bridge | attach/detach native state around a boundary comparison | no |

The end state is **bridgeless and memoryless**: native structures, direct
field and array access, ordinary control flow, and no residual object whose
purpose is to translate back to a DOS layout.  If a mechanism's only job is to
make the old and new representations comparable, it is scaffolding by
definition, and the milestone that introduced it owes an explicit removal
gate.

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
  paths raise `HybridGap` with a `ReplayArtifact` point and replay command.
  Nothing is
  silently faked or silently handed back to hidden emulation.
- **End-to-end oracle comparison** protects against UNKNOWN silent mistakes
  (translator, linker, scheduler, platform).  Full guest state is diffed
  against the interpreted oracle over the relevant stable-point intervals.
  **This is the authoritative gate.**

Consequences:

- **Assemble the largest supported graph as early as possible.**  Mechanical
  integration is optimistic within the declared supported subset.
- **Per-function ORACLE_PASSING does not gate linking or inclusion in
  `release profile`.**  Per-function proofs remain useful metadata — hybrid
  auto-install, diagnostics, regression tests, later refactoring — but they
  are not a precondition for graph assembly.
- **Divergences are localized automatically** (`dos_re.replay.bisect_divergence`
  searches stable replay points for the smallest observed failing transition),
  then the function-visit index identifies the covered code to inspect.
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

**The recorded replay artifact is the test.** Replay exactly the same semantic
events on the oracle and candidate between the same stable points, then compare
complete continuation or canonical semantic state. Never approximate the
recording with synthetic frame-number drives, periodic key spam, or mouse-hold
heuristics—a guessed test verifies a guessed path. Artifacts or derived
boundaries with stale identities are rejected; unsupported old recordings are
discarded and recorded again. Input models are investigated only when the same
artifact succeeds on the oracle and diverges on the candidate.

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
- Existing reusable material: the `release profile` shells of **pre2_port,
  skyroads_port, overkill_port** already contain adapter-shaped video/input/
  timing code to mine; **`opl3_fast.py` is usable as-is as the audio-adapter
  synth in a native game**.

The progression:

```
binary → automatic lifting → effect recognition → native adapter binding
→ linked CPU-less game graph → generated release profile
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
- **M3 — CPUless graph.  ACCEPTED (2026-07-16).**  The CPU carrier is removed
  from the required reachable graph: every reachable function is a recovered
  CPUless implementation, verified byte-exact against the oracle standalone
  (§CPUless machinery).
- **M3b — ABI-recovered CPUless graph.**  The mechanical CPUless graph's
  public contracts stop being register-shaped (§Stage 2b): inferred real
  parameters, caller-observed return values, stack args as normal
  parameters, pointer/view types where proven, direct recovered-to-recovered
  calls through recovered contracts, dual generated entrypoints (private
  compat for verification, public ABI-recovered), provenance naming.  The
  recovery-time memory image stays authoritative; the canonical replay stays
  oracle-clean.  Complete when no public recovered contract contains a CPU
  object, register-named parameter, or return-address mechanics — and
  unsupported ABI shapes fail loudly with evidence.  **End state + acceptance
  gate: `docs/abi_end_state.md`** — which machine concepts must be eliminated
  before M4 (generic virtual stack, register-named public parameters,
  dict-keyed results, dead flag computation), which may remain as
  deterministic emission detail (CFG-shaped `bb` bodies, register-named
  *locals*, partial-register widths — M4 analyses read the IR, not emitted
  Python), and which must remain as private compatibility metadata
  (`_fmask`, exact flag word, virtual cost).  Every function must end as a
  de-stacked core OR a named exception class with a generated representation.
- **M4 — DOS-layout dissolution.**  Historical memory structures are replaced
  with native objects, oracle verification retained through the generated
  bridge: historical memory views → authoritative native dataclasses and
  object graph → generated bridge back to the original layout → memoryless
  runtime.  **Design: `docs/memory_schema.md`** — a generated Memory Schema
  IR is the single layout authority; native dataclasses hold ORDINARY
  DETACHED VALUES (a field that secretly re-reads flat memory is the
  anti-pattern, not the goal); import/export bridges and field-level diffs
  are generated from the schema and exist only in the verification/migration
  environment; each promoted region is protected by a fail-loud wall.  The
  machine stack was already promoted this way in M3b slice 2 (virtual stack +
  proven no-alias), which is the pattern in miniature.

  **What M4 must mechanically recover** (clarified 2026-07-18, after the first
  scalar promotion): native values, **arrays**, **records**, **fields**,
  **indexing**, ownership and direct control flow — while eliminating
  registers, segments, the flat DOS image, historical offsets and runtime
  bridges.  The target shape is

  ```
  state.entities[i].field_06 += state.entities[i].field_0A
  ```

  with ANONYMOUS field names (§*Two claims, never merged*).

  **Status honesty.**  The first slice (`ds:[0xA949]` — one byte, one owner)
  proved the machinery end to end, but it is the degenerate case: no index, no
  record, no stride.  The indexed-region capability — array base, element
  stride, field offsets within an element — is the SUBSTANCE of M4 and is not
  yet built.  So M4 is *started*, not *half done*: one promoted scalar is a
  working pipeline, not a dissolved memory model.
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

## 6a. CPUless machinery (the generic de-carrier, M3 — reusable)

Everything below lives in `dos_re` and is game-agnostic; a new port supplies
only recovery FACTS (entries, boundary heads, dispatch/vector evidence) in its
own tree.  The Lemmings pilot exercised all of it; nothing here is
Lemmings-specific.

- **ABI inference** (`lift/cpuless.py`).  Over the shared recovery IR: register
  live-ins (exit-seeded backward may-liveness), register outputs (every
  written reg — the boundary differential observes the full file), must-defined
  flag analysis, and stack-depth analysis (a per-address depth SET; negative
  depth = a caller-frame read; unbalanced/varying exits make `sp` an output;
  correlated-branch explosion widens to UNKNOWN rather than refusing).  Each
  refusal names the missing capability — the promotion work-list.

- **The CPUless emitter** (`lift/emit_cpuless.py`).  Emits, per promotable
  function, a RECOVERED module (pure Python over `(mem[, plat], *regs)`,
  imports nothing, returns semantic outputs) and a generated CPU-ABI ADAPTER
  that occupies the lifted slot.  Owner corrections are load-bearing: exact
  timing and exit-flag reproduction ride a hidden `_compat` channel owned by
  the adapter, NEVER the recovered API; dead registers/flags are not semantic
  outputs; uncertain contracts REFUSE loudly.  Covered constructs: the full
  16-bit integer/logic/shift/rotate (incl. adc/sbb carry-chain, rcl/rcr
  through-carry), string ops (df/if as compat bits, rep = one instruction of
  virtual time), call-ABI composition (bottom-up DAG fixpoint; near + static
  far + the MSC push-cs idiom), stack-argument ABIs, the frameless Borland
  cdecl idioms (`add/sub sp,imm` as a constant depth delta; `mov bx,sp` /
  `mov bp,sp` capturing a frame base to read args — sp stays statically exact,
  so it is stack discipline, not sp-as-data), and hidden compat inputs
  for the direction flag (`_df`) and the full flags word (`_flags_in`).

- **Platform effects — two backends of one duck-typed contract**
  (`lift/platform.py`).  `plat.inp/outp` (port I/O), `plat.intr` (INT services
  as an explicit register-bundle request/response), `plat.boundary` (the
  scheduler seam).  `VMlessPlatformAdapter` binds effects to a live VM for
  VERIFICATION; `CPUlessPlatformRuntime` is the STANDALONE owner (its own
  clock + a pure device model, no CPU/interpreter/lifted).  Timing is
  backend-independent metadata: each effect gets the recovered graph's absolute
  instruction offset (`_base + _cost + in-block`).

- **Dynamic control flow → explicit recovered dispatch.**  A near indirect
  call/jmp resolves its runtime selector through a generated DISPATCH registry
  (or an intra-function jump-table landing); game-vectored interrupts and ISR
  chains route through a generated HANDLERS registry with literal interrupt
  frames; an unknown selector raises a structured `UnknownDispatchTarget`
  witness — never a CPU/interpreter fallback.  Promotion is EVIDENCE-GATED:
  a function with dynamic transfers promotes only when every probe-observed
  target is dispatchable; mutually recursive dispatch clusters promote
  atomically.  Interrupt/iret frames, `pushf/popf`, and the far-vector ISR
  chain are all modelled as literal stack data.

- **Boundary observers + the standalone scheduler.**  A boundary head inside a
  recovered body emits a `plat.boundary` call; the standalone runtime parks the
  program on a worker thread and the shell releases one boundary per frame,
  delivering input + timer IRQs through the game's OWN recovered ISRs.  The
  standalone ISR delivery RE-DISPATCHES (run handler → pop its iret frame →
  follow a resume/alternate entry until control returns to the interrupted
  point) — the explicit form of what a VM's fetch/dispatch loop does implicitly.

- **The promotion pipeline** (`tools/cpuless_promote.py`, `cpuless_census.py`,
  `cpuless_closure.py`).  A fixpoint DAG driver promotes functions whose callees
  compose, emits the recovered + adapter pair and the DISPATCH/HANDLERS
  registries, and measures completion by the required RUNTIME CLOSURE from the
  declared roots (not "all named functions promoted").  The block-dispatch loop
  carries an iteration cap so an unbounded spin fails loud with the function +
  block rather than freezing.

- **Two-level acceptance.**  The function differential (`verify_cpuless.py`
  pattern) checks recovered-vs-oracle over randomized register/flag/memory
  trials; the demo differential is the authoritative gate — the port's recorded
  demo replayed on the oracle and on the recovered program in lockstep, masked
  byte-exact at every boundary.  The clean-room form runs the STANDALONE program
  (no VM at all) against the oracle.

---

## 7. Canonical summary

**The scripts perform the transformations.  The AI removes the obstacles.
The oracle decides whether the result is correct.**

```
Deterministic tooling does the labor.
AI provides judgment where automation is blocked.
Oracle verification checks every result.
```
