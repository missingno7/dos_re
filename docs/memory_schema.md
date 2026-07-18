# M4 design: the Memory Schema and the generated detachable bridge

Status: **accepted design direction** for M4 (DOS-layout dissolution), owner
proposal 2026-07-18, evaluated against the M3b (ABI-recovered CPUless)
architecture actually in the tree.  This document records the evaluation,
what already exists, the one genuinely missing analysis, and the smallest
safe first slice.  It is a design record, not an implementation.

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

## 3. What already exists (verified in-tree, not assumed)

The proposal's infrastructure is substantially present:

* **Masked differential.** `scripts/acceptance_cpuless.py` already digests
  `registers + POISON-MASKED memory` per boundary: `_poison_mask(manifest)`
  builds a byte mask from declared ranges, `_masked()` applies it before
  hashing.  Per-region STRICT/OPAQUE/ELIMINATED policies are a
  *generalization of a mask that already exists*, not a new mechanism.
* **Boundary snapshots are sufficient for import/export.** The gate holds
  BOTH sides' full memory images at every boundary and reports differing
  ranges (`_mem_diff_ranges`).  The proposed cycle (oracle snapshot →
  import → native tick → export → compare) consumes exactly these artifacts.
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
* **The substitution seam exists.** The M3b shadow loader swaps
  implementations via `sys.modules` pre-registration (+ retro-patching of
  already-bound module-level names).  The proposed runtime modes
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
  Yes; the acceptance gate already materialises both full memory images per
  boundary, which is exactly the import bridge's input and the export
  bridge's comparison target.
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
  **After** — and this validates M3b as the prerequisite.  On a de-stacked
  ABI core, a memory access is already `mem.rw(seg, <expression over named
  semantic parameters>)`; the address expression is explicit at the Python
  level instead of buried in register/stack juggling.  Extraction on the ABI
  cores is both easier and more precise than on mechanical cores, and the
  contract inputs give the expression its symbolic roots.
* **Smallest safe M4 slice?**  See §6.

## 6. The smallest safe first slice

Two steps, smallest first.  Both target regions this port already
understands independently, so a wrong schema is caught by knowledge as well
as by the differential.

**Step 1 — fixed-base scalar globals.**  Take a small cluster of
direct-addressed (`mod=0, rm=6`) DS globals whose semantics are already
proven — e.g. the render-scroll scalars at `ds:0x00` / `ds:0x02`.  These have
no stride, no index, no aliasing question, and known widths.  Prove the whole
loop end to end: schema → dataclass → import/export → rewrite the access
sites → poison the range → oracle differential green.

**Step 2 — the first array-of-structs: the lemming array.**  `ds:0x00B0`,
45 records × 45 bytes, with `+0` anim frame (u8), `+1` action (u8), `+5`
world-X (u16), `+7` world-Y (u16), `+9` dx (signed, `>=0x8000` = facing
left).  This is the ideal first array because the base, stride, count, and
five field offsets/widths/signednesses are independently established, and
the ~40 remaining bytes per record exercise the **OPAQUE policy** (preserve
unknown bytes rather than zero them) which is the single most important
correctness rule for partial promotion.

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
