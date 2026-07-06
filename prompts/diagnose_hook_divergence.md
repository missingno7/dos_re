# Task: diagnose a hook/frame divergence

A verifier says your code and the original disagree. The original is right.
Your job is to find out *what the original actually did*, not to make the
report go away.

1. **Reproduce deterministically**: the divergence report names the hook, the
   call count, and (with repro capture on) a saved pre-state. Load it.
2. **Get the truth**: set `OK_TRACE_HOOK=CS:IP` and rerun — the verifier
   prints the ASM oracle's instruction trace to the continuation. Read it
   before theorizing. For frame divergences, read the dumped
   ref/hook/diff/compare PNGs and the changed-offset list.
3. **Check the classic causes first** (they account for most divergences):
   freed-stack scratch words below SP; flag shape (INC preserves CF; use
   `dos_re.asm`); an early-out branch that jumps to a shared RET; a nested
   child hook polluting the reference (route through
   `call_installed_hook_like_near_call`); capture phase (pass-entry vs
   frame-boundary state — pitfalls #9/#10); an IRQ due inside a skipped span
   (pitfalls #12/#13).
4. **Fix the hook to match the trace** — never adjust the verifier, never
   narrow the diff, never special-case the test (pitfall #7). If the trace
   shows the boundary itself was wrong (the routine doesn't end where you
   declared), fix the HookStop/boundary, re-verify from scratch.
5. If two focused attempts don't close it: full revert, blocker-file entry
   with the trace excerpt and your best hypothesis, move on. Deep divergences
   usually dissolve when a lower layer gets recovered (pitfall #20).

Finish with the REPORT block — including, explicitly, what the original did
that your code did not.
