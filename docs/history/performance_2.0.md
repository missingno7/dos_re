# Performance benchmark record from dos_re 2.0

This is a dated measurement record, not current tuning guidance or a supported
workflow. It preserves the empirical observations that informed the framework
performance contract.

Measurements made on Windows with CPython 3.11 and PyPy 3.11 v7.3.20 found:

| Historical workload | CPython | PyPy | Observed speedup |
|---|---:|---:|---:|
| SkyRoads interpreted steady state | 714k instr/s | 12.5M instr/s | 17.5x |
| Prehistorik 2 interpreted steady state | 623k instr/s | 8.3M instr/s | 13.4x |
| 5,109-frame SkyRoads replay verification | 500.8 s | 47.3 s | 10.6x |
| 60-frame SkyRoads replay verification | 40.4 s | 7.5 s | 5.4x |

The shorter replay gained less because process startup and JIT warm-up were a
larger fraction of the run. The two long verification logs had matching stable
content after timing telemetry was excluded. That comparison was evidence for
those exact runtime versions and artifacts only.

Parallel test measurements also varied by suite. A verification-heavy suite
improved from 4m17s serial to 56s with CPython and `pytest-xdist`, while suites
dominated by one replay test changed little. PyPy plus many workers could be
slower because every worker paid JIT warm-up.

Generated CPUless code exhibited a different cost model. Historical profiling
identified eager flag computation, linear basic-block dispatch, bytewise
memory helper calls, and cross-thread boundary handoff as possible costs. The
important lesson was methodological: profile the executing thread, use
deterministic profiles only to form hypotheses, and confirm every change with
wall-clock A/B measurements and oracle equivalence.

These numbers should not be quoted as expected dos_re 3.0 performance. Current
results depend on the selected per-region implementations, replay interval,
runtime, devices, presentation adapter, and host.
