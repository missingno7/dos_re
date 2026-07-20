> Framework method reference (DOS_RE 2.0).  Authority: [`dos_re_2.0.md`](dos_re_2.0.md)
> is canonical; where older per-routine/hook-first phrasing below conflicts with the
> 2.0 hard walls and automation principle, dos_re_2.0.md wins.  Promoted from the
> DOS_RE 1.0 starter (template_dos_port, retired) because the mechanics remain valid.

# Pitfalls — the mistakes the source ports actually made

Every entry below is a real mistake from the Overkill or Prehistorik 2 port,
with the consequence it caused and the rule that fixed it. These are not
hypothetical warnings; each one cost hours or days. Read this before your
first hook, and reread the relevant section whenever a verifier starts
disagreeing with you.

Format: **Mistake → Consequence → Rule.** (OK = Overkill, P2 = Prehistorik 2.)

## Naming and abstraction

**1. Naming by guess.** [OK] Hooks were named by assumed gameplay semantics
(`step_death_*` for what turned out to be a scripted boss-event subsystem, not
player death). → The false names reinforced a wrong mental model for weeks and
required a full evidence-trace rename. → *Keep address-level or structural
names until multiple independent evidence paths agree: `logic_id` before
`enemy_type`, `ObjectSlot` before `Enemy`. A kept address name is cheaper than
an encoded false abstraction.* (See the status ladder in
the retired 1.0 methodology docs (historical).)

**2. Width-aliased fields treated as one field.** [P2] The same bytes read at
different widths by the ASM (a velocity word vs an anim-mirror byte). → Bugs
where a "width argument" was threaded through call sites and got confused. →
*A different width is a different semantic: give each width its own named
descriptor (`facing` vs `facing_lo`), never a width parameter at the call
site.* (`dos_re.state_view` supports this directly.)

## Hooks and structure

**3. Logic accumulating in hooks.** [OK] Hooks were added without declared
lifetimes or merge targets until `hooks.py` reached 4106 lines *of gameplay
logic*. → The VM-shaped hook pile became the de facto architecture; every later
refactor had to first extract logic back out of glue. → *Every hook declares a
role (`dos_re.hook_taxonomy`) and a merge target (`@oracle_link`); hooks stay
registration + adapter only. "A hook without a lifetime is suspicious; a hook
without a merge target is suspicious." Falling glue-hook count IS progress.*

**4. The "checker" duplicate.** [OK] A hook kept a full ASM-shaped replay as
the real implementation and called the pure recovered function only to assert
agreement. → Two copies of one behaviour, drifting on the next change, with
the wrong one able to win silently. → *One recovered leaf, many adapters. The
adapter reads state → calls the leaf → writes back; the parallel replay is
deleted in the same commit that grounds the leaf.*

**5. Parent hooks hiding children.** [both] A lifted parent called a child
hook's Python function directly, making the child a shared black box inside
the parent's verify transaction. → `--verify-hooks` passed while the child was
wrong. → *Route child boundaries through
`call_installed_hook_like_near_call` / `jump_installed_hook_boundary`;
`dos_re/tools/audit_hook_oracle.py` enforces it statically.*

## Verification

**6. Trusting per-hook oracles alone.** [OK] A contact predicate was never
exercised by its hook's captured oracle (callers passed a stub), so three
different visible bugs all traced to one unverified branch. → Divergences only
surfaced in long demo replays, far from the cause. → *Per-hook oracles are
scaffolding; demo-replay equivalence is the real gate — and only counts if the
routine is actually exercised. For every hook: a focused oracle, evidence it
runs in a real demo, and zero demo-suite divergence.*

**7. Narrowing the diff.** [both] Restricting verification to "the registers
that matter" or a memory window. → Freed-stack scratch words, flag shapes, and
off-window writes hid there; bugs surfaced later as unexplainable frame
divergence. → *Full-memory + full-state diffs by default; narrow only as a
deliberate, temporary performance lever. Never weaken an oracle or test to
make a change pass.*

**8. Refactoring away "dead" flag writes.** [OK] Flag-setting helpers that
*looked* dead were removed; one EGA marker-byte bit-test was reversed in an
"optimization". → Demo divergence; visibly corrupted menu assets. The hook
oracle passed because it never exercised the branch. → *The boundary contract
is law. Before deleting a state write, prove it dead: instrument with a counter
and demo-replay; if the demo suite never reaches it (count = 0), the change is
UNVERIFIABLE — revert it, don't keep it.*

## State capture and rendering fidelity

**9. Ad-hoc mid-frame state reads.** [P2] A live viewer read renderer state
whenever it liked, mid-frame. → 79% of the frame diverged during fast camera
moves: the mirror mixed the in-progress frame's state with the completed
frame's pixels. → *Capture visual/game state as one snapshot at the frame
boundary (after the page flip commits), and render THAT snapshot.*

**10. Boundary capture of transient state.** [P2] Blink counters and one-shot
particles are mutated (or killed) *during* the draw pass, so the frame-boundary
capture read them post-mutation. → Particles vanished; blink was one phase
behind. → *State consumed by a pass is captured at that pass's ENTRY; the
frame boundary is right only for state that survives the frame.*

**11. Rebuilding history-dependent buffers from scratch.** [P2] The menu/map
pages are stateful (an init fill + per-frame self-copy + per-column updates);
a stateless rebuild "from the source data" was attempted. → ~11% pixel match —
worse than random guessing among plausible models. → *If a buffer is
history-dependent, either replay the real sequence from a known init (the game
is deterministic) or recover the exact invariant. Never guess a stateless
model for stateful VRAM.*

## Timing and determinism

