# Task: audit for silent fallbacks

Silent fallback creates fake confidence — a run that "works" while quietly
guessing. Sweep the adapter (and any framework code touched recently) for the
danger patterns and make each one loud, modeled, or explicitly documented.

Search for:
1. **Quiet ASM fallback**: any path where native/hybrid code, on missing
   recovery, falls back to the original instead of raising a `HybridGap`.
2. **Default-instead-of-model**: unsupported video/write mode treated as
   another mode; unknown interrupt/service returning "success"; unknown port
   read used for logic. (The core's known cases are documented in
   `docs/hardware_support.md`; anything new needs the same honesty.) For port
   reads specifically: check `dos.unmodeled_port_reads`, and run a session
   with `rt.dos.strict_ports = True` — every unmodeled read then fails loud
   with the reading CS:IP.
3. **Tolerated mismatches**: a verifier/checkpoint that logs a diff without
   failing; a comparison narrowed to "the bytes that matter"; a test relaxed
   to pass (grep for recently-changed tolerances, skips, and `except` blocks
   swallowing verifier exceptions).
4. **Signature-less patched-code hooks**: hooks at addresses the game patches
   at runtime without `self_disable_if_patched` guards.
5. **Boundary drift**: a driver with its own frame/wait definition instead of
   the shared input-wait registry.

For each finding, classify: *intentionally unsupported* (make it raise with
context) / *not yet modeled* (raise + ledger entry) / *modeled but
approximate* (document exactly where it diverges and what verifies it) /
*adapter responsibility* (move it there). Fix small ones inline; file blockers
for the rest. Every change re-runs the full gates.

Finish with the REPORT block, listing every location examined — an audit that
only reports its hits can't be re-checked.
