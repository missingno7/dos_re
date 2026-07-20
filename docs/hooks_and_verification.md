# Hooks and verification

> **dos_re 3.0 architecture:** [`override_architecture.md`](override_architecture.md)
> defines the target baseline-plus-overrides model. This document describes
> the currently implemented CPU-backed hook mechanics and differential
> engines that will become backend adapters. New project architecture should
> use the three override categories and must not treat generated lifts,
> framework interceptors, and authored overrides as one hook layer.

The currently implemented hook oracle lets a faithful authored replacement run
at one original routine boundary and proves—rather than assumes—that its
behavior is exact.

## What a hook is (and is not)

A CPU-backed hook is a **minimal backend adapter, not a place where logic
accumulates**. A good hook only:

1. marshals the recovered contract from memory, registers, and stack state,
2. calls the CPUless authored body with natural arguments and only its declared
   capabilities (which may include DOS memory, but never a CPU/interpreter),
3. writes the result back to original memory/registers,
4. returns to the original control flow with **exact** return mechanics:

```text
near routine:    cpu.s.ip = cpu.pop()
far routine:     cpu.s.ip = cpu.pop(); cpu.s.cs = cpu.pop()
internal block:  cpu.s.ip = <exact continuation IP>
```

Do not assume a routine returns; some are loop bodies, jump targets, or dispatch
stubs. Never add a hook because it looks right — every hook needs oracle
evidence (a VM trace of what the original actually did).

## Registering hooks

```python
from dos_re.hooks import registry

@registry.replace(0x1010, 0x1234, "sqz_decode_1234")
def sqz_decode_1234(cpu):
    ...
```

`dos_re.runtime.create_runtime()` installs everything registered.  Duplicate
registrations at one address fail fast.  `DOS_RE_DISABLE_HOOKS=CS:IP,CS:IP`
disables individual hooks for A/B checks without code changes.

When a lifted parent composes a child routine that is itself hooked, never call
the child's Python function directly — route through
`call_installed_hook_like_near_call` / `jump_installed_hook_boundary` so the
child stays a verifier-visible boundary instead of a shared black box.
`tools/audit_hook_oracle.py` statically enforces this.

## The hook oracle (`dos_re/verification.py`)

The differential verifier wraps every hooked address. On each call it clones
the runtime, executes the **original ASM** on the clone up to the hook's
continuation, executes **your hook** on the live runtime, and diffs registers +
flags + (by default) full memory. Two modes:

- **metadata mode** — the adapter declares each hook's valid continuation
  (`GenericHookStop("near_ret")`, far-ret, iret, fixed-ip, computed dispatch, …).
  Fast; the standing verification for a maturing adapter.
- **strict / auto-continuation mode** (`HookVerifierConfig.strict()`) — runs the
  hook first, takes its final address as the only acceptable target, then runs
  the original ASM to that address and diffs. No metadata to maintain; slower;
  ideal for focused investigation.

On a divergence, set `OK_TRACE_HOOK="CS:IP"` and reproduce: the verifier prints
the exact ASM-oracle instruction trace — what the original did that your hook
did not. Fix the hook to match what the original *did*, not what you think it
should do. The classic bug classes (you will hit all of them): freed-stack
scratch words, flag shape (INC preserves CF — use `dos_re.asm` helpers),
early-out branch selection.

**Full-memory + full-state diffs by default.** Narrowing the diff hides bugs.
Narrow only as a deliberate, temporary performance lever.

## The frame oracle (`dos_re/frame_verify.py`)

Per-hook equivalence gets weaker as hooks collapse into larger native chains,
so the second engine diffs **whole frames**: it steps a reference runtime (pure
ASM) and a candidate runtime (hooked/native) to adapter-defined frame
boundaries, samples framebuffer + visible VRAM (+ whatever state the adapter
adds), and dumps PNG/report artifacts on divergence.

The adapter supplies: boundary addresses (present/timer/retrace), a
`sample_builder`, `reference_env_hooks` (the hardware waits the *oracle* side
must keep so the original ASM doesn't spin forever), optional `pump_inputs`,
and the shared input-wait detector (see
[`demos_and_snapshots.md`](demos_and_snapshots.md) — mandatory reading before
trusting any demo).

Widen the frame sample until it covers **all observable state** — every object
field, RNG state, score/lives, timers, and the framebuffer. *If it is not in
the snapshot, divergence can hide there.*

## Hook roles and lifetimes (`dos_re/hook_taxonomy.py`)

Classify each hook by **role**, not address:

| Role | Meaning | Direction |
|------|---------|-----------|
| **checkpoint** | a real logical resume boundary (frame/object-update/render/input) | keep, make explicit |
| **env_wait** | hardware/environment wait (PIT/IRQ0, CRTC retrace, INT 09h) the interpreter can't satisfy natively | keep hooked, even on the oracle reference |
| **debug_probe** | exists only to observe/verify | keep out of the hot path |
| **glue** | accidental ASM-boundary plumbing (tails, helpers, per-row scan steps) | collapse into native chains between checkpoints |

A registered hook address is scaffolding, not architecture. Collapsing glue
chains into one native flow is desirable — but only with evidence from the real
original call graph, and with correctness protected by the frame/state verifier
rather than by preserving historical hook boundaries.

## One authored body, many backend adapters

The override body is the **single implementation**. Interpreter, VMless,
CPUless, and ABI-recovered execution use backend-specific wrappers over that
same body. The differential verifier observes the faithful body through those
wrappers; it does not require a verification-only implementation.

Duplicating semantic logic between a CPU hook and a detached backend silently
forks the program from its own proof. See
[`override_architecture.md`](override_architecture.md) for the shared registry
and category policies.

Beyond the confidence ladder on each function
(the retired 1.0 starter's methodology docs (historical)), track which **adapter state** each piece
is in — every rendering/audio piece is exactly one of:

1. **recovered + live-grounded** — leaf + live replacement hook + verifier;
2. **recovered, verify-only** — leaf + checkpoint diff, but the ASM still runs it;
3. **native-consumer-only** — composed by the native side but *not* grounded by
   a live hook (a transitional state to fix — not an endpoint, not "done");
4. **known gap** — not recovered; fails loud everywhere (never a VM fallback);
5. **blocked — history-dependent buffer state** — the original keeps stateful
   buffers (scroll-page rings, self-copies); needs the real stateful model, not
   a from-scratch rebuild;
6. **not worth hooking** — a pure controller/setup wrapper with no hot or
   reusable behaviour.

Record the recovered set itself with `@oracle_link` metadata and a generated
manifest (`dos_re.islands` + `tools/gen_island_manifest.py`) so "what is
recovered" is answered by the code, not by a hand-edited list.

## The hook lifecycle

```text
observe -> classify -> choose boundary -> build ASM oracle -> implement hook -> verify -> document
```

See the retired 1.0 starter's methodology docs (historical) for each step in detail and its
`docs/ai_porting_charter.md` §4 for the per-slice lifting loop this fits into.
