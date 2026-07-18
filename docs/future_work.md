# Future work — examined, deferred, and why

Ideas that survived review but are **not** on the roadmap.  Nothing here is an
active milestone; nothing here should be cited as a plan of record.

This file exists because the alternative is worse.  Good ideas with no home
either get implemented early (growing the system before it needs growing) or
get rediscovered from scratch every few months.  A deferral with a written
reason is cheaper than either.

Each entry records: the concrete problem, the prerequisite, why not now, the
smallest falsifiable prototype, and where it would live —
**dos_re** (game-agnostic mechanism), a **platform layer** (`win16_re` and
friends), or a **game port**.

Most entries came from a source-grounded review of Ghidra (2026-07-18).  The
principle that *was* adopted from it — declare instruction semantics once —
is already implemented as `lift/effects.py` and is described in
`docs/architecture.md`, not here.

---

## 1. SSA / def-use and value-range analysis

**Problem.**  Three separate blockers are the same missing capability:

* `computed-ss-address` (9 functions refused, 37 in the cascade) needs the
  index register at `mov r8, ss:[bx]` bounded;
* the implicit-string-access blocker (814 instructions across 99 functions)
  needs `si`/`di` ranges before those accesses can be attributed to a region;
* indexed-region recovery for M4 needs an element stride and a bounded index
  to propose an array at all.

Today each would be a bespoke backward walk.

**Prerequisite.**  `lift/effects.py` consumed by the census (the migration in
§*Immediate* of the roadmap).  A range analysis over per-consumer opcode
tables would inherit exactly the drift that layer removes.

**Why not now.**  M4's indexed-region work may not need it.  A cheaper lever
exists and is untried: **segment provenance**.  Most of the 84 functions that
cannot currently be ruled out as touchers of a promoted `ds` region are
renderers that reload `ds` to a sprite or level-data segment before the
access; if `ds` at the site is provably not the globals segment, the function
is excluded with no reasoning about `si` at all.  Build the cheap filter,
measure the residue, and only then decide whether ranges are needed.

**Smallest falsifiable prototype.**  `CircleRange`-style intervals mod 2¹⁶
with an explicit `step`, plus a bounded backward walk over `effects_of`, for
ONE function.  Ghidra's own pull-back is refusal-first (it handles six
opcodes and returns false otherwise), which is the right shape.  The gate is
**observed ⊆ predicted**: log the actual register values at each site during
an oracle run and assert the computed range contains every one.  A range that
misses an observed value is falsified immediately; a range of ⊤ is useless but
honest.

Explicitly NOT worth building: the weak-topological-ordering fixpoint solver
with widening operators.  Ghidra has one and uses it in exactly one place
(stack load-guard ranges) — jump tables, the obvious customer, use interval
intersection instead.

**Where.**  dos_re.

---

## 2. Scoped platform and game fact specifications

**Problem.**  Facts that come from outside the binary are scattered across
three incompatible mechanisms: `ss_globals_floor` is a function argument,
`keep_interpreted` / `boundary_heads` / `dispatch_entries` are flat address
lists in `facts_applied`, and `setjmp`/`longjmp` handling is hardcoded inside
`emit_cpuless.py`.  Thirteen refusal classes need permanent generated
representations, and at least three of them (`game-vectored-int`,
`iret-contract`, the existing setjmp/longjmp case) are literally "replace a
known routine's semantics" — which is a declaration, not code.

**Prerequisite.**  None technically; it is a refactor.  But it should follow
the effects migration so that fact *kinds* are described against a stable
semantic vocabulary.

**Why not now.**  It reorganises working machinery without changing any
result.  That is worth doing when the number of fact kinds justifies a schema
— and today's honest count is small enough that a schema would be
speculation.  Revisit when the fourth kind appears.

**Smallest falsifiable prototype.**  A `facts.json` with
`(scope: program | function | address-range, kind, payload, evidence)`, where
`evidence` is mandatory.  First slice moves `ss_globals_floor` and the
setjmp/longjmp case into it with **zero behaviour change**, proven by the
964-boundary acceptance being byte-identical and the verified-core count
unchanged.

**Design note worth keeping.**  Ghidra's version is instructive: declared and
recovered prototypes are the *same object* distinguished by a lock bit, and
the lock **gates whether recovery runs at all** rather than overriding it
afterwards.  Varargs is the one case that punches through, because a declared
signature is only a prefix of the truth.  If this is built, build it that way:
one representation, a provenance bit, and inference skipped — not layered — where
a fact exists.

**Where.**  Mechanism in dos_re; DOS/BIOS interrupt semantics in a platform
layer; per-game vectors and floors in the game port.

