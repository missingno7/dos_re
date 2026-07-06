# Prompt pack — standard rituals for recovery tasks

Each file here is a self-contained task brief for an AI agent (or a human
following the same discipline). They exist so the workflow is **encoded, not
tribal**: every task ends with the same accountability block, and no task lets
plausible output masquerade as verified output.

Use: open the prompt for the task at hand, follow it, and fill in its REPORT
block before finishing. `START_HERE.md` routes here; the loop protocol there
(smallest slice, never commit red, revert + log blockers) applies to every
prompt.

## The accountability block (every task ends with this)

```
REPORT
- Evidence:       what oracle facts this rests on (traces, snapshots, goldens — paths)
- Changed:        files + one-line what/why each
- Proven by:      which verifier(s) passed (hook oracle / frame oracle / demo replay / golden test) and the command
- Status claimed: GUESS | OBSERVED | RECOVERED | ASM_MATCHED | VERIFIED | CANONICAL
- Unknown:        what remains unproven or unexplored, explicitly
- Blockers:       anything reverted + logged, with the blocker-file entry
```

Never present a lower status as a higher one. A routine that is merely
described is OBSERVED, not RECOVERED; code that merely runs is RECOVERED, not
VERIFIED. The ladder is defined in `dos_re/islands.py` and
[`docs/methodology.md`](../docs/methodology.md).

## The one warning that matters

**AI can be confidently wrong about semantics.** On Prehistorik 2, an agent
once interpreted bridge tiles bending under the player as the player *eating
something* — a fluent, plausible, completely false story. That is why the
oracle stays central: without it you guess; with it you are forced to
converge. Name things by evidence, verify before you trust, and treat your own
narrative sense as a hypothesis generator, never a source.

| Prompt | Task |
|---|---|
| [`start_new_game_adapter.md`](start_new_game_adapter.md) | Day 0: stand up a new game's adapter and reach the first verified frame boundary. |
| [`recover_one_routine.md`](recover_one_routine.md) | The core loop: lift one routine, one verification. |
| [`diagnose_hook_divergence.md`](diagnose_hook_divergence.md) | A verifier disagreed with you. Find out why the *original* is right. |
| [`create_state_mirror.md`](create_state_mirror.md) | Name an island's offsets behind typed views without weakening verification. |
| [`promote_hook_to_subsystem.md`](promote_hook_to_subsystem.md) | Collapse proven islands into a larger native unit. |
| [`audit_no_silent_fallbacks.md`](audit_no_silent_fallbacks.md) | Sweep for quiet guesses, tolerated mismatches, and fake confidence. |
| [`write_recovery_status.md`](write_recovery_status.md) | Regenerate the ledgers and report honest progress. |
