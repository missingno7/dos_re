# Task: promote hooks into a subsystem (coastline shortening)

Collapse a set of proven neighbouring islands into one larger native unit.
This is Stage 3 of `docs/lifecycle.md`; read its two working rules first.

Preconditions — do not start unless all hold:
- every island involved is VERIFIED (not just RECOVERED), tagged, and demo-
  exercised;
- the original call graph *proves* they belong to one original
  routine/controller (traces, not aesthetics — the collapse rule);
- the glue between them is classified `glue` in the hook taxonomy.

1. **Map the target**: name the subsystem after the evidence
   (`frame renderer`, not `RenderingEngine2`). Its islands' `merge_target`
   fields should already point at it.
2. **Compose, don't rewrite**: the subsystem calls the SAME recovered leaf
   functions the hooks used (one leaf, many adapters — pitfall #4). Where a
   recovered island returned to ASM only to reach another recovered callee,
   call it directly.
3. **Keep children verifier-visible** where they remain hooked:
   `call_installed_hook_like_near_call`, and run
   `tools/audit_hook_oracle.py` after.
4. **Prove it standalone**: given state captured from a snapshot, the
   subsystem reproduces the oracle's output byte-exact *without stepping the
   VM* — as a committed test. Then demo-replay: zero new divergences with the
   collapsed chain live.
5. **Retire scaffolding in the same change**: remove the now-internal glue
   hooks, update the taxonomy and frontier manifest, regenerate the island
   manifest. Falling hook count is the point.

If the composed subsystem diverges where the individual islands passed, the
glue you collapsed had behaviour you didn't understand — revert, trace the
glue, recover it as its own island first. Finish with the REPORT block.
