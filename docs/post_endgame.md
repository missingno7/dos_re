> Framework method reference (DOS_RE 2.0).  Authority: [`dos_re_2.0.md`](dos_re_2.0.md)
> is canonical; where older per-routine/hook-first phrasing below conflicts with the
> 2.0 hard walls and automation principle, dos_re_2.0.md wins.  Promoted from the
> DOS_RE 1.0 starter (template_dos_port, retired) because the mechanics remain valid.

# Post-endgame playbook — enhancements on the native product

> **GATE: do not read this during recovery.** This playbook applies only after
> the flip — a complete faithful VM-less game with every authoritative
> `ReplayArtifact` interval green under `verify_interval`. If you are here earlier, close this and
> go back to the checklist; pitfall #24 is how projects drown. The *rules*
> (sequencing, the read-only boundary, the parity gate) are
> [`enhancements.md`](enhancements.md) — this is the workflow on top of them.
> This is an optional late-product workflow, not an architectural prerequisite
> for non-authoritative enhancements. The override architecture permits an
> enhancement earlier once its authoritative read-only seam is verified.
>
> Every game's post-endgame is different; this file deliberately carries only
> the **sterile core ideas** that survived a completed port, not a feature
> catalog to reproduce.

## The three structural rules

**1. This playbook installs enhancements only in the shipped product profile.**
The interpreted oracle stays pristine. Earlier incremental enhancement work,
when useful, follows the same registry category and verified read-only seam;
it does not modify the baseline backend.

**2. Everything is a toggle with a faithful default, behind the in-game
menu.** One settings dict, persisted next to the game data, edited live. The
menu widget is a framework piece now — `dos_re.overlay_menu` (tabbed modal
overlay, pygame-injected, items-as-data closures, structural determinism
firewall: the tick freezes while it is open, so nothing it consumes can
perturb the game or a demo).

**3. The tab taxonomy IS the accuracy boundary.** Three classes, never mixed:

| Tab class | Contents | Contract |
|---|---|---|
| **Presentation** (Display / Audio / …) | read-only enhancements | parity gate: enhanced-at-neutral ≡ faithful, pixel- and state-exact; the demo corpus stays green with them on |
| **Experimental** | anything that can affect game accuracy (state-writing opt-ins, input-layer comforts, gameplay-adjacent toggles) | quarantined in this one tab, labeled, default OFF; each documents exactly what it writes |
| **Debug** (cheats) | deliberate game-state writes | exists only behind a `--debug`-style flag; hidden from the product |

If a proposed enhancement can't meet the Presentation contract, it is not
"almost safe" — it goes to Experimental, or it doesn't ship.

## The bridge-free product (the packaging endgame)

The end-user product must carry NONE of the verification machinery — the
VM-facing "bridge" (frame capture, timing fast-forwards, hook glue) becomes a
DETACHABLE package plugged in only when a proof runs. Make the boundary true
**by construction** (the tree), not by deny-list alone:

- **Split the seam package by MEASURED imports** (compute the product's real
  import graph, transitive over top-level imports — do not guess): the pure
  state-view / dataclass-reader modules the product needs go to a shipped
  `views/`-style package; the VM-coupled glue stays behind as the workbench.
  Watch both import forms (`from pkg.mod import x` AND `from pkg import mod`)
  — a closure computed from one form ships a tree that breaks on import.
- **Three enforcement layers**: a structural lint (shipped layers never import
  the workbench or the emulator — top-level always; function-local stays the
  fail-loud pattern for VM-needing flags), the deploy deny-list as the
  belt-and-braces assert, and the deploy smoke test (N ticks + render on the
  deployed tree, then assert no denied module ever entered `sys.modules`).
- **Prove the split is a behavioral no-op with the proof corpus** — the whole
  point of having it: full test suite and every authoritative replay interval
  equivalent before and after. A repackaging this size is
  safe exactly because the corpus exists.
- The shipped `views/` package is then the landing zone for the READABILITY
  lift: gameplay code stops naming raw offsets; the views layer is the one
  place layout lives; the workbench maps it back to the original bytes when
  verification plugs in.

Worked example: pre2_port's `pre2/views/` (28 shipped state-view modules) vs
`pre2/bridge/` (8 detachable workbench modules), enforced by its
`scripts/lint.py` + `deploy_native.py` DENY; proven no-op by its complete
oracle/candidate replay corpus.

## The workflow: agent-executed, human-steered

Recovery had one judge (the oracle) and needed almost no human input. The
post-endgame is uncharted per game, and the human judges **taste** — which
cannot be traced from the binary. The loop:

1. **Propose** — a short pitch per enhancement: what it does, which tab class
   it lands in, the cost. Let the human pick and order.
2. **Prototype behind a toggle** (faithful default), persisted, live-switchable.
3. **Show, don't describe** — screenshots / before-after pairs (menu toggling
   makes A/B trivial); ask concrete taste questions.
4. **Iterate on feedback**, then the parity gate + the full demo corpus
   before the commit. Ledgers stay current — still the loop protocol.

Expect much more human interaction per slice than recovery ever needed; that
is what "presentation" means.

## FPS first, interpolation second

Interpolation is the *fallback*, not the feature. The smoothness ladder:

1. **Never raise the game tick.** The simulation rate is gameplay (RNG,
   physics, timers) — running it faster is a different game.
2. **Re-render natively at display rate where presentation allows it.** If a
   motion is driven by presentation-side values the renderer derives per
   frame (camera pan, scroll position, palette ramps) and the renderer is a
   pure function of state, present MORE REAL FRAMES — no state mutation, no
   made-up frames.
3. **Only where motion is welded to simulation state**, interpolate
   presentation between two captured tick snapshots. Lerp presentation, never
   simulation.

State the tier per moving element in the port's enhancement notes.

## Worked examples and the promotion path

The completed port's Stage-6 layer is the reference implementation
(pre2_port: interpolation's two-snapshot window `frame_capture.py`, smooth
transitions, widescreen decomposition — the widescreen and pixel-aspect
*lessons* are in [`enhancements.md`](enhancements.md)). Read them as *ideas
that survived*, not features to copy: each game's list will differ.

Promoted so far: the overlay menu (`dos_re.overlay_menu`). Next candidate
when a second port reaches Stage 6: the two-snapshot interpolation presenter
(see `roadmap.md`).
