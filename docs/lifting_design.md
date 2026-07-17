# Automatic literal lifting: ASM function → Python hook → oracle → refactor

> **DOS_RE 2.0 supersession note (2026-07-17).**  This document designed the
> lifter under the 1.x risk model: lift one function, prove it ORACLE_PASSING,
> only then trust it.  Under DOS_RE 2.0 ([`dos_re_2.0.md`](dos_re_2.0.md), the
> canonical architecture) that per-function gate applies ONLY to the hybrid
> auto-install tier.  Graph assembly is different: the largest supported
> **VMless lifted graph** is emitted (`tools/liftemit.py`), structurally linked
> (`tools/liftlink.py`), installed whole (`lift.install.install_vmless_graph`),
> and judged by END-TO-END oracle comparison with auto-bisection
> (`tools/hook_bisect.py`) — per-function ORACLE_PASSING is metadata, not a
> precondition.  Wherever this document says or implies "proven before
> linked/installed", read it as describing the hybrid tier or as historical.
> The M0–M4 roadmap in §10 is the 1.x lifter roadmap; the project-level
> milestones are now M1–M6 in `dos_re_2.0.md` §6.  Terminology: what this doc
> calls the eventual "native" artifact is, in 2.0 vocabulary, the **VMless
> lifted runtime** — the CPU carrier and DOS memory model are removed by later
> stages (CPUless emitter, DOS-layout dissolution), not by this lifter.

> **Two ISA pipelines.** This document describes the design in its original
> 16-bit terms; the 32-bit flat (DOS/4GW / CPU386) counterpart mirrors it
> module for module — `decode32`/`cfg32`/`emit32`/`runtime32`, verified by
> `pm_verification.PMHookVerifier`, driven by `tools/pmlift.py`. Design
> deltas: x87 lines are interp-fallbacks (not refusals), and hooks key on
> flat linear EIP instead of CS:IP.

> Status: **M0–M3 LANDED** (2026-07-10). Decoder + CFG (M0, §10a), emitter
> (M1, §10b), in-situ verify pipeline + proof ledger (M2, §10c), and the
> full-loop proof on a real game (M3, §10d). `liftverify` emits a literal
> hook per entry and differentially verifies each against the interpreted
> original; a passing lift is then refactored into clean recovered source
> with the same oracle. Proven end to end: skyroads_port's first island (its
> master timer ISR) was recovered this way. The lifter is the optional
> accelerator a porting agent reaches for (wired into the port workflow's
> checklist + cookbook, and adopted as an overkill_port workflow invariant).

## 0. The idea, in this ecosystem's terms

Today the recovery loop's expensive step is manual: the AI reads a routine's
ASM and hand-translates it into a Python hook, then the differential oracle
keeps it honest. This proposal adds a tool that does the first translation
mechanically:

```
original ASM function (entry CS:IP, live snapshot bytes)
  → dos_re.lift: literal, ugly, per-instruction Python hook  (automatic)
  → differential oracle verification                          (existing machinery)
  → installed as a replacement island                         (existing machinery)
  → LATER: the agent refactors it into real recovered Python  (manual, the actual goal)
  → the SAME oracle tests keep the refactor honest            (existing machinery)
```

The lifter is **not a decompiler** and must never try to be one. It produces
scaffolding: a faithful, verified, *refactorable* artifact, so the AI's job
shifts from "translate 200 instructions without a mistake" to "simplify code
that is already proven equivalent". Understanding still comes from the
refactor step — a lifted island is a *liability ledger entry*, not recovered
source (§8 keeps the metrics honest about that).

## 1. First-principles anchors (what makes this tractable HERE)

Three properties of this ecosystem remove the classic lifter risks:

