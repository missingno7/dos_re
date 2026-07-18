# M4 design: the Memory Schema and the generated detachable bridge

Status: **accepted design direction** for M4 (DOS-layout dissolution), owner
proposal 2026-07-18, evaluated against the M3b (ABI-recovered CPUless)
architecture actually in the tree.  This document records the evaluation,
what already exists, the one genuinely missing analysis, and the smallest
safe first slice.  It is a design record, not an implementation.

The schema *mechanism*, generators, walls, and proof machinery belong in
`dos_re`.  A particular game's regions, offsets, inferred fields, and
declarations belong in that port.  Worked game examples below are evidence,
not framework defaults.

## 1. The requirement, restated

The outcome M4 must deliver (owner, non-negotiable):

1. the original flat DOS memory stops being the runtime authority;
2. native objects hold ordinary detached values;
3. all historical layout knowledge remains available for verification;
4. the bridge is GENERATED, never handwritten;
5. promoted regions are protected by a fail-loud hard wall;
6. oracle verification remains possible throughout the migration;
7. AI semantic naming happens only AFTER deterministic recovery + proof.

The anti-pattern to avoid is explicit: a dataclass whose `field_06` secretly
performs `memory.read_u16(base + 6)` is not memoryless — it is the flat model
wearing a costume, and the historical image is still the authority.

## 2. Verdict

**Adopt the Memory Schema + generated import/export bridge.**  It is the
cleanest route to the seven requirements, and — more importantly — it is the
same architecture the pipeline already uses one stage down.  The mapping is
almost exact:

| CPUless / M3b (built, proven)              | M4 analogue (proposed)                  |
|--------------------------------------------|-----------------------------------------|
| Recovery IR = single code-identity truth    | Memory Schema IR = single layout truth  |
| ONE core, two entrypoints (compat + public) | ONE native object, import/export bridge |
| contract-proof shadow (poisoned outputs)    | region poison + field-level diff         |
| CPUless wall (no CPU/interpreter route)     | Memoryless wall (no FlatMemory route)    |
| `abi_promote --cores` bottom-up fixpoint    | region-by-region promotion              |
| refusal census = the work list              | `MemorylessPromotionBlocked` = work list |

The proposal is not a new paradigm; it is the existing one applied to data.
That is the strongest argument for it.

## 3. What can be reused

The proposal's infrastructure is substantially present, but it is split
between this framework and the Lemmings reference port.  Do not mistake a
port-side acceptance script for an in-tree framework facility:

* **Masked differential.** The Lemmings port's
  `scripts/acceptance_cpuless.py` already digests
  `registers + POISON-MASKED memory` per boundary: `_poison_mask(manifest)`
  builds a byte mask from declared ranges, `_masked()` applies it before
  hashing.  Per-region STRICT/OPAQUE/ELIMINATED policies are a
  *generalization of a proven port-side mask*, not yet a reusable M4
  framework mechanism.
* **Boundary snapshots are sufficient for import/export.** The gate holds
  BOTH sides' full memory images at every boundary and reports differing
  ranges (`_mem_diff_ranges`).  The proposed cycle (oracle snapshot →
  import → native tick → export → compare) consumes these byte artifacts.
  This is sufficient only at a declared quiescent boundary and only when the
  gate also compares all non-memory observable effects (platform events,
  files/devices, return values, and virtual time where relevant).
* **Field-level diagnostics are a strict upgrade of existing output.** The
  gate reports raw address ranges today; the schema supplies the
  address→field naming that turns `memory differs at DS:83AE` into
  `array_8300[7].field_06 differs`.
* **Poison-and-fail-loud is an established pattern.** `dos_re/independence.py`
  already poisons recovered CODE in the boot image and arms a wall so the
  interpreter cannot serve it.  Region poisoning is the data twin.
* **The type vocabulary is already canon.** M3b (§Stage 2b) requires every
  generated type to be classified *value type | historical memory view |
  native authoritative model*.  M4 is precisely the stage that is allowed to
  emit the third kind; the Memory Schema is what licenses the promotion.
* **A substitution seam exists in the reference port.** The M3b shadow loader
  swaps implementations via `sys.modules` pre-registration (+
  retro-patching of already-bound module-level names).  The proposed runtime
  modes
  (ATTACHED_DEBUG / IMPORT_EXPORT / SHADOW_VERIFY / DETACHED) should reuse
  this seam rather than invent a second one.

