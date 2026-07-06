# Task: write the recovery status report

Regenerate the ledgers and report progress that is measured, not vibed.
Honesty is the product: an accurate "23% native" beats an impressive lie —
false confidence poisons every plan built on it.

1. **Regenerate generated artifacts**: the island manifest
   (`tools/gen_island_manifest.py`), and confirm its drift test passes.
2. **Collect the numbers**:
   - islands by status-ladder level (GUESS…CANONICAL) — counts and deltas
     since the last report;
   - native % over a demo replay (hooked / total step counts from your
     adapter's coverage collector — the framework only provides the hook
     points; if you haven't built the collector yet, say "not yet measured",
     never estimate), per island where classified;
   - demo corpus: count, total frames, pass rate, which
     levels/behaviours/transitions it exercises — and does NOT;
   - hook counts by taxonomy role (glue falling is progress);
   - open blockers (from the blocker file) with age;
   - raw-offset count remaining in recovered logic (state-mirror progress);
   - frontier manifest state, if coverage has converged that far.
3. **Narrate the deltas** in `docs/<game>/run_status.md`: what landed (with
   the verifier that proved it), what was reverted and why, what changed in
   the framework core (with the oracle evidence that justified it).
4. **Name the risks explicitly**: the largest unverified surface, the demo
   corpus's blind spots, any "modeled but approximate" hardware in play, any
   status-ladder claims that rest on a single oracle case.
5. Keep durable policy out of the status file (that belongs in the method
   docs); keep time-stamped state out of the method docs.

Finish with the REPORT block. The status claimed for the report itself is
simple: every number must be reproducible by the command that generated it.
