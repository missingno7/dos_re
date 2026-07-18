# M3b end state: what "ABI-recovered CPUless complete" must mean

Status: **architecture review**, owner question 2026-07-18, answered against
the emitter and cores actually in the tree (101 de-stacked cores, all
differentially proven identical to their mechanical closures).

The question: is the current de-stacked/ABI direction correct, or are we
building a second long-lived mechanical representation that M4 will have to
undo?  And what, precisely, must be true before M4 starts?

## 1. Honest assessment

**The direction is correct.**  The load-bearing properties are already real
and proven, not cosmetic:

* contracts are narrowed by whole-program observer-aware analysis and the
  removals are oracle-proven by poison shadows (964/964 boundaries green);
* the machine stack has left both the contract and the memory image for the
  de-stacked tier, with the no-alias precondition modelled and tested;
* recovered-to-recovered calls carry no return-address writes and no sp;
* platform effects (port I/O, DOS/BIOS INT) route through `plat` with exact
  virtual time;
* every core is differentially identical to a fresh mechanical closure over
  seeded states, including flags, cost, memory writes, and port/INT traffic.

**But the emitted form carries four pieces of debt of very different
weight, and one thing that looks like debt and is not.**  Judging by
appearance would get all five wrong, so each is classified by a single
test: *does M4 have to undo it, or work around it?*

## 2. Debt classification

### 2a. `_vs: list[int]` — REAL debt, cheap to remove

The virtual stack faithfully reproduces LIFO *mechanics* that no longer
mean anything.  It is an emission scaffold, not a representation.

It is cheap to remove because `check_composable` **already proves a unique
virtual-stack depth at every ip** (it returns the depth map).  So every
push/pop resolves statically to a numbered slot:

```python
_vs.append(bx)      →      _slot_0 = bx
...                        ...
bx = _vs.pop()             bx = _slot_0
```

After this the generated body contains no stack object of any kind — which
is what "the compiler stack carrier is eliminated" should mean.  A generic
runtime stack retained "because emission is convenient" is exactly the
long-lived mechanical representation the owner warns about.

**Verdict: eliminate before M4.**

### 2b. Register-named PUBLIC parameters + dict outputs — MILD debt worth fixing

Inside a body, `ax` is a plain Python local; renaming it is an alpha-rename
with zero semantic content (see 2e).  At the **contract surface** it is
different: `def _abi_core(mem, *, ax=0, bx=0, ds=0, si=0)` and
`_o["ax"]` say *the caller must still know register identity*.  The
information content is already narrowed and correct — this is labelling —
but fixing it converts a convention into a **machine-checkable property**:

```
no public recovered parameter is named after a register   ← greppable
no call site indexes a result by register name            ← greppable
```

That is precisely the kind of hard-wall criterion the phase needs, and it
costs one deterministic rename driven by the contract the census already
computes.

**Verdict: eliminate before M4** (as anonymous positional/typed params and
positional results).

### 2c. Eager flag computation — MODERATE debt (noise M4 must read past)

Today every arithmetic site computes every historical flag.  In the real
core below, `pf`/`af`/`of` are computed after operations whose flags are
immediately overwritten and never read.  `dos_re.lift.analyze.dead_flag_sites`
— a seam-conservative backward flag liveness — **already exists** and is not
yet wired into the ABI emitter.

Flags that must survive: those read by in-function control flow, consumed
by a recovered callee, observed at a park/verification boundary, or live at
an exit a caller observes (the `exit_flags` contract).  Everything else is
carrier noise.

**Verdict: eliminate before M4, poison-tested** — corrupt each elided flag
and prove no observer notices, in the same spirit as dropped registers.

**MEASURED (2026-07-18), and it revises the estimate down.**  Over the 101
cores: 6437 flag-assignment lines, but `dead_flag_sites` proves only **355
of 3961 instruction sites (~9%)** are flag-dead.  The reason is structural:
the analysis is *seam-conservative* — flags are observable at every call,
INT, and exit — and composed cores are seam-dense, so a flag write is
rarely dead.  Two consequences:

* the apparent "flag noise" is mostly **load-bearing**, not carrier debris;
* eliding is provably compat-preserving (a dead write is by definition
  overwritten before every seam and exit, so both the flag word and
  `_fmask` are unchanged at every observation point) — meaning no semantic
  mask would be needed — but the yield is ~9% for a change to the SHARED
  translator that every port's mechanical emitter depends on.

**Revised disposition: low-yield, deferred.**  Not architectural debt at
the scale first assumed.  Revisit if a cheaper-to-prove liveness (e.g.
per-flag rather than per-site) raises the yield materially.

### 2d. `_fmask` and exact flag serialization — NOT debt; must remain