### 3a. De-stacking was already a miniature region promotion

M3b slice 2 promoted ONE region — the machine stack — and it rehearsed every
M4 obligation:

* the region left the memory image (push/pop became a Python list);
* the historical representation stayed available for verification;
* **ownership was proven, not assumed**: `abi_diff.TraceMem` gives the
  mechanical side a SHADOW-STACK overlay, modelling the program's no-alias
  precondition, and the differential fails if a semantic pointer ever
  aliases the promoted region;
* the gate (`check_composable`) REFUSES any function that reads its stack as
  memory (`stack-addressed-memory`, 59 functions) rather than guessing.

That is the M4 loop in miniature, already green on the canonical demo.  The
no-alias proof obligation should therefore be a **first-class schema
analysis**, not an afterthought — we have already needed it once.

## 4. The one genuinely missing analysis

The recovery IR pins instruction bytes and flags memory operands
(`"mem_operand": true`) but does **not** record a decoded address
expression.  Because bytes are pinned, the single decoder can re-elaborate
each site into `(segment, base_reg, index_reg, disp, width, signedness
evidence)` — the IR is *sufficient* — but the extraction pass does not exist
yet.  It is well-precedented: `cpuless.register_effects` already computes EA
base/index registers per instruction for the ABI analysis.

Corpus raw material (Lemmings, measured):

```
memory EAs: direct/static = 1621    register-indexed = 2232
```

The static half is the fixed-base-globals subset; the indexed half is where
array/stride recovery lives.

**Therefore the foundational M4 pass is:** decode every `mem_operand` site
into a symbolic address expression, then cluster the expressions into
candidate regions (fixed base / base+index*stride+offset), carrying the
read/write site lists as provenance.  Everything else in the proposal is
generation from that schema.

## 5. Answers to the owner's investigation questions

* **Does the IR contain enough address-expression information?**  Yes, via
  re-elaboration (bytes are pinned) — but the extraction pass must be
  written.  No IR format change is required, which preserves the
  single-source-of-truth rule.
* **Can typed stack views generalize to typed memory-region views?**  Yes,
  and the generalization is already half-done: M3b slice 1 emits the `(ss,
  sp)` pair as one explicitly-classified *historical memory view* parameter.
  `StructSchema`/`ArrayRegion` are the same idea with a richer descriptor.
* **Can the oracle differential consume strict/opaque/eliminated masks?**
  Yes — it already consumes a poison mask (§3).  The change is generating
  the mask per-policy from the schema instead of from a manifest range list.
* **Are boundary snapshots sufficient for import/export verification?**
  They are byte-complete inputs and comparison targets, but are not by
  themselves a complete semantic proof.  The boundary must be quiescent and
  the gate must include non-memory effects.  It must also include a
  continuation test that imports once and runs across several boundaries
  without re-importing (§11); otherwise lost native state can be refreshed
  from the oracle every tick and remain invisible.
* **How should ownership work during partial migration?**  Exactly one
  authority per region, enforced by TOOLING, not convention.  The CPUless
  wall taught this the hard way: it needs three checks (static lint, runtime
  check, import guard) because the runtime guard alone silently missed
  relative imports.  The Memoryless wall needs the same trio from day one,
  plus the ALIAS_VIEW policy so overlapping views are declared rather than
  discovered by a divergence.
* **How should pointer identity be represented after detachment?**  Keep
  `FarPointer(seg, off)` as a **value type** during migration.  Promote a
  pointer to an object reference (or region index) ONLY where its provenance
  is proven to stay inside one promoted region; otherwise refuse the field.
  Lemmings has real far-pointer-as-data (the sprite drawer takes a sprite
  bank SEGMENT in `cx`, re-allocated per load), so a scheme that assumes
  pointers are always intra-region will fail on this corpus.
* **Schema generation before or after recovered-to-recovered call emission?**
  **After ABI contracts are established, but never by parsing the emitted
  Python.**  M3b provides de-stacked parameters and contract roles as the
  symbolic roots.  The M4 pass re-elaborates the pinned instruction bytes and
  combines those address expressions with the ABI contracts in Recovery IR.
  Emitted Python is an output and review artifact, not an analysis database.
* **Smallest safe M4 slice?**  See §6.

## 6. The smallest safe first slice

Two steps, smallest first.  Both target regions this port already
understands independently, so a wrong schema is caught by knowledge as well
as by the differential.

