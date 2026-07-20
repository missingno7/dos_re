# dos_re 3.0

dos_re is an oracle-driven framework for recovering DOS games into maintainable
programs. It keeps the original executable runnable as a reference, extracts
canonical program structure into Recovery IR, records deterministic execution
evidence, and lets generated and authored implementations replace original
regions incrementally. The result is one mixed implementation graph—not a
choice between unrelated “emulated”, “VMless”, or “native” games.

Unlike an emulator, dos_re is designed to remove its own lower-level
dependencies over time. Unlike a decompiler, it keeps executable evidence and
verification attached to stable program identities. Unlike a manual port, it
can compare every faithful replacement with the original oracle at reproducible
runtime intervals.

## The dos_re 3.0 model

```text
original program
      |
      +-- Recovery IR -------------------+
      +-- deterministic ReplayArtifacts -+
                                         v
                                  Execution Atlas
                                         |
                              conservative ProgramCoverage
                                         v
 implementations + services + bootstrap + explicit policy
                                         |
                                         v
                                  ExecutionPlan
                         +---------------+---------------+
                         v                               v
              development / verification        detached / release
                         |                               |
                  oracle comparison               closed-world export
```

Recovery IR owns recovered static structure. `ReplayArtifact` owns deterministic
events, continuation state, cached boundaries, and observed execution.
The Execution Atlas combines that evidence for navigation and conservative
coverage; it is not an execution engine. `ImplementationCatalog` describes the
available original, generated, and authored implementations.
`ExecutionPlanner` selects exactly one implementation for every reachable
identity and computes the full dependency closure. The unified player executes
that immutable plan through a backend adapter. Export accepts only a complete,
package-ready release plan and physically includes its declared closure.

Interpreted, VMless, CPUless, ABI-recovered, DOS-memory-backed, memoryless, and
native are properties of individual implementations. A single recovered game
can use several of them at once. A fully native port is simply a complete
selection whose dependency closure no longer contains the original executable,
CPU model, DOS memory, or dos_re runtime.

## Recovery lifecycle

1. Load the original program and retain recovered facts as Recovery IR with
   stable program, image, function, region, execution-point, and runtime-code
   identities.
2. Record oracle-verifiable `ReplayArtifact`s. Replays provide deterministic
   input, persistent base-relative boundaries, function visits, transfers, and
   exact verification intervals.
3. Build or update the Execution Atlas from Recovery IR and replay evidence.
   Unknown control flow remains explicitly unresolved.
4. Register generated implementations and authored overrides in one
   `ImplementationCatalog`.
5. Create an `ExecutionConfiguration` selecting composition, execution policy,
   verification policy, bootstrap provider, runtime services, and build target.
6. Plan once. The resulting `ExecutionPlan` and `DetachmentReport` are the only
   runtime selection and dependency authorities.
7. Verify faithful replacements against the oracle. Where representations
   match, compare complete continuation state; where they differ, compare a
   canonical authoritative-state projection.
8. Export only a package-ready release plan, then prove the artifact can start
   and run without development paths or detached components.

Authored alternatives have explicit policy:

- a **faithful replacement** preserves observable authoritative behavior and is
  differentially verified;
- a **non-authoritative enhancement** may replace presentation or host
  integration but must not mutate authoritative gameplay state;
- a **behavioral modification** declares its intended divergence scope and is
  tested as changed behavior.

## Authorities

| Authority | Owns |
|---|---|
| Recovery IR | Canonical recovered static structure and machine facts |
| Stable identities | Backend-independent names for program entities and points |
| `ReplayArtifact` | Immutable events, continuation state, cached boundaries, visits, transfers, verification intervals |
| Execution Atlas | Persistent evidence aggregation, navigation, graph queries, inverse replay coverage, conservative coverage |
| `ImplementationCatalog` | Available generated, interpreted, authored, and region implementations |
| `RuntimeServiceCatalog` | Runtime and product services plus their transitive dependencies |
| `ExecutionConfiguration` | Composition, execution policy, verification policy, bootstrap, and build target |
| `ExecutionPlanner` | Selection, dependency closure, unresolved frontiers, and detachment proof |
| `BootstrapProvider` | The declared source and materialization of initial runtime state |
| Unified player | Execution of an already validated plan through backend adapters |
| Verification | Comparison of oracle and candidate plans; never implementation selection |
| Export | Physical closed-world packaging of one exact release plan |

No player, backend, replay, or Atlas query may silently select an implementation
or fall back outside the plan.

## Detached and standalone

**EXE-detached** means the selected runtime closure does not require the
original executable or original code. A build image may have been produced from
the EXE during development while remaining EXE-detached at runtime.

**Standalone** is stronger: a closed-world export has complete reachable
coverage, no unresolved frontier, an allowed bootstrap and service closure, no
development-only imports or files, and a hermetically validated launcher.
CPU-model, DOS-memory, DOS-services, and dos_re-runtime detachment are separate
milestones; a useful release does not have to achieve all of them at once.

## What dos_re is—and is not

dos_re currently provides an 8086 real-mode runtime and a flat 386 DOS/4GW path,
lifting and recovery tools, replay and snapshot infrastructure, Atlas storage,
mixed-implementation planning, differential verification, and closed-world
export controls. It is a recovery framework, not a universal DOS emulator or a
turnkey decompiler. Hardware models, protected-mode behavior, and automatic ABI
recovery are deliberately incomplete; a port must model the devices and
runtime services its program actually uses. Atlas evidence is conservative:
unobserved execution is never treated as proof of absence.

Verification claims are scoped. dos_re can prove machine-state equivalence when
oracle and candidate share a representation, canonical authoritative-state
equivalence when they do not, declared output differences for enhancements,
and declared divergence scopes for behavioral modifications. It does not claim
universal byte identity for intentionally different representations or output.

## Sanity check

From a supported Python 3.11+ environment:

```bash
python -m pytest -q
python tools/lint.py
python tools/check_undefined.py
python tools/check_doc_links.py
python examples/tiny_frame_game/walkthrough.py
```

The tiny-frame example walks through stable identities, retained structure,
ReplayArtifact creation, Atlas coverage, catalog/configuration planning,
development execution, verification, detachment reporting, and the release
boundary.

## Using dos_re from a port repository

A port owns game-specific recovery facts, configuration, authored
implementations, services, bootstrap materialization, assets, and replay
corpus. dos_re owns the generic authorities above. Keep the framework as an
ordinary dependency or submodule; do not copy its execution planner or invent a
project-local replay, coverage, hook registry, or release authority.

Start with [Getting started](docs/getting_started.md), then follow the
[documentation map](docs/README.md). Tool commands are indexed in
[tools/README.md](tools/README.md).

## Legal boundary and license

dos_re contains framework code, not redistributed proprietary game binaries or
assets. Port repositories must obtain and manage their own lawful inputs.
Generated boot images, snapshots, replays, and recovered assets may contain
original-game material and should not be published without the relevant rights.

The framework is licensed under the [MIT License](LICENSE). Third-party code
with incompatible licensing remains isolated under `graveyard/` for explicit
technical provenance and is not imported by the active package.