These are compatibility metadata and they are **already correctly
quarantined** in the `_c` channel, never in the public contract.  Exact
flag reproduction is what keeps the oracle differential meaningful.  Keep
them, keep them private.

### 2e. The `bb` dispatch loop — NOT debt for M4; defer structuring

This is the one that *looks* like the worst offender and is not, for a
decisive reason:

> **M4's analyses run on the Recovery IR and the recovered contracts, never
> by parsing generated Python** (dos_re_2.0.md §1b, the ironclad rule).

Address expressions, region clustering, stride recovery, and access-site
provenance all come from re-elaborating pinned instruction bytes — the
emitted control-flow shape is irrelevant to them.  Structured Python
therefore buys M4 nothing, while forcing structure on irreducible or
multi-entry regions risks correctness in a phase whose entire value is
byte-exactness.

Registers-as-body-locals fall in the same bucket: an alpha-rename with no
structural content.

**Verdict: may remain as a deterministic emission detail.**  Optionally,
later, emit structured Python for *provably reducible* regions only and keep
dispatch form elsewhere — a readability improvement that belongs after M4
(or in M5), never a prerequisite.

## 3. The three-way naming distinction

The owner's axis 1 asks for this boundary; it is worth stating sharply:

| Layer | Produces | Owner |
|---|---|---|
| **machine-register ABI recovery** | narrowed contracts still labelled `ax`, `si` | M3b (done) |
| **anonymous semantic ABI recovery** | `arg_0`, `FarPointer(seg,off)`, positional results — identity and *role* without meaning | M3b (slice 6) |
| **human semantic naming** | `game.enemies[i].x`, `format_decimal` | M5, AI, only after M4 proves structure |

Anonymous recovery is deterministic and provable; human naming is not.
Keeping them in separate phases is what makes the AI step safe.

## 4. Pointer contracts: the M3b/M4 boundary

The sharpest question, and the cleanest answer:

* **YES at the contract surface.**  M3b should normalise proven `(segment,
  register)` pairs into an explicit `FarPointer`-style value type.  The
  evidence already exists — the contract census records pointer-pair joint-EA
  use per function (122 functions in this corpus).  Handing M4 *"this
  function takes a far pointer and an index"* instead of *"it takes `ds`,
  `si`, and `bx`"* gives the region-clustering pass its symbolic roots.
* **NO in the body.**  Rewriting `mem.rb(ds, di + 18)` into a field access
  *is* region inference — M4's defining job.  M3b leaves raw historical
  accesses present and fully traceable.

What M3b should recover: scalars, near pointers, far pointers, array
indices, constant segment bindings, semantically-used return addresses,
typed stack views, and **opaque pointer values whose target region is not
yet known** (refusing to classify is a valid, recorded outcome).

## 5. Stack end state: eliminated vs. genuinely semantic

Two outcomes, and the phase must say which applies to every function:

```
compiler stack carrier ELIMINATED      → no stack object in the body at all
stack has REAL program semantics       → explicit typed stack view in the contract
```

The second class is not a failure; it is a finding.  It covers exactly:
functions that read their own return address (15 in this corpus), functions
whose frame address escapes, park/resume boundaries needing exact frame
shape (9), alternate-entry functions (11), interrupt-visible frames (3
iret), and functions using stack memory as semantic data (59
`stack-addressed-memory`).  Each must get a **generated representation**,
not a refusal note.

## 6. The disposition table

> **REVISED 2026-07-18, after the overarching goal was stated**
> (dos_re_2.0.md, "The north star").  This table originally classified each
> item by ONE test: *does M4 have to undo it?*  That test is necessary but
> NOT sufficient.  The programme's actual measure is whether the recovered
> code is an ideal working representation for AI — explicit enough that
> substantial changes are quick and safe.
>
> Those are DIFFERENT tests, and three verdicts below were argued on the
> first while reading as if they settled the second:
>
> * **the `bb` dispatch loop** — my argument ("M4 analyses read the IR, not
>   emitted Python, so structuring buys M4 nothing") is correct and still
>   stands *for M4*.  But a `while True: if bb == 3:` body is exactly the
>   machine plumbing the goal says must not remain.  It may persist through
>   M4; it must not persist at the end.  Structured control flow for
>   provably reducible regions is a DELIVERABLE, not optional polish.
> * **register-named body locals** — mechanically a pure alpha-rename, so
>   "no structural content" is true.  Against the goal it is false: `ax`
>   and `si` are the machine's vocabulary, and every one of them is a thing
>   AI must decode rather than read.
> * **per-operation flag computation** — I measured only ~9% *provably*
>   dead under seam-conservatism and deferred on yield.  That measurement
>   stands, but 6437 flag-assignment lines is enormous noise in the final
>   representation, so the right response is not "defer" but "find a
>   better-yielding formulation" (e.g. render a comparison consumed only by
>   an adjacent jcc as a plain boolean, rather than trying to prove
>   individual flag writes dead).
>
> The column below therefore distinguishes **may remain THROUGH M4** from
> **may remain AT THE END**.  Nothing in the first column is settled; it is
> debt with a deadline.