**Step 1 — selected by census, not prescribed here.**  Run the EA/alias
census (§14.2) first, then select the SMALLEST FULLY VERIFIED OWNERSHIP
CLOSURE it reports.  The first region is whichever one the evidence says is
cheapest to own completely -- not whichever is semantically attractive or
smallest in bytes (§9).

**`ds:0x00–0x10` is a REJECTED first candidate.**  It was prescribed here as
"no stride, no index, no aliasing question".  The aliasing claim is false.
Measured over the recovery IR, these offsets are reached through BOTH default
`ds:` addressing and explicit `ss:` overrides -- the small-model SS == DS
idiom that M3b slice 9 had to promote separately:

| offset | reached via `ss:` | reached via default `ds:` |
| ---: | ---: | ---: |
| 0x00 | 45 functions | 11 functions |
| 0x02 | 49 | 5 |
| 0x08 | 56 | 3 |

So `ds:0x00` and `ss:0x00` are the same bytes reached through two segment
registers, and the ownership closure is ~56 functions rather than a handful.
That is precisely the shape §9 warns about: *a small byte range with a large
closure is not a small first slice*.  This document stated the rule and then
violated it in its own worked example.

Keep the range as a deliberate **segment-alias test case** for later, once
`ALIAS_VIEW` is implemented: it is the natural first exercise of a declared
alias with one canonical storage owner.

**Segment-alias canonicalization is conditional, not automatic.**  The schema
may canonicalize `ss:X` and `ds:X` to one owner ONLY where `DS == SS` is
PROVEN for the relevant lifetime and boundaries.  Otherwise it must refuse:
the two segments are equal by convention in this memory model, not by
architecture, and a program that reloads either register breaks the identity.

**The lemming array is a candidate, not a promotion.**  `ds:0x00B0`,
45 records x 45 bytes, with independently established base/stride/count and
five known field offsets, remains attractive because a wrong schema would be
caught by knowledge as well as by the differential.  But it is subject to the
same gates as anything else: access closure must prove the selected native
logic neither reads nor writes the ~40 opaque bytes per record, and `OPAQUE`
preserving historical bytes for comparison does NOT make an unmodelled
dependency memoryless.  If closure is broad, it is not the first slice either.

Explicitly deferred until those two are green: unions, dynamic bounds,
pointer tables, linked structures, interrupt-shared memory, memory-mapped
hardware (the VGA window at `0xA000` — note the terrain store lives there,
off-screen at `0xA000:0x4A40`, and is written by the game's own compositor),
code/data overlap, and self-modifying data.

## 7. Refinements this architecture suggests

Where the existing tree suggests a change to the proposal:

1. **Do not zero-fill the export image.**  The proposal already recommends
   starting from a copy of the oracle state and overwriting only recovered
   fields; the pilot's experience with the EXE-independence poison (a
   `code_as_data` self-checksum range had to be PRESERVED, not poisoned, or
   every downstream consumer diverged) is direct evidence that conservative
   preservation is mandatory, not optional.
2. **Make the region wall a trio of checks** (static lint + runtime check +
   import guard), mirroring `cpuless` enforcement, and add it in the same
   commit as the first promotion — not after.
3. **Reuse the shadow-loader seam** for ATTACHED_DEBUG / SHADOW_VERIFY
   rather than introducing a parallel mechanism.
4. **Carry the no-alias obligation in the schema** as an explicit provable
   property per region (the stack promotion needed it; every later region
   will too).
5. **Generate specialized bridge functions**, as the proposal says; the
   pipeline's existing habit (generated modules, no runtime reflection) and
   the byte-exactness requirement both point the same way.

## 8. Relationship to the milestone ladder

```
M3  mechanical CPUless          func(mem, ax, bx, ds, si, ss, sp)      DONE
M3b ABI-recovered CPUless       func(mem, value, destination, index)   IN PROGRESS
M4  DOS-layout-less             func(game_object, value)               THIS DESIGN
M5  semantic clean port         game.enemies[i].x                      AI naming, later
```

M3b is the enabling stage, exactly as the owner framed it: de-stacked cores
with semantic signatures are the substrate the address-expression extraction
runs on, and the stack is the first region already promoted and proven.

## 9. What the Memory Schema must model

A list of structs and fields is necessary, but not sufficient.  The schema is
the machine-readable **ownership and identity boundary** between the
historical layout and the detached runtime.  At minimum it must express:

