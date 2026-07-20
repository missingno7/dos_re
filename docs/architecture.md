# Architecture and authority boundaries

dos_re 3.0 has one acyclic lifecycle:

```text
identity <- Recovery IR
    ^           |
    |           +------> Atlas ------> ProgramCoverage
    |                      ^                  |
    +------ ReplayArtifact-+                  v
                                      ImplementationCatalog
                                                |
RuntimeServiceCatalog + BootstrapProvider ------+
                                                v
                                  ExecutionConfiguration
                                                |
                                                v
                              ExecutionPlan + DetachmentReport
                                  |             |          |
                               player      verification   export
                                  |
                           backend adapters
```

Arrows mean “is consumed by.” Replay and Recovery IR depend only on stable
identities, not on Atlas or planning. Atlas may join externally supplied plan
and catalog records for navigation, but it does not own them. The planner
consumes the `CoverageSource` protocol and has no Atlas storage or CPU-backend
dependency.

## Module ownership

| Modules | Authority |
|---|---|
| `identity.py` | Stable serialized identities |
| `lift/ir.py`, `recovery_ir.py` | Canonical retained recovery structure |
| `replay.py`, `replay_input.py`, `pm_replay_input.py`, snapshot modules | ReplayArtifact and backend-specific capture/apply mechanics |
| `atlas.py` | Persistent evidence aggregation, graph queries, inverse replay coverage |
| `execution.py` | ProgramCoverage, catalogs, configuration, planning, detachment report |
| `overrides.py` | Authored implementation metadata, context boundary, verification contract |
| `bootstrap.py`, `bootstrap_runtime.py` | Bootstrap providers and consumption of planned artifacts |
| `player.py` | One launch lifecycle |
| `pm_backend.py` and project adapters | Backend construction and machine-specific I/O |
| `verify.py`, `frame_verify.py`, replay verification modules | Oracle/candidate comparison |
| `export.py` | Explicit closed-world packaging |
| `hooks.py`, `interrupts.py` | Low-level runtime interception details, not implementation catalogs |

`RuntimeServiceCatalog` and `ImplementationCatalog` are separate because a
service such as display or host input is not an owner of recovered program
code. Framework interrupt and host-service interceptors likewise remain backend
machinery rather than authored overrides.

## Hard invariants

1. Stable identity values cross artifact boundaries; generated names and raw
   addresses do not.
2. Recovery IR owns static facts; replay owns recorded dynamic facts; Atlas
   owns their normalized evidence join.
3. Atlas coverage is conservative. Unknown edges remain unresolved.
4. `ImplementationCatalog` is the only available-implementation authority.
5. `ExecutionConfiguration` is the only composition and policy authority.
6. `ExecutionPlan` is immutable and binds exactly one implementation per
   reachable identity.
7. The player and backend adapters consume a plan. They cannot select or
   silently fall back.
8. Verification compares plans but never changes selection.
9. Bootstrap is declared and validated before runtime construction.
10. Export consumes one exact release plan and includes only its explicit file
    and dependency closure.

## Backend boundary

Real mode and protected mode differ in machine construction, event
normalization, stable-point observation, and continuation-state projection.
They share the player, ReplayArtifact format, identity model, Atlas, planning,
verification policies, and export rules. `pm_backend.py` is therefore a backend
adapter, not a second player.

An authored override receives a canonical context. A wrapper may translate
registers, stack state, DOS memory, native state, arguments, returns, and
control flow for a particular backend; the semantic implementation is not
duplicated.

## Dependency direction guard

Low-level identity and Recovery IR modules must not import replay, Atlas,
execution planning, player, or backend modules. Replay must not import Atlas or
execution selection. Atlas must not import player or backend dispatch.
`execution.py` stays backend-neutral. Export and player may consume a plan but
may not create an alternate selection authority.

Historical architecture records live under [`history/`](history/) and are
non-normative.