**12. Skipping waits by poking flags.** [OK] "Fast-forward" set the timer flag
directly to skip the PIT wait. → The skipped ISR ticks lost music/SFX and
BIOS-chain state — ~314 bytes of measurable divergence. → *Fast-forward means
delivering the REAL installed ISR at the same instruction boundaries the
verifier uses, then letting the wait exit naturally. No game state is faked;
only the delivery point is synthetic.*

**13. Skipping across IRQ-due boundaries.** [P2] A wait-loop skip jumped past
points where a PIT tick was due. → The tick counter diverged and demos forked.
→ *Only collapse provably-identical poll iterations BETWEEN pump boundaries;
re-emit every due IRQ at its emulated-time point. The step budget is
`cpu.step()` invocations, not `instruction_count` — an IRQ entry is one step.*

**14. Conflating deterministic skip with live pacing.** [P2] The deterministic
retrace skip was reused in the interactive viewer. → The game ran faster than
real time (the live clock is wall-clock, not instruction count). → *Two
different mechanisms: deterministic fast-forward (advance the emulated
timeline exactly) vs live wall-clock parking (sleep until the real phase
matches, keep servicing IRQs). Never swap them.*

## Self-modifying and runtime-patched code

**15. Hooking patched code blind.** [OK] Some routines' live bytes are
rewritten at runtime (a cold display helper became a hot object-steering
routine after patching). → A hook written against the cold bytes would have
silently run the wrong recovery. → *Every accepted live-byte body is named and
signature-guarded (`self_disable_if_patched` in `dos_re.hooks`); an unknown
variant fails loud. The end state is static: observed bytes → named variant →
byte guard → explicit Python. Never keep "Python-level self-modifying code".*

## Performance

**16. Optimizing the hot leaf.** [OK] Per-address frequency profiling pointed
at small leaf hooks; effort went into micro-optimizing them. → The real cost
was interpreted *outer driver loops* crossing the VM/hook boundary thousands
of times — invisible to leaf-frequency counts. → *Profile control-flow
patterns (backward edges, boundary crossings — `dos_re/tools/profile_hotspots.py`
reports both) before optimizing anything. And never trade byte-exactness for
speed: a faster wrong replacement is a regression.*

## Layering

**17. Pure layers quietly importing the VM.** [OK] During refactors, `cpu`/
`mem` imports crept into the recovered-logic layers. → The "portable" game
logic became unmigratable to the native runtime. → *Automated layer audits run
with the test suite from day one: recovered/ never imports dos_re/cpu/mem/
hooks/offsets. `dos_re/tools/audit_layers.py` is that audit, ready to point at
your adapter's pure directories; `dos_re/tools/lint.py` covers the framework
side.*

**18. Two parallel state models.** [P2] A semantic frame model was built as a
*parallel* representation next to the machine-level render state, each
maintained separately. → Two truths for palette/camera/HUD state that drifted
apart. → *One canonical capture; the semantic/enhanced model is DERIVED from
it, never maintained beside it.*

## Working style (agent sessions)

**19. Broad speculative changes.** [OK] Autonomous sessions occasionally
attempted multi-subsystem refactors or "fixed" a divergence by weakening the
oracle. → Reverts, wasted effort, and near-miss silent regressions. → *The
smallest coherent unit per iteration; never commit red; a blocked slice is
fully reverted and logged as a blocker, not worked around. See
[`getting_started.md`](getting_started.md) for the current workflow.*

**20. Stopping at the symptom.** [OK] A long-open "player death divergence"
blocker was attacked at the death logic repeatedly. → No progress until the
actual cause — a missing contact predicate one layer *below* — was recovered.
→ *When a divergence resists two focused trace attempts, log it and move on;
returning later with more of the lower layer recovered often dissolves it.*

**21. Fluent semantic hallucination.** [P2] An AI agent watched bridge tiles
bend under the player and confidently described the player *eating something*
— a coherent, plausible, completely false story. → Any name or model built on
that reading would have encoded fiction into the port. → *Narrative sense is a
hypothesis generator, never a source. Semantics are earned per the status
ladder (multiple independent evidence paths), and the oracle is the only
judge. This is WHY the framework constrains AI rather than trusting it.*

**22. A demo corpus that flatters the code.** [both] Early demo suites
exercised the happy path (short runs, few transitions). → Divergences in
death/respawn, level-end, game-over, and rare spawns survived long past the
point they "should" have been caught; cold-start testing later exposed paths
no bounded demo reached. → *Corpus coverage is a measured artifact: track
which levels, transitions, behaviours, and RNG paths the demos exercise, and
treat the blind spots as open risks in every status report. Record death,
game-over, and full-playthrough demos early — they are the proof spine's
spine.*

**23. Presentation quietly mutating the simulation.** [P2] The tempting
widescreen shortcut — advancing the object producer/spawner so entities exist
in the margins — changes gameplay state; camera/render helpers "just poking"
one state byte have the same shape. → The enhanced build silently forks from
the verified game; demos recorded on one desync on the other. → *Enhancements
read state and write none, enforced by a parity gate (enhanced-at-neutral ≡
faithful, pixel- and state-exact). Anything that needs to write is not an
enhancement. See [`enhancements.md`](enhancements.md).*

**24. Building presentation over an unverified state seam.** [P2]
Early faithful/enhanced viewer backends grew alongside hook-based recovery
without a single enforced authoritative boundary. → They required transitional
state ledgers and one-implementation audits to stop parallel truths. → *A
non-authoritative enhancement may land incrementally, but only after the state
it consumes has a verified read-only seam. It never invents missing gameplay
state and never writes the authoritative model. See
[`enhancements.md`](enhancements.md) and
[`override_architecture.md`](override_architecture.md).*