1. **The oracle IS the interpreter.** Correctness for a hook is defined as
   "byte-identical to what `cpu.py` would have done", not "identical to a
   real 8086". So the lifter does not need its own semantic model of x86 —
   the target semantics are exactly the interpreter's. The right mental model
   is **ahead-of-time specialization of the interpreter**: for each concrete
   instruction, emit the same operations `step()` would perform, using the
   same helpers (`set_sub_flags`, `mem.rb/rw/wb/ww`, `push/pop`,
   `dos_re.asm`'s REP fast paths). Divergence surface shrinks to translation
   bugs, which the existing verifier is specifically built to catch.
2. **`dos_re.asm` already exists for exactly this.** Its docstring: "shared
   8086-style arithmetic and string helpers for *lifted routines*". Hand
   lifting has been the practice all along; this proposal industrializes it.
   The emitter's runtime library is `asm.py`, grown deliberately.
3. **Hooks already compose through the VM.** A lifted function that hits
   `call 0x1234` does not need the callee lifted: it emulates the call
   *through the interpreter*, and if the callee later gets its own hook, the
   interpreter's dispatch runs it automatically. No whole-program lifting,
   no link step, no lifting order constraints. Every function is independently
   liftable, verifiable, and revertible.

## 2. Non-goals

- No structured control-flow recovery (if/while reconstruction), no variable
  naming, no type inference — that is the refactor step's job, done by AI on
  a verified artifact.
- No performance target. Literal lifts will run maybe 2–5x faster than
  interpretation on CPython (no fetch/decode; same helper costs) and that is
  incidental. The value is recovery throughput, not speed.
- No lifting from EXE files. Input is always a **live runtime or snapshot**
  (post-bootstrap, post-relocation, overlays resolved) — the same evidence
  source everything else uses.

## 3. Where it lives

**`dos_re/lift/` subpackage now; extraction later if win16_re wants it.**

- The emitter targets dos_re's runtime API (`cpu.s`, `cpu.mem`, flag
  helpers) — it cannot be meaningfully independent of dos_re today, and
  win16_re does not exist yet. Creating a third shared repo now would violate
  the ecosystem's own promote-on-second-consumer rule.
- BUT the internal layering is enforced from day one, so extraction is a
  `git mv`: `lift/decode.py` + `lift/cfg.py` are pure x86-16 (import nothing
  from `dos.py`/`interrupts.py`; lint-enforced the same way the core/frontend
  ring is). OS-specific behaviour enters only through a **boundary policy**
  object (§6). Precedent: `runtime_code.py` and `player.py` both landed in
  dos_re with internal discipline and clean extraction seams.
- Win16 reality check: Win16 shares the CPU (cpu.py already grew 286
  selectors), and a win16_re would almost certainly reuse the whole VM +
  verifier + lifter stack, not just the lifter — so the real shared unit is
  bigger than this tool, and that split is win16_re's bootstrap decision,
  not this proposal's.

Layer map:

```
dos_re/lift/decode.py    x86-16 table decoder → Instruction records   (OS-free)
dos_re/lift/cfg.py       region discovery, basic blocks, exits        (OS-free)
dos_re/lift/emit.py      Instruction → Python source lines            (targets cpu/mem API)
dos_re/lift/runtime.py   emulate_call / emulate_int / bail helpers    (+ asm.py grows)
dos_re/lift/policy.py    BoundaryPolicy protocol + DOSBoundaryPolicy  (Win16Policy later)
tools/liftgen.py         CLI: snapshot + CS:IP → artifact + report
```

## 4. The decoder: new code, but self-verifying against the interpreter

