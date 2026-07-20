# Dead code in dos_re: reachability is under-approximated, not over-emitted

**Historical conclusion.** In the evaluated SkyRoads recovery corpus,
reachability was UNDER-approximated far more often than
code is over-emitted. The lifter does not decode a whole binary and prune it; it
emits an *evidence-gated entry set*, and within each function decodes only the
CFG-reachable instructions. So the emitted program is already close to minimal by
construction. The real and recurring risk is the opposite of dead code: a
*missing* root or dynamic edge, which surfaces as a VMless wall violation on a
live function nobody declared.

Therefore: **reachability machinery must first prove the root set COMPLETE.
Pruning is not safe merely because a function is absent from the static near-call
closure or from replay coverage.** A function the static graph cannot reach is
almost always reached by a dynamic edge the static graph cannot see.

## The evidence (skyroads, 179 IR functions)

A naive static near-call closure from the boot entry `1010:61F3` reaches 148
functions and flags **31 as "unreachable."** Of those 31:

- **23** are render-dispatch and music-table targets — reached by indirect call
  through a runtime-built jump table;
- **3** are IVT-vectored ISRs (`3B17` timer, `3BCC` keyboard, and the music ISR
  reached from the timer chain) — entered by hardware, never by a near call;
- the remaining **5** are reached from those ISRs.

Deleting the "unreachable 31" would delete the code that makes the game run. Every
one was added to the census this session *because the wall fired on it* — the
completeness problem, not a deadness problem.

With the COMPLETE assembled root set (`scripts/reachability_audit.py`), all 179
are accounted for:

```
IR functions ................................ 179
Reachable by static call edges .............. 150
Retained by dynamic dispatch evidence ....... 25
Retained as IVT / root entry ................ 3
Retained as scheduler resume (head/snapshot)  1
NOT reached by assembled roots (EXPLAIN) .... 0
Unresolved indirect edge sites .............. 37
```

`NOT reached = 0`: there is no dead code to remove. The 37 unresolved indirect
sites are exactly where the static graph cannot follow — which is *why* the
dynamic-evidence roots are load-bearing.

## What the existing pipeline already does

- **Unreachable basic blocks (category 2):** eliminated by construction.
  `cfg.scan_function` follows control flow from the entry, so unreachable-in-region
  bytes are never decoded. Nothing to prune.
- **Unreachable whole functions (category 1):** the emitted set is the
  evidence-gated census entry set (executed call targets + INT/IVT + declared
  extras), not "decode everything." No over-approximation to prune — and a
  static-closure prune would be unsound (see the evidence above).
- **Fail-loud on a removed/undeclared path:** already built. Poison zeroes lifted
  instruction bytes; the VMless **wall** (`interp_forbidden`) raises on execution
  with no hook; `UnknownDispatchTarget` raises on an unrecovered indirect selector;
  `interp_frontier` collect-mode enumerates the whole uncovered frontier in one run.
- **Dead flags (category 3):** `--drop-dead-flags` (VMless, `analyze.dead_flag_sites`)
  and `_fmask` (CPUless) already elide provably-unobservable flag writes.

## Dead register-output pruning (this slice)

`AbiReport.exit_live` (new, `lift/cpuless.py`) is the register set live at ≥1 clean
return exit, from the same backward fixpoint as `inputs`; `None` when a tail
transfer governs live-out. `emit_cpuless._output_set` intersects the adapter's
writeback with it — applied identically to the recovered return dict and the
adapter, so they cannot drift.

**Result: 0 register outputs removed, across all 176 liftable functions.** This is
correct, not a bug: `abi_scan` deliberately seeds *every* may-written register live
at exit, because the whole-register-file boundary differential must match. So
`exit_live == outputs − {sp}` for every clean-return function, and the intersection
is the identity — proven byte-identical to the pre-slice output (0 differing
generated files) and deterministic across regenerations.

**The abi_scan weakness this exposes:** exit-liveness is *externally* conservative
(all may-writes seeded live), so there is no *intra*-procedural dead output. A
genuinely narrower dead-output set requires **inter-procedural** exit liveness —
the union, over a function's call sites, of the caller's live-in-after-the-call —
which needs the complete call graph and root set. That is the same completeness
machinery the audit is about. `exit_live` is a first-class field so that future
analysis can narrow it without touching the emitter. Until then the slice's value
is a proof: the emitted output set is already minimal.

## The scattered root sources, and a proposed `roots.json`

The complete root set is currently assembled from five places (this scatter is the
weakness, not the values):

| source | today | meaning |
|---|---|---|
| canonical entry | `--extra` / build_boot_image | the boot far-jump target |
| IVT handlers | `observed.json` `ivt_game_vectors` | hardware-entered ISRs |
| dynamic dispatch | `artifacts/codemap/dispatch_extra.txt` | indirect-call targets (bounded/derived) |
| boundary heads | `artifacts/codemap/boundary_heads.txt` | scheduler resume points |
| snapshot entries | `artifacts/codemap/snapshot_entries.txt` | resumed-into addresses |

No single artifact answers "what are all the runtime roots?" — so no closure can be
*trusted* to be complete, which is the precondition for any sound pruning. This
slice does **not** introduce `roots.json` (out of scope), but proposes it as the
consolidation:

```json
{
  "schema": "dos_re/roots@1",
  "code_seg": "1010",
  "roots": [
    {"addr": "1010:61F3", "source": "canonical",  "evidence": "boot far-jump target"},
    {"addr": "1010:3B17", "source": "ivt",         "evidence": "observed IVT vector 08h"},
    {"addr": "1010:34A7", "source": "dynamic",     "evidence": "dispatch table ds:0E38 [0]"},
    {"addr": "1010:434A", "source": "boundary",    "evidence": "tick-wait head; scheduler resume"},
    {"addr": "1010:22F8", "source": "snapshot",    "evidence": "replay snapshot start CS:IP"}
  ]
}
```

Each root carries its **source** and **evidence** so the set is auditable, and the
closure/audit/poison stages consume one file instead of five. Building it is the
prerequisite for trusting any future function-level pruning — and for the
inter-procedural exit liveness that would make dead-output pruning bite.

## Recommendation

Do not add a static reachability-based function/block pruner: the emitted set is
already evidence-minimal, and this game's dynamic control flow makes near-call
closure unsound. The valuable work is (1) the dead-computation passes inside
`emit_cpuless` (flags done; register outputs proven already-minimal here; dead
stores need an alias model), and (2) consolidating the roots so the audit can
*prove* completeness — the risk that actually bites.