* region identity, address extent, element capacity, live-count source, and
  initialization/lifetime rules;
* canonical storage ownership and every overlapping or partial-width view;
* field storage width and byte order separately from signed/unsigned use-site
  interpretation;
* native arithmetic normalization (for example 8/16-bit wrap) so an ordinary
  Python `int += delta` cannot silently acquire different overflow semantics;
* pointer provenance, null/equality semantics, target region, escape status,
  and the stable native handle/index used after detachment;
* all read/write sites and the ABI roots used to prove each address
  expression;
* the declared oracle boundary at which the field is observable;
* proof obligations, evidence references, and the generator/schema digest.

This means `SignedInt(bits=16)` must not be inferred merely because one use
performs a signed comparison.  The stored bits may have mixed signed and
unsigned interpretations.  Keep storage facts and use-site semantics separate
until a single native meaning is proven.

Likewise, replacing a far pointer with an arbitrary Python object reference is
not automatically faithful.  Pointer equality, stable identity, null,
serialization, and observed arithmetic may be behavior.  Use explicit stable
handles, region indices, or retained `FarPointer` values until the stronger
object-reference transformation is proven.

The unit of promotion is therefore not merely an address interval or one
dataclass.  It is an **ownership closure**:

```
region + all access sites + aliases + pointer sources/escapes
       + relevant functions + boundary effects + lifetime rules
```

A small byte range with a large closure is not a small first slice.

## 10. Separate ownership policy from comparison policy

The proposal's single policy enum mixes different questions.  `OPAQUE`,
`ALIAS_VIEW`, and `ELIMINATED` describe ownership/storage, while `STRICT`,
`SEMANTIC`, and `VOLATILE` describe comparison.  Model these as orthogonal
axes so impossible combinations can be refused explicitly.

For example:

| Axis | Example values | Question answered |
|---|---|---|
| ownership | `NATIVE`, `HISTORICAL_OPAQUE`, `ALIAS_VIEW`, `ELIMINATED` | who owns or materializes these bytes? |
| comparison | `EXACT`, `NORMALIZED(rule)`, `NOT_OBSERVED(reason)` | how is the declared boundary judged? |

Rules:

* `ALIAS_VIEW` has exactly one canonical storage owner and never exports
  independently.
* `ELIMINATED` requires evidence that the bytes are unobservable carrier
  state; exclusion from a digest is not that evidence.
* `VOLATILE` is not a pass condition.  Prefer a deterministic model or an
  explicit normalizer; otherwise the result is inconclusive for that
  boundary.
* `HISTORICAL_OPAQUE` may preserve bytes during migration, but promoted native
  logic must be proven unable to depend on them.  Before final DETACHED mode,
  each opaque range must become native-owned (possibly as an ordinary opaque
  `bytes` value), proven eliminated, or proven outside the runtime state.

## 11. Required proof matrix

The proposed one-boundary cycle is valuable but not sufficient on its own.
M4 acceptance should require all of these:

1. **Codec law:** import then export preserves every owned/preserved historical
   byte according to the schema, including padding, aliases, and partial
   writes.
2. **One-step equivalence:** import oracle boundary N, execute one detached
   step, export, and compare boundary N+1 plus all observable effects.
3. **Continuation equivalence:** import once, execute multiple deterministic
   boundaries without refreshing from oracle memory, and compare at each
   checkpoint.  This catches lost state, wrong object identity/lifetime, and
   accidental dependence on per-tick import.
4. **Access closure:** static analysis plus runtime poison proves no promoted
   byte is reached through historical memory, aliases, interrupts, DMA/device
   models, or escaped pointers.

   > **Correction (2026-07-18): "static analysis" was doing less work than
   > this sentence implies, and the first promotion shipped on the runtime
   > half alone.**  `Census.closure()` is computed only from addresses the
   > census can EXPRESS, so instructions with implicit operands contributed
   > nothing to it — the string instructions were invisible entirely.  With
   > those named and attributed by segment, the honest verdict for the first
   > promoted region is `1` definite toucher and **84 that cannot be ruled
   > out**.  None is shown to reach the region; none is excluded either.
   >
   > So the load-bearing evidence for slice 1 was the **poison run** — the
   > byte was poisoned for 964 boundaries and nothing diverged, which a hidden
   > reader would have broken.  That is real evidence and it is demo-scoped
   > evidence.  Static sole-ownership was NOT proven, and the gate should not
   > be described as though it were.
   >
   > A closure computed from an incomplete census is a claim about the
   > census, not about the program.  Until the possible-toucher set can be
   > driven to empty (segment provenance first, then ranges — see
   > `docs/future_work.md` §1), this gate is *empirical with a static hint*,
   > and region promotions should say so.