| Concept | Disposition |
|---|---|
| CPU object, interpreter path | **must be eliminated** (already: zero) |
| machine call/ret, ret-addr writes, sp adjust | **must be eliminated** (done for cores) |
| `ss`/`sp` as public parameters | **must be eliminated** (done for cores) |
| generic `_vs` virtual stack | **must be eliminated** → slot locals (slice 6) |
| register-named public params, dict-keyed results | **must be eliminated** → anonymous/positional (slice 6) |
| unproven dropped outputs | **must be eliminated** (already: poison-proven) |
| register-named *body locals* | may remain THROUGH M4 (alpha-rename) — **must not remain at the end**: machine vocabulary AI has to decode |
| `bb` dispatch loop / CFG-shaped body | may remain THROUGH M4 (analyses read the IR) — **must not remain at the end**: structured flow for provably reducible regions is a deliverable |
| dead flag computation | attempted in slice 6 and poison-tested, but only ~9% proved dead — **deferred on yield, NOT accepted**: 6437 flag lines is noise in the final form, so this needs a better formulation before the end state |
| 8/16-bit partial-register ops | may remain — faithful width semantics (a real property of the program, not plumbing) |
| `_fmask`, exact flag word, virtual cost | **must remain**, private compat channel only |
| typed stack view for genuinely-stack-semantic functions | **must remain**, explicitly classified |
| raw `mem.rb/rw/ww` with historical addresses | **must remain** — this is precisely M4's input |
| address→region clustering, stride/field recovery, dataclasses, ownership, bridge | **M4** |
| structured control flow, human names, domain models | **M5 / AI**, after M4 proof |

## 7. Worked example: `1010:4B68` (a 3-byte decimal buffer formatter)

**(a) mechanical CPUless** — the CPU carrier in the contract:

```python
def func_1010_4b68(mem, *, ax=0, bx=0, ds=0, si=0, sp=0, ss=0):
    ...  # push/pop into mem.ww(ss, sp, ...); returns a 9-register dict + compat
```

**(b) current ABI core (today, slice 2–5)** — stack and CPU gone, shape still
assembler:

```python
def _abi_core(mem, *, ax=0, bx=0, ds=0, si=0):
    _vs = []                                   # scaffold, unused here
    bb = 0
    while True:
        if bb == 0:
            mem.wb(ds, ((si) & 0xFFFF), 0x20)
            mem.wb(ds, ((si + 1) & 0xFFFF), 0x20)
            mem.wb(ds, ((si + 2) & 0xFFFF), 0x20)
            _t = ((ax >> 8) & 0xFF) ^ ((ax >> 8) & 0xFF)
            zf = ...; sf = ...; pf = _PARITY[...]; cf = of = False   # all dead
            ...
    return {'ax': ..., 'bx': ..., 'si': ...}, {'flags':…, 'fmask':…, 'cost':…}
```

**(c) target M3b end state (slice 6)** — anonymous, pointer-typed, live flags
only, no stack object; raw memory accesses deliberately retained:

```python
def core_1010_4b68(mem, buf: FarPointer, arg_0: int, arg_1: int):
    """[1010:4B68] -> (v0, v1, v2)"""
    mem.wb(buf.segment, (buf.offset) & 0xFFFF, 0x20)
    mem.wb(buf.segment, (buf.offset + 1) & 0xFFFF, 0x20)
    mem.wb(buf.segment, (buf.offset + 2) & 0xFFFF, 0x20)
    arg_0 = arg_0 & 0x00FF                     # xor ah,ah — no flags live
    ...
    return (arg_0, arg_1, buf.offset), _compat  # positional, contract-shaped
```

**(d) M4 memoryless form** — the buffer becomes a typed region; the address
expression is gone, the logic is untouched:

```python
def core_1010_4b68(out: DigitBuffer, arg_0: int, arg_1: int):
    out[0] = 0x20
    out[1] = 0x20
    out[2] = 0x20
    ...
```

The step (c)→(d) is *only* memory recovery — which is the point: M4 should
inherit no CPU-recovery work.

## 8. Fail-loud acceptance gate for "M3b complete"

Machine-checkable, reported as counts that must all be zero:

