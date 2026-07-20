# Performance

Performance work in dos_re is evidence-backed and composition-specific. A
program may execute different regions through the interpreter, generated
VMless or CPUless code, ABI-recovered code, or authored implementations.
Measure the selected execution plan instead of assigning one cost model to the
whole program.

## Choose the measurement boundary

Report both the unit under investigation and the end-to-end workload that
matters:

- interpreter throughput for an interpreter change;
- replay interval wall time for verification work;
- boundary or frame time for a generated implementation;
- presentation time separately from authoritative simulation;
- export startup and steady-state behavior for a release candidate.

Record the Python implementation and version, host, selected execution-plan
digest, replay identity and interval, warm-up policy, sample count, and summary
statistic. Port-specific measurements belong with the port or in a dated
historical record, not in this framework contract.

## Runtime choices

PyPy can substantially accelerate long scalar interpreter or generated-Python
workloads, while CPython may be faster for short processes, extension-heavy
work, or frequent thread synchronization. Treat that as a hypothesis for the
current composition:

1. run the same deterministic replay interval under both runtimes;
2. compare complete continuation or canonical semantic state;
3. compare logs or stable event digests after excluding declared timing
   telemetry;
4. then compare steady-state wall time.

Re-run the equivalence check after changing the Python runtime, JIT, native
extension, or device implementation. Runtime choice is not verification
evidence by itself.

For test suites with independent tests, `pytest-xdist` can reduce wall time:

```bash
python -m pytest -q -n auto
```

It does not accelerate one dominant replay test, and multiple JIT workers may
each pay warm-up cost. Measure serial and parallel configurations before
adopting either as a gate.

## Optimize the owning layer

Use profiles and traces to locate the cost before changing code:

- interpreter hot paths belong to CPU, memory, or device models;
- generated-code overhead belongs to the relevant emitter or runtime adapter;
- authored replacements must remain separate catalog implementations;
- rasterization, scaling, audio output, and host UI belong to presentation
  adapters;
- replay persistence and comparison overhead belong to replay or verification.

Do not copy a project-specific fast path into the framework. Generalize it only
when its hardware or execution semantics are game-independent and covered by
focused tests.

`cProfile` observes only the thread on which it is enabled, and its call
instrumentation can distort call-heavy code. Profile the thread that performs
the work, then confirm the proposed improvement with repeated wall-clock A/B
runs over the same replay interval.

## Correctness gate

An optimization that claims faithful behavior must preserve the same contract
as any faithful replacement:

1. add focused adversarial tests for the changed operation and its refusal or
   fallback boundaries;
2. compare a deterministic oracle and candidate interval through
   `ReplayArtifact`;
3. compare complete continuation state when representations match, or the
   declared canonical semantic projection when they do not;
4. exercise self-modifying code, device, timing, and interrupt cases affected
   by the change;
5. run the full repository validation.

Trace equality is useful diagnostic evidence, but it does not replace the
endpoint state contract. An observed speedup is not sufficient reason to
weaken a fail-loud boundary or introduce an undeclared execution fallback.

Historical 2.0 benchmark data and optimization notes are preserved in
[`history/performance_2.0.md`](history/performance_2.0.md).