5. **Detached artifact test:** run the declared corpus with FlatMemory,
   bridges, historical address tables, and original memory artifacts absent
   from the import/runtime environment.
6. **Freshness:** every generated type, bridge, mask, rewrite, and diagnostic
   embeds the same schema/input digest; mixed or stale generations refuse.

Comparison proves observational equivalence at the declared boundaries.  If
transient effect ordering is observable, the gate must additionally compare
the ordered event trace or introduce an intermediate checkpoint.

## 12. Runtime modes and authority

Multiple modes are useful only if each names one authority:

| Mode | Authority | Allowed role of the other representation |
|---|---|---|
| `ATTACHED_DEBUG` | historical *or* native, declared per run | read-only diagnostics/shadow |
| `IMPORT_EXPORT` | transfers once at explicit boundaries | codec input/output only |
| `SHADOW_VERIFY` | native | historical representation is generated read-only evidence |
| `DETACHED` | native | historical representation is absent |

No mode permits two writable authorities.  Re-importing from historical memory
inside a native tick is a proof failure, not synchronization.

## 13. Implementation target

The desired result is not literally “without memory”; it is
**DOS-layout-less native state**:

* ordinary detached values, collections, stable references/handles, and
  explicit platform state are authoritative;
* generated historical codecs and provenance remain available to the
  verifier but are dependency-inverted away from gameplay;
* deterministic recovery establishes anonymous structural truth first;
* semantic naming and domain reshaping happen later in M5.

The Memory Schema plus generated detachable bridge remains the right
architecture.  The refinement is to treat it as a proven ownership graph with
codecs—not as a convenient struct declaration language.

## 14. Recommended implementation order

1. Stabilize M3b's verifier and ABI-contract ledger; M4 may consume only
   VERIFIED contracts in the selected ownership closure.
2. Add a read-only EA census over Recovery IR.  It should emit symbolic address
   expressions, candidate regions, aliases/escapes, evidence, and structured
   blockers without rewriting code.
3. Define and version the game-agnostic schema IR, policy axes, provenance, and
   digest rules in `dos_re`; keep concrete game schemas in the port.
4. Generate codecs, field diagnostics, masks, and codec-law tests from one
   deliberately small schema.
5. Select the first promotion by proven closure, not semantic attractiveness
   or byte size.
6. Generate the native state and access rewrite, transfer authority once, and
   install the static/runtime/import wall in the same change.
7. Require one-step, continuation, access-closure, and detached-artifact gates
   before publishing the region as promoted.
8. Expand the refusal census one generic blocker class at a time; regenerate
   all outputs after every capability addition.

### Status (2026-07-18)

Steps 1–7 are **done**, once, for `ds:[0xA949]` — one byte, one owner, all
gates green at 964/964 boundaries in both import and detached mode.  That
proved the machinery: schema → codec → detached native state → access rewrite
→ poison → oracle.

It is worth being blunt about what it did NOT prove.  That region is the
degenerate case — **no index, no record, no stride** — so none of the
machinery that makes M4 *matter* was exercised:

| capability | state |
|---|---|
| scalar region, single owner | done |
| array base + element stride | **not built** |
| fields within an element | **not built** |
| indexed access rewrite (`entities[i].field_06`) | **not built** |
| closure over an indexed region | **not built** (and see §11.4) |

Step 9, therefore, and it is the substance of M4 rather than an extension of
it:

9. **Indexed regions.**  Extend the schema IR with an array region (base,
   element stride, element record, field offsets), extend the codec generator
   to import/export an element-wise region, and extend the access rewrite to
   turn `mem.rb(seg, base + i*stride + off)` into `entities[i].field_off`.
   The lemming array (`ds:0x00B0`, stride 45) is the intended first case; its
   owner `1010:1F19` is currently refused (`computed-ss-address`), so a
   verified owner is a prerequisite and is NOT yet available.

Field names stay ANONYMOUS throughout (`field_06`, not `x`) — see
*Two claims, never merged* in `docs/dos_re_2.0.md`.