```
CPU object imports in recovered/ABI modules ........ 0
interpreter-reachable paths ........................ 0
public parameters named after a register ........... 0
public ss/sp parameters ............................ 0
historical stack reads/writes inside cores ......... 0
mechanical return-address writes ................... 0
full register-bundle call sites .................... 0
result values indexed by register name ............. 0
generic virtual-stack objects in emitted cores ..... 0
dead flag computations (unproven) .................. 0
unproven dropped register outputs .................. 0
unproven elided flag outputs ....................... 0
functions with no core AND no exception class ...... 0
unresolved recovered call contracts ................ 0
poison-shadow divergences .......................... 0
oracle boundary divergences ........................ 0
```

Plus a **classified exception report** — each class with a count and a
*generated representation*, not merely a name:

```
return-address-semantic functions   → explicit return-address value contract
stack-semantic functions            → typed stack view
park-frame-preserving functions     → exact frame contract
alternate-entry functions           → multi-entry contract
interrupt-frame (iret) functions    → interrupt-frame contract
opaque pointer contracts            → opaque pointer value type
irreducible-CFG functions           → dispatch-form body (permitted)
```

M3b is complete when every function in the required runtime closure is
either a de-stacked ABI core or a member of a named class **with a
generated representation**, and every counter above is zero.

## 9. Verification strategy (unchanged in spirit, extended in reach)

Static analysis proposes; the differential promotes.  Each new elimination
gets its own poison:

| Eliminated | Poison test |
|---|---|
| register outputs | XOR the dropped output (done — 964/964 green) |
| flag outputs | invert the elided flag before every observer |
| stack slots | randomize eliminated slot values |
| return-address writes | poison the historical ret-addr location |
| mechanical fallback | block the mechanical core from being importable |

Where the new representation *intentionally* differs from the mechanical one
(the stack region), use a **semantic mask** — the shadow-stack overlay
already in `abi_diff` — never a broadly weakened comparison.

## 9a. Parked: the stack/data-shared tier, and a verifier-cost wall

The last `stack-addressed-memory` functions use an explicit `ss:` override
for DATA *and* have a real machine stack, so ONE segment carries both.  In
the ABI form the ambiguity vanishes (push/pop become slot locals, calls
write no return address), leaving `ss` used for data only — so the
transformation is straightforward and reaches **113 → 149 cores** with the
static wall still green.

**It is parked on branch `abi-ss-stack-split`, unverified**, for a reason
worth recording: the verification is not tractable as built.  The mechanical
reference writes stack and data through the SAME segment, so neither a
segment shadow nor an assumed offset window separates them.  The
discriminator therefore MEASURES the split — drive the mechanical side twice
with different initial `sp`; writes that move with `sp` are stack, writes
identical under both are data — guarded so that a differing outcome, a
differing result, or an excluded write outside the stack segment each fail
loudly rather than silently splitting.

That design is sound but doubles the mechanical work on exactly the
loop-heavy functions (a `rep` with a seeded 16-bit `cx` runs up to 65535
iterations).  The full run reached **50 GB RSS without finishing**; one
function needed 84 s for 3 states.  Shipping the 36 new cores unproven would
be precisely the wrong trade, since they are the stack/data-shared cases —
the ones most likely to expose a flaw in the discriminator itself.

**What it needs before landing:** a tractable verifier — bound the per-state
write trace and iteration budget (reporting, never silently skipping), seed
loop counters narrowly, or decide `sp`-dependence STATICALLY from the depth
map instead of by re-execution.  The static route is most promising: the
depth map already proves every stack slot's offset, so stack writes are
identifiable without running anything twice.

**Lesson for M4:** the memory-schema phase will face this same question as
*region ownership*, and it will face it at whole-program scale.  A
verification strategy that costs 2× execution on loop-heavy code does not
survive that.  Prefer static ownership proofs, with re-execution reserved
for spot-checking.

## 10. Remaining slice sequence

Do the representation tightening **now, at 101 cores**, not later at 250 —
it changes the emitter shape, so every subsequent tier is born correct.

```
slice 6  representation tightening (THE ARCHITECTURE FIX)
         _vs -> slot locals; anonymous params + positional results;
         FarPointer contracts; flag-liveness elision + flag poison test
slice 7  typed stack views: stack-addressed-memory 59, ret-addr-touch 15,
         ss-value 1  — the "stack is semantic" class
slice 8  far-call + indirect-call composition (14 + 3)
slice 9  game-vectored INT + iret handlers (5 + 3) via the _ivec dispatch
slice 10 alt-entry 11 + observer 9 — likely permanent classes with an
         explicit generated frame contract
slice 11 flags-word-stack (pushf/popf) 7
slice 12 acceptance gate + full-corpus poison proof on the canonical demo
```

Then, and only then, M4 (`docs/memory_schema.md`).