---

## 3. Ghidra as a decoder-boundary cross-check

**Problem.**  The oracle differential has one blind spot by construction: it
compares what the emitter *emits*.  If an analysis never looks at an
instruction class, nothing diverges.  That is exactly how 814 string
instructions stayed invisible to the census — **the oracle could not have
caught it**.  An independent decoder disagreeing about instruction boundaries
would have.

**Prerequisite.**  A Ghidra install (none present; Java 21 is).

**Why not now.**  `lift/effects.py` plus its corpus shadow already closed the
specific gap, and the decoder's lengths are cross-checked against the
interpreter's IP-delta today.  This is insurance against a *future* gap, and
insurance is worth buying when there is something to insure.

**Smallest falsifiable prototype.**  Headless export of
`(offset, length, mnemonic)` for the code segment; diff against `dos_re`
decode; require 100% agreement on instruction boundaries.

**Scope it narrowly, and this is not optional.**  Compare boundaries, lengths
and operand widths inside a known code range only — never addresses, segment
resolution, or call targets.  Ghidra's real-mode support has open defects in
precisely those areas (segment starts, call/jump target computation, the
`segment` userop missing from its own emulator library), so a broader
comparison would generate noise that looks like signal.

**Where.**  dos_re, as a dev-time gate.  Never a runtime or CI dependency.

---

## 4. Automatic proposals for indexed regions and structures

**Problem.**  Every M4 region is hand-declared in the port's generate script.
That does not scale past a handful, and the interesting regions (the lemming
array at stride 45; the 416-byte row stride in `1010:1F19`) are exactly the
ones where hand-declaration is most error-prone.

**Prerequisite.**  The census must be able to see all touchers — i.e. the
effects migration, plus enough of §1 to attribute implicit accesses.  A
proposal engine fed by a census with known blind spots would propose confident
nonsense.

**Why not now.**  It is the natural *second* half of M4's indexed-region work,
and the first half (expressing an indexed region at all) does not exist yet.
Building the proposer first would mean generating proposals nothing can
consume.

**Smallest falsifiable prototype.**  From `Census.indexed_clusters()`, emit
*candidate* regions with `(base, stride, element width, field offsets)` — as a
report, never an automatic promotion.  The gate is reproduction: the proposer
must independently produce the already-hand-written `ds:[0xA949]` region and
the stride-45 array shape.  Anything it proposes beyond that is a hypothesis
for a human or a later pass, not a fact.

**Design note.**  Ghidra reconciles overlapping access hints into a layout
(`RangeHint` carries `fixed` / `open` / `endpoint`, where `open` means an
array of unknown length) and gates promotion on an alias check.  Our
84-possible-touchers result is precisely such an alias verdict, and `open` is
precisely how to express the lemming array before its length is proven.

**Where.**  dos_re proposes; the game port declares.  A proposal must never
become a declaration without passing through a reviewed artifact.

---

## 5. Control-flow structuring

**Problem.**  Recovered bodies are CFG-shaped: basic blocks and a dispatch
loop.  The Stage 3 target — `state.entities[i].field_06 += ...` — reads well
at the statement level, but the surrounding control flow is still a block
machine.

**Prerequisite.**  M4 structural recovery.  Structuring the flow around
`mem.rb(seg, off)` calls would have to be redone once those become field
accesses.

**Why not now.**  It changes no behaviour and proves nothing; it is
readability work, and readability is Stage 4's concern.  Doing it early also
risks it being mistaken for semantic progress.

**Smallest falsifiable prototype.**  Recover `if` / `while` for the reducible
subset of one function, with the byte-exact differential unchanged — the
structured form must be *provably the same program*, not merely a nicer one.

**Where.**  dos_re.

---

## 6. Semantic naming

**Problem.**  `state.entities[i].field_06` is structurally correct and
unreadable.

**Prerequisite.**  Everything above.  Naming a field whose offset or extent is
wrong is worse than leaving it anonymous — a plausible name makes an unproven
structure look verified (see *Two claims, never merged* in
`docs/dos_re_2.0.md`).

**Why not now.**  It is Stage 4 / M5 by definition, and it is the one part of
this pipeline that **no proof produces**.  Behavioural verification says
nothing about whether a field is a coordinate.

**Smallest falsifiable prototype.**  A naming pass that emits a *separate
mapping artifact* (`field_06 -> x`) reviewed on its own terms, with the
recovered code unchanged.  If applying the map is reversible and the
differential is untouched by it, the two claims stayed separate — which is the
property being tested.

**Where.**  A game port, or a later dos_re pass that consumes a port-supplied
map.  Never inline in the recovery output.
