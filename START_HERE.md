# START_HERE — you are the porting agent

You are an AI agent (or a human — same rules) who has been given this
framework and a DOS game to port. This file is the boot sequence. Everything
else is reachable from here.

## What you are building

A verified, native source port of the game, recovered one proven routine at a
time from the original executable running in this repo's VM. The original
binary is the oracle — the single source of truth. You never guess behaviour;
you trace what the original did and match it, byte-exact, at every step.
Definition of done: the native port replays the whole demo corpus with the VM
disabled in the hot path, and the VM-as-oracle suite confirms frame-and-state
equivalence. ([`docs/lifecycle.md`](docs/lifecycle.md) tells the whole arc.)

## Boot sequence

1. **Read, in order:** [`docs/lifecycle.md`](docs/lifecycle.md) →
   [`docs/ai_porting_charter.md`](docs/ai_porting_charter.md) (the method —
   read all of it, twice for §6) → [`docs/pitfalls.md`](docs/pitfalls.md)
   (the mistakes already made for you) →
   [`docs/porting_new_game.md`](docs/porting_new_game.md) (the checklist you
   will now follow). For each recurring task type, use the ritual in
   [`prompts/`](prompts/README.md) — every task ends with its accountability
   REPORT block, and status claims follow the ladder (never present OBSERVED
   work as VERIFIED).
2. **Set up the workspace.** The game's files (EXE + data) go in `assets/`
   (gitignored — original game files are never committed). Create your adapter
   package next to `dos_re/` by copying the shape of
   [`examples/adapter_skeleton/`](examples/adapter_skeleton/README.md), named
   after the game. Run `python examples/minimal_adapter/example.py` once to
   confirm the framework works on this machine.
3. **Start the ledgers** (empty is fine): `docs/<game>/run_status.md` (current
   phase, recent findings), `docs/<game>/symbol_ledger.md` (addresses →
   evidence), `docs/<game>/blockers.md` (see the loop protocol), and the
   generated island manifest (`tools/gen_island_manifest.py`).
4. **Follow [`docs/porting_new_game.md`](docs/porting_new_game.md)** step by
   step: load & run → see output → find frame boundaries → stand up the frame
   verifier → build the input-wait registry → record the first demo → start
   the lifting loop.

## The loop protocol (how work proceeds, slice by slice)

Proven over months of autonomous recovery on the source ports:

1. **One slice per iteration** — one routine, one field naming, one raw-offset
   drain; the smallest coherent unit. Not a subsystem.
2. **Never commit red.** Every commit passes lint + the test suite + the demo
   gates. One slice = one focused commit.
3. **Blocked ⇒ revert + log.** If a slice can't be finished byte-exact, or the
   fix would require guessing: revert all its changes immediately, write the
   evidence into `docs/<game>/blockers.md`, and take the next target. A logged
   blocker is progress; a workaround is debt. If a divergence resists ~2
   focused trace attempts, it usually needs a lower layer recovered first.
4. **Never weaken an oracle or test to make a change pass.** Fix the code to
   match the original, or revert.
5. **Fail loud, never fake.** An unrecovered path raises a
   [`HybridGap`](dos_re/gaps.py); it never silently falls back to ASM or to a
   plausible guess.
6. **Check for existing mechanisms before building.** The framework and your
   own adapter likely already have the tool (see the module map in
   [`docs/architecture.md`](docs/architecture.md), the `tools/` directory, and
   the two source repos as worked examples).
7. **Update the ledgers as you go** — `run_status.md` for state, the island
   manifest for progress, the symbol ledger for evidence. The next agent (or
   the next session of you) resumes from git + these files alone.

## The framework is a living organism

Your game WILL exercise CPU instructions, DOS services, and hardware behaviour
the previous games didn't. Extending `dos_re/` is part of the job — under its
rules ([`AGENTS.md`](AGENTS.md)): stdlib-only, game-agnostic, add only what
your executable *proves* it needs, document the observed register/flag
contract, add a focused test, keep it deterministic by default. When the VM
fails loud on an unimplemented opcode or port, that is the framework asking to
grow — implement the observed behaviour, never a datasheet's generality. If
you build a mechanism that the *next* game would reuse (a new hardware model,
a new verifier capability), promote it into `dos_re/` with an origin note; if
it knows your game's addresses or formats, it stays in your adapter.

## Hard boundaries (violating these voids the work)

- `dos_re/` never learns your game (enforced: `python tools/lint.py`).
- Your `recovered/` layer never imports the VM (add the layer audit to your
  adapter's tests on day one — pitfall #17).
- One shared definition of "a boundary" and "a wait loop" across all drivers
  ([`docs/demos_and_snapshots.md`](docs/demos_and_snapshots.md) — the trap
  that silently voids demo proofs).
- Full-memory diffs by default; narrowing is a temporary, deliberate lever.

## Progress is measured, not vibed

- % of per-frame instructions running native vs interpreted
  (`coverage_telemetry`, adapter-classified into islands).
- The generated island manifest (count × confidence ladder).
- Demo-corpus coverage and pass rate.
- The glue-hook count (falling is good) and the frontier manifest
  (`dos_re/frontier.py`) once coverage converges.

When in doubt: trace it, snapshot it, prove it. The oracle is right there.