There is no reusable decoder today — `lindis` "decodes" by single-stepping a
throwaway runtime and observing bytes consumed. The lifter needs static
decode (a jcc has two successors; execution takes one). So `lift/decode.py`
is a new table-driven decoder producing structured records
(mnemonic, operands, length, branch kind + static targets), **cross-checked
per region against the interpreter**: for every decoded instruction, single-
step a throwaway CPU at that address (the lindis trick) and require the
consumed byte count to match. Any disagreement = refuse the lift, loudly.
The decoder therefore never needs to be trusted alone — it has the same
oracle discipline as everything else. (Deliberately NOT capstone: a new
non-stdlib core dependency for semantics we already own, and its decode
quirks would not match cpu.py's, which is the only authority that matters.)

## 5. The generated hook: anatomy

Register/flag state stays **architectural at every instruction boundary** —
the generated code reads and writes `cpu.s` / `cpu.mem` directly, exactly
like the interpreter (no local register caching in v1). That buys: bail-out
anywhere with coherent state, watcher/aperture/ROM semantics identical by
construction (all memory access through `mem.rb/rw/wb/ww` — never raw
`mem.data`), and trivially reviewable emission. Python has no goto, so the
CFG is executed by the standard dispatch-loop pattern:

```python
# AUTOGENERATED by dos_re.lift v<N> — literal lift. Refactor freely; the
# oracle tests are the contract. Regenerating overwrites this file.
# region 1010:4537..45E0 (169 bytes) sha1=..., exits: near_ret
REGION = (0x1010, 0x4537, bytes.fromhex("2e8b1e9c5b..."))

@oracle_link(boundary="1010:4537", contract="...", status="LIFTED")
def lifted_1010_4537(cpu):
    if self_disable_if_patched(cpu, 0x4537, REGION[2], "lifted_1010_4537"):
        return                       # SMC guard: fall back to interpretation
    s, mem = cpu.s, cpu.mem
    bb = 0
    while True:
        if bb == 0:
            # 1010:4537  2E8B1E9C5B   mov bx, cs:[0x5B9C]
            s.bx = mem.rw(s.cs, 0x5B9C)
            # 1010:453C  8A04         mov al, [si]
            ...
            cpu.instruction_count += 9          # block-exact demo-clock preservation
            bb = 2 if (s.flags & ZF) else 1
        elif bb == 1:
            ...
        elif bb == 2:
            # 1010:45E0  C3           ret
            cpu.instruction_count += 1
            s.ip = cpu.pop()
            return
```

Non-negotiable properties:

- **Original disassembly as per-line comments** — this is what the refactoring
  AI reads; the artifact must be self-explanatory without re-disassembling.
- **`instruction_count` preserved block-exactly**, so pre2-style
  instruction-count clocks and demo determinism are unaffected by installing
  a literal hook (a stronger transparency guarantee than hand hooks give).
  Dropped deliberately (with re-record) only at refactor time.
- **Entry signature guard** via the existing `self_disable_if_patched` — the
  established defence for runtime-patched code.
- **Never auto-installed.** The artifact lands in `<game>/lifted/`, carries
  `status="LIFTED"`, and installation is gated on oracle status (§7).
- One file per function, deterministic output (no timestamps), so re-lifting
  after a tool upgrade diffs cleanly and git churn is reviewable.
- The lifter also emits the verifier metadata for free: its discovered exits
  map 1:1 onto the existing `GenericHookStop` kinds (`near_ret`, `far_ret`,
  `iret`, `fixed_ips` for jump-out exits).

## 6. Control flow and the outside world

| Construct | v1 treatment |
|---|---|
| fallthrough, direct jmp/jcc/loop/jcxz | lifted (dispatch loop) |
| `ret` / `ret n` / `retf` / `iret` | function exits, emitted literally |
| **direct call** | `emulate_call(cpu, cs, ip)`: push return, run the *interpreter* until it returns (step-budgeted, fail-loud). Callee hooks dispatch automatically → free composition; lifting order never matters |
| **indirect call** (`call bx`, `call [table]`) | same as direct — target resolves at runtime like the interpreter would; safe by construction |
| `int n` | via BoundaryPolicy. DOS policy: `emulate_int` through the VM (dos.py services it — already the semantics being verified against) |
| **indirect jmp** | ~~refuse at lift time (v1)~~ **since 2026-07-15: lifted as a TAIL EXIT** (parity with the 32-bit pipeline): the hook computes the runtime target, sets CS:IP, returns to the VM — a dispatcher lifts as prologue + tail transfer, its cases stay interpreted and re-enter any installed hook. First consumers: Lemmings' sound-driver dispatcher (`jmp rm16`) and an ISR chaining to the saved vector (`jmp far [old_vec]`). The same date, the region budget switched from lo..hi SPAN to DECODED BYTES (discontiguous far-tail functions are real — Lemmings `1010:3944`, 39 insts across 17KB). Full static jump-table recognition (native switch over table bytes) remains the LINK-stage item |
| in/out, string ops, EGA aperture | through the same VM paths/helpers the interpreter uses (incl. `asm.py`'s guarded REP fast paths) |
| unsupported opcode (x87, …) | refuse at lift time |

Refusals are structured (address + reason), because M0 turns them into data.
A later "runtime bail" tier (lift the common blocks, fall back to the
interpreter at a precise CS:IP for rare tails — state is always architectural
so bailing is just `s.ip = addr; return`) is possible but deliberately v2+:
whole-function-or-nothing is easier to reason about and to trust first.
The Win16 policy plugs in at the same seam later: far call to a selector
owned by KERNEL/USER/GDI = API boundary, handled by whatever win16_re's
runtime does — decoder/CFG/emitter unchanged.

## 7. Verification and the proof ladder

Primary verification is **in-situ over the demo corpus** with the existing
`HookVerifier` (clone runtime → run interpreted original to the continuation
→ run the lifted hook → diff registers + flags + full memory, dead-stack
scratch excluded). The lifted artifact additionally gets **basic-block
coverage instrumentation** (debug flag): "verified" must state *which paths*
were exercised, or it overstates.

Status ladder (extends `islands.STATUSES`):

```
LIFTED                generated; never executed as a replacement
ORACLE_PASSING        in-situ verified: N calls, 0 divergence, M/K blocks covered
INSTALLED             running as the default replacement (still guarded by entry signature)
REFACTORED            the agent rewrote into real recovered Python; SAME tests green
```

> **2.0 scope of this ladder:** these statuses gate the HYBRID auto-install
> tier (`install_passing_lifts`) and serve diagnostics/regression.  They do
> NOT gate structural linking or inclusion in the assembled VMless graph —
> that is judged end-to-end (see the supersession note at the top and
> `dos_re_2.0.md` §2).

- Promotion LIFTED → ORACLE_PASSING is done by a driver
  (`tools/liftverify.py`) that replays chosen demos with the verifier
  attached and writes results into the island manifest — the same evidence
  language the ports already speak.
- Synthetic register/memory fuzz (the `test_expand_4plane_row_4537_fuzz`
  house style) is opt-in per function with declared input ranges — random
  environments can violate preconditions and send the *oracle* into garbage,
  so it cannot be the default.
- **Metrics honesty rule:** lifted islands are counted in their own tier.
  Campaign "recovered %" continues to count REFACTORED code only. A thousand
  lifted functions is coverage of the *verification* frontier, not of the
  *understanding* frontier, and the dashboards must never blur that line.

## 8. Failure safety (fail loud, never fake)

- **Lift time**: refuse on decoder/interpreter disagreement, unsupported
  opcode, indirect jmp, region overlapping a known `runtime_code.py` slot,
  region exceeding a size budget, or CFG escaping a sanity window (ambiguous
  boundary). Every refusal is a structured record.
- **Run time**: the entry signature guard self-disables the hook if the
  region bytes changed (SMC/overlay swap) — execution falls back to the
  interpreter, and the event is counted, not silent. Mid-execution
  self-modification by the function itself is the residual risk (statically
  undecidable); it is caught by in-situ verification on real inputs and by
  `RuntimeCodeWriteTracer` when suspected — same status as hand hooks today.
- **Runtime-loaded code**: lifting from snapshots pins the bytes; the hash
  in the artifact makes staleness detectable and re-lift automatable.

## 9. Tradeoffs and risks, stated plainly

- **Equivalence is vs the VM, not vs silicon.** Same epistemic status as
  every existing hook; no regression, but worth restating: a cpu.py bug
  reproduced by the lifter verifies as "equivalent". (Guarded, as today, by
  real games working + DOSBox cross-checks.)
- **The scaffolding trap.** Mechanically lifted code *feels* like progress.
  If the refactor step lags, the ports accumulate unreadable-but-verified
  Python and the actual goal (recovered source) stalls. Mitigations: the
  metrics rule (§7), and the workflow prompt (`prompts/recover_one_routine.md`)
  being updated so the refactor step starts from a lifted artifact by default
  — the lifter exists to *feed* that step, not replace it.
- **Helper API becomes a contract.** Generated code freezes today's
  `cpu.set_sub_flags(...)`/`asm.py` signatures. Interpreter refactors must
  keep them or regenerate; the consumer-suites-green equivalence gate already
  enforces this in practice.
- **Git/lint surface**: generated files must be exempted from oversized-file
  checks but NOT from syntax/undefined-name guards; `<game>/lifted/` gets its
  own lint category.
- **PyPy interaction**: literal hooks neither help nor hurt the JIT much
  (they replace already-hot interpreter traces with equivalent Python);
  benchmark before claiming wins in either direction.

Rejected alternatives: Ghidra/RetDec-based lifting (their IR/memory model
does not map onto the `cpu.s`/`mem` hook contract; bridging faithfully is
harder than direct emission, plus a heavyweight dependency); capstone as
decoder (§4); "LLM does the literal translation" (that is the status quo —
the failure mode this proposal removes is precisely unverified hand
translation at scale).

## 10. Staged roadmap (each stage ships value alone)

- **M0 — the census (no codegen).** Decoder + CFG + refusal taxonomy;
  `tools/liftgen.py --report` over the frontier/hot lists of all four ports.
  Output: % of real functions v1-liftable, refusal histogram, size
  distribution. Cheap, evidence-first; validates or kills the scope before
  any emitter work.
- **M1 — emitter + offline proof.** Lift the v1 subset; verify lifted-vs-
  interpreted on functions that ALREADY have hand-written hooks (4537, the
  sprite blits, 08F2…) — the hand hooks' fuzz suites become free test beds,
  and disagreement with either the oracle or the hand hook is a tool bug.
- **M2 — the pipeline.** `liftverify` in-situ driver + manifest/status
  integration + block coverage; first real batch on skyroads_port (youngest
  port, zero hooks, hot LZS/frame candidates already profiled).
- **M3 — the refactor loop, proven end-to-end.** Take 2–3 ORACLE_PASSING
  lifted islands, have the AI refactor them to clean Python with tests
  unchanged, land as REFACTORED. Update the framework method docs
  + prompts. This is the milestone that proves the *actual* thesis.
- **M4+ — widen.** Jump tables; runtime-bail partial lifts; block-local
  register caching if profiling justifies; structurizer pass (source-to-
  source on verified artifacts, re-verified); Win16 boundary policy when
  win16_re exists.

## 10a. M0 census results (2026-07-10)

Ran over every REAL function entry available: all 335 registered hook
addresses in overkill_port, all 44 in pre2_port, and the documented ledger
addresses of the two young ports. Probe: every non-transfer instruction's
length cross-checked against one interpreter step() (IP delta).

| Port | entries (source) | liftable | dominant refusals |
|---|---|---|---|
| overkill_port | 335 (hook registry) | 269 (**80%**) | 52 indirect-jump, 10 unsupported, 10 no-exit, 9 region-budget |
| pre2_port | 44 (hook registry) | 44 (**100%**) | — |
| skyroads_port | 17 (symbol ledger) | 16 (94%) | 1 unsupported (likely a non-entry doc address) |
| ancient_port | 27 (docs) | 22 (81%) | 5 indirect-jump, 1 unsupported |
| **total** | **423** | **351 (83%)** | |

**Zero decoder-mismatch refusals anywhere** — the static decoder agreed with
the interpreter on every probeable instruction of every real function.
Liftable-function size: median 25 instructions, max 199 (overkill). The
refusal histogram confirms the §6 prediction: indirect jumps (dispatch
tables) are the one class that matters for coverage beyond v1 — jump-table
recognition is the highest-value M4 item. The unsupported/no-exit tail is
small and partly non-entries (doc addresses that are labels, not function
heads). Verdict: **the M1 emitter is justified.**

Notes from the field: the census probe found that the 2026-07-09 interpreter
fetch-path inlining had silently broken `tools/lindis.py`'s fetch8-counting
length trick; lindis now takes lengths from `dos_re.lift.decode` (and gained
the ability to decode non-executable bytes).

## 10b. M1 results (2026-07-10) — the emitter

`dos_re/lift/emit.py` turns a `FunctionScan` into a self-contained Python
module defining one hook, exactly as §5 specified: architectural state at
every instruction boundary, a basic-block dispatch loop, per-line
address/bytes/mnemonic comments, the fail-loud SMC entry guard, and
`--count-instructions` for demo-clock transparency. `lift/runtime.py`
provides the VM-delegation primitives (`emulate_call` / `emulate_far_call` /
`emulate_int` — callees and ISRs run through the interpreter, so hooks
compose and lifting order never matters) and `interp_one` (the exact
single-instruction fallback for opcodes with no native form yet).

**Faithfulness by reuse, not reimplementation.** Emitted code never
re-derives semantics: ALU/flags call `cpu.set_add_flags`/`set_sub_flags`/
`set_logic_flags`/`set_incdec_flags`, shifts call `cpu.shift`, string ops
call the interpreter's IP-independent `cpu.string_op` (DF/REP/segment-override
and the bulk MOVS/STOS fast path included), and all memory goes through
`mem.rb/rw/wb/ww`. So a lifted function is byte-exact against the interpreter
by construction — the only divergence surface is translation bugs, which the
differential oracle catches.

Measured over all 269 v1-liftable overkill functions (`liftgen --emit`):

| Metric | Result |
|---|---|
| modules generated | 269 |
| syntax errors | **0** |
| instructions emitted | 10,253 |
| **native** (real per-instruction Python) | **95.4%** |
| interpreter-fallback | 4.6% (mostly `cli/sti/cld/std/cmc`, `mul/div/neg/not`, `in/out`) |

**The load-bearing proof:** the emitted `lifted_1010_4537` — a real 43-
instruction, 3-block game function — passes overkill's own
`test_expand_4plane_row_4537_fuzz` harness **300/300 byte-exact** (registers
+ flags + full memory) against the interpreted ASM oracle, with no
hand-editing. (It is also correct out of the box on the ZF/OF flag tail that
the *hand-written* hook got wrong until it was fixed earlier the same day —
evidence for the thesis that mechanical lifting removes a class of human
translation error.)

29 new differential tests (`tests/test_lift_emit.py`): each hand-assembles a
function, lifts it, and diffs lifted-vs-interpreted over randomized states —
covering the ALU/mov/inc-dec/push-pop/xchg/lea/shift/test/string families,
every control-flow shape (jcc, loop*, jcxz, diamonds, ret imm), call
composition with an installed callee hook, the SMC guard, and the
instruction-count option.

Deferred to keep M1 scoped: growing the native set to cover the 4.6% tail
(flag ops and mul/div are easy next adds), block-local register caching, and
a structurizer. None are needed for the thesis — the fallback keeps every
function total and exact today.

## 10c. M2 results (2026-07-10) — the verify pipeline

`tools/liftverify.py` closes the loop from *emitted* to *trusted*, and is the
form a porting agent actually uses: point it at a snapshot (a moment where the
target runs) and a set of entries, and it emits each lifted hook, installs
them all, runs the VM forward, and — every time a lifted function executes —
interprets the ORIGINAL ASM from the same pre-state to the hook's own
continuation and diffs the full machine state. It uses the framework's strict
**auto-continuation** verifier, so there is no hand-written stop metadata and
no game-specific harness — any snapshot works.

Design points that made it practical:

- **Per-hook sampling (`--samples`, default 20).** Each verification clones
  the runtime twice and re-runs the ASM oracle, so verifying every call of a
  hot function would crawl. The driver verifies each function N times, then
  retires it from verification (leaving it *running*) by pruning it from the
  verify set between step chunks — so one hot function never starves the
  others' sample budget. The sample is what proves the hook; block coverage
  (below) reports how much of it the sample exercised.
- **Block coverage (`emit(..., coverage=True)`).** Each generated hook records
  which basic blocks executed (`BLOCKS_SEEN`), so a pass reports `M/K blocks`
  and is flagged `PARTIAL COVERAGE` when the sample didn't reach every path —
  "verified" never overstates (the §7 honesty rule, enforced in output).
- **Coverage as the reached-signal.** `verified==0 but blocks_covered>0` means
  the function ran but wasn't sampled (raise `--steps`/`--samples`); only
  `blocks_covered==0` is a true `NOT_REACHED`. No function is silently
  mislabelled.
- **Stuck detection — fail loud, never hang, and say WHY.** A lifted function
  runs SYNCHRONOUSLY to completion; unlike the interpreter, no external I/O or
  timing advances between its blocks. So a loop that waits on hardware state (a
  retrace/timer poll) would spin forever in the generated dispatch loop.

  The detector is a *checkable claim*, not a magic number: a spin returns to the
  same dispatch block with **identical registers**, which is provably no
  progress. Emitted bodies sample `(bb, regs)` every 64K dispatches
  (`PROGRESS_SAMPLE`) and raise `LiftStuck` on a repeat — caught in ~64K, with
  the address (`BLOCK_ADDRS`), the machine state, whether it is provably stuck
  or merely long, and the fixes in likelihood order (**declare a boundary head**
  first — usually right; then *is the host delivering the IRQ the loop waits
  for?*; then *is it just a long loop?*). `IF=0` is called out explicitly: a
  wait loop with interrupts disabled can never be released by any ISR.

  A loop whose registers advance — every honest long loop — is **never**
  reported. That false-positive half is what lets `MAX_ITERATIONS` relax to a
  pure backstop (100M) for the pathological rest (state changes, never
  terminates). It used to be `len(insts) * 5_000`, which bounds nothing: a
  loop's trip count has no relation to its instruction count. A 27-instruction
  LZS decompressor got 135,000 and legitimately needs millions, so ports "fixed"
  it by raising a magic number until the number stopped mattering — which is how
  a guard trains people to ignore it.

  `liftverify` catches the raise, retires just that hook, and keeps verifying
  the rest. Confirmed live twice: overkill's `0679` timer-wait failed loud while
  `0162` still verified byte-exact in the same run; and SkyRoads' undiagnosed
  stall reported `lifted_1010_3a96 ... at 1010:3ABC` + "registers WERE still
  changing … may be a genuinely long loop" — correctly fingering the ANIM
  decompressor's guard rather than a phantom env-wait.

  (An env-wait loop is *not* automatically hand-hook territory any more —
  declaring it a boundary head lets the lifted body park and resume; see §
  boundary heads.)
- **The ledger is separate.** Results land in `dos_re.lift.manifest`
  (`LIFTED → ORACLE_PASSING → INSTALLED → REFACTORED`), whose statuses are
  asserted *disjoint* from `islands.STATUSES` by a test — a lift cannot be
  counted as recovered source.

Smoke over real overkill functions from a live snapshot (6 entries, 400k
steps): every entry emitted valid installable Python; the reached function
(`1010:0162`, 69 instructions / 33 blocks, 97% native) verified **byte-exact
against the interpreted original**, and the five that don't run from that
snapshot state were reported `NOT_REACHED` — no false pass. The manifest
round-trips as readable JSON. `tests/test_lift_manifest.py` adds ledger
round-trip, the disjoint-status invariant, and accumulating block-coverage
tests; the lift suite is now 77 cases.

Discoverability: the getting-started workflow now teaches the tool — a new "Automatic
lifting" cookbook entry (problem-indexed) and an "optional accelerator" block
in the porting checklist's lifting-loop step, both stressing that a lift is
recovered *only after* an AI refactors it and tags `@oracle_link` (M3).

## 10d. M3 results (2026-07-10) — the full loop, on a real game

The thesis — *ASM function → auto-lift → oracle-verify → refactor to clean
recovered source, oracle unchanged* — proven end to end by recovering
**skyroads_port's first island**, its master timer ISR (`1010:3B17`, the
game's INT 08h clock + music-tempo driver):

1. **Reach + verify.** A plain forward run never fires a timer ISR, so
   `liftverify` gained `--timer-irqs` (deliver N INT 08h per frame — mirror the
   game's frontend). With it, the lifted ISR verified **199 in-situ calls
   byte-exact** against the interpreted original.
2. **Refactor into the port's real architecture.** The mechanical lift became
   the port's pure-rule + thin-adapter split:
   `skyroads/recovered/timer_isr.py::advance_music_timer` (VM-free — the
   prescaler/song/PIT-divisor decision, `@oracle_link ASM_MATCHED`, and it
   passes the pure-layer VM-leak audit) plus `skyroads/hooks.py::
   master_timer_isr` (the pusha/popa/iret frame, the sound-engine call via
   `emulate_call`, the PIT/PIC ports).
3. **Same oracle, full coverage.** A unit oracle drives **every prescaler value
   0..9 × song-continue/end** and diffs full machine state — all 8 basic
   blocks, 22/22 byte-exact, including the wrap→reset→chain-to-BIOS path whose
   `dec` flags survive the far exit (the IRET path pops them away). The refactor
   is now installed by default and transparent (skyroads suite green, 154).

The payoff the exercise made concrete: the lift was correct on that flag
detail *out of the box* — exactly the kind of thing hand translation gets
wrong (as the hand-written overkill `4537` did, fixed the same day). The
lifter turns "hand-decode and hope" into "auto-lift, verify, then refactor a
proven artifact."

## 11. Decisions wanted from the owner

1. Placement as `dos_re/lift/` (extraction-ready) vs separate repo now.
2. The metrics honesty rule (§7) — lifted tier never counts as recovered.
3. M0 first (census before emitter) — or straight to M1 on one port?
4. Whether `instruction_count` preservation should be mandatory (my
   recommendation: yes in literal mode; it makes installs demo-transparent).


## Self-modifying code

The scanner refuses statically-visible code writes (`self-modifying` /
`code-patched-at-runtime`) so a frozen lift of mutable code is impossible;
the evidence-driven rehabilitation pass on top of that refusal --
transforming supported operand patches into live-memory reads -- is
[`desmc.md`](desmc.md).

Mid-frame palette / raster effects (copper splits, raster blinks) are
recovered on the DEVICE side, not by a lifter pass: the lifted port-effect
stream already preserves the palette writes and their raster synchronization
byte-exactly on every runtime, so `DOSMachine` journals them per displayed
frame and renderers compose bands -- [`raster_effects.md`](raster_effects.md).
