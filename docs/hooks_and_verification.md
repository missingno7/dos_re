# Backend adapters and verification

The architecture is defined by
[`override_architecture.md`](override_architecture.md). This document explains
the CPU-backed adapter and proof machinery which implements that architecture
while interpreted code remains in a plan.

## Selection comes first

Every executable implementation is an `ImplementationEntry` in the project's
single `ImplementationCatalog`:

```python
ImplementationEntry(
    descriptor=ImplementationDescriptor(
        implementation_id="sqz_decode",
        targets=frozenset({"function:main-image:1010:1234:v1"}),
        origin=ImplementationOrigin.AUTHORED,
        category=OverrideCategory.FAITHFUL,
        implementation_digest="...",
    ),
    implementation=sqz_decode,
    adapters=(BackendAdapter(
        CPU_MODEL_BACKEND,
        real_mode_sqz_adapter,
    ),),
)
```

Authored entries are inactive unless selected by
`ExecutionConfiguration.selected_overrides`. The planner chooses one owner for
each reachable target. `GameFrontend.bind_execution_plan` invokes only the
adapter for the runtime's declared carrier in that resolved plan. A selected
implementation with no adapter for that carrier fails loudly; it never falls
back to another implementation.

There is no global hook registry, import-time selection, environment selection,
or player flag which installs program behavior.

## The CPU-backed adapter

The semantic implementation is ordinary CPUless Python. A real-mode activator
may bind a thin callable into the CPU backend's private dispatch table. That
callable only:

1. marshals the recovered contract from registers, stack, and memory;
2. calls the catalog entry's semantic implementation;
3. applies declared results and effects;
4. reproduces the exact continuation.

The body must not receive a decoder or an implicit operation that falls back
to interpreted instructions. CPUless does not imply memoryless: DOS memory may
remain a declared authoritative capability.

Near return, far return, interrupt return, internal continuation, and
non-returning transfer are distinct contracts. The adapter must reproduce the
one evidenced for its stable target.

Generated parents use `call_installed_hook_like_near_call`,
`call_installed_hook_like_far_call`, or `jump_installed_hook_boundary` when
crossing another selected CPU-backed boundary. These helpers preserve original
control-flow mechanics and keep nested verification visible. They do not
select implementations.

Runtime-patched code uses `runtime_code.py` identities and signatures. Unknown
variants fail loudly; they do not silently interpret or choose a different
implementation.

## Focused hook oracle

`dos_re.verification` can prove a selected faithful CPU-backed implementation
at one call boundary. It clones the pre-call runtime, executes original
instructions on the oracle clone, executes the selected adapter on the
candidate, and compares continuation state.

`HookVerifierConfig.strict()` derives the oracle stop from the candidate's
actual continuation and compares full memory by default. Explicit
`GenericHookStop` metadata can make repeated local verification faster.

This verifier is a focused development tool. It does not own selection and it
does not replace ReplayArtifact interval verification.

## Replay interval verification

The canonical project command is:

```text
python scripts/play.py --profile verification \
  --play-replay artifacts/replays/example \
  --verify-start X --verify-end Y
```

Add `--bisect` to persist the first divergent transition. The frontend supplies
the oracle and candidate `ReplayDriver` pair; `player.main` owns the command,
stable points, comparison, and exit status.

Verification compares complete continuation state when both sides share the
machine representation, or the same `CanonicalState` schema when the candidate
is detached or memoryless. A reproduction is always a boundary reference
inside the original replay artifact; verification creates no secondary replay
artifact.

## Verification is a scoped claim

A passing comparison proves one finite statement:

> this implementation digest, under this execution-plan identity, matched this
> oracle identity over this replay and exact interval using this projection
> schema.

It does not prove the function correct for all possible inputs. Reports and
implementation descriptors should cite those claim identities and summarize
their observed replays, intervals, invocations, paths or blocks. A bare
`verified=True` is not an architectural authority.

dos_re defines no global minimum replay count, invocation count, or
block-coverage percentage before development may select a faithful
implementation. The practical default is to proceed after one relevant
passing oracle interval and focused contract tests, then accumulate more
evidence whenever new replays visit the implementation. Projects may demand
more for unusually risky subsystems, but evidence counts are prioritization
signals rather than universal promotion gates.

A newly discovered divergence is a new counterexample, not evidence that the
earlier passing claims were fictitious: those claims remain true for their
recorded scope. Persist the failing boundary, fix the implementation under a
new digest, replay that smallest counterexample first, and then rerun every
previously passing claim affected by the old digest.

Release closure and behavioral confidence are separate. Release must have a
closed implementation/dependency graph and pass the finite verification suite
declared by its policy. Closed-world packaging does not pretend that the suite
enumerates every possible program input.

## Framework interceptors

Replay clocks, wait parking, profilers, diagnostic probes, device entrypoints,
and verifier wrappers are runtime services. They may use a backend's private
dispatch mechanism, but they are not program implementations and cannot claim
coverage. Their availability is constrained by the execution policy and they
must not enter a release closure unless declared product-safe.

## Verification policy by category

| Category | Contract |
|---|---|
| faithful replacement | Equivalent complete or canonical state at the interval endpoint; mismatch is failure |
| non-authoritative enhancement | Authoritative state remains equivalent; only declared presentation output is excluded |
| behavioral modification | Expected divergence is declared and covered by modification-specific tests |

One authored body can have real-mode, protected-mode, generated, ABI-recovered,
or detached activators. Semantic duplication between those adapters creates a
second authority and is forbidden.
