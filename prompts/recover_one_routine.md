# Task: recover one routine (the core loop)

One routine, one verification, per slice. Nothing else.

1. **Pick by evidence**: a profiler hotspot or a frame-verify divergence — not
   an address that "looks interesting". State why this routine, with numbers.
2. **Observe**: trace it in the VM from a snapshot fixture. Record: entry
   state, exit IP, stack effect, registers/flags written, memory touched,
   file/port side effects. Save the snapshot; it is the golden's seed.
3. **Classify + choose the boundary** (`docs/methodology.md` hook lifecycle):
   the smallest coherent unit with a clean continuation. Avoid broad parents
   that hide unverified children.
4. **Implement**: a pure recovered rule (VM-free, unit-testable, in
   `recovered/`) behind a thin hook adapter with exact return mechanics. Use
   `dos_re.asm` helpers for flag-exact 8086 semantics. Tag the rule with
   `@oracle_link(boundary, contract, status="RECOVERED", merge_target=...)`.
5. **Verify**: strict mode first (`HookVerifierConfig.strict()`), then add the
   `HookStop` metadata. Full-memory diff. Then confirm the routine actually
   fires in a demo replay with zero divergence — a hook no demo exercises is
   NOT verified (pitfall #6). Only now raise the status to ASM_MATCHED /
   VERIFIED accordingly.
6. **Document**: symbol ledger entry, island manifest regenerated, run_status
   updated.

Hard rules: address-level names until evidence converges (pitfall #1); no
logic accumulating in the hook body (pitfall #3); if it diverges and resists
two focused trace attempts (`OK_TRACE_HOOK`), revert fully, log the blocker,
and take the next target (pitfall #20). Finish with the REPORT block.
