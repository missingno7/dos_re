# Architecture and authority boundaries

dos_re consists of optional recovery operations connected by one acyclic
authority graph. “Acyclic” describes dependency direction; it does not require
a game to use every component.

```text
                         stable identities
                +-------------+-------------+
                |             |             |
          Recovery IR   ReplayArtifact   explicit facts
                |             |             |
                +-------------+-------------+
                              |
                    Execution Atlas projection
                    navigation / CoverageSource
                              |
     ImplementationCatalog + RuntimeServiceCatalog + BootstrapProvider
                              |
                    ExecutionConfiguration
                              |
                ExecutionPlan + DetachmentReport
                    /          |          \
                 player   verification   export
                    |
              backend adapters
```

Recovery IR, replay, and explicit facts can exist independently. The Atlas
stores normalized materializations and deterministic indexes so they can be
queried together; the cited source artifacts remain the evidence authorities.
Planning can also consume a conservative `ProgramCoverage` directly.

## Module ownership

| Modules | Authority |
|---|---|
| `identity.py` | Stable serialized identities |
| `lift/ir.py` | Recovery IR loading and re-elaboration |
| `replay.py`, replay input adapters, snapshot modules | ReplayArtifact and backend-specific capture/apply mechanics |
| `atlas.py` | Materialized evidence projection, graph queries, inverse replay index |
| `execution.py` | Coverage, catalogs, configuration, bootstrap declarations, planning, detachment report |
| `bootstrap_runtime.py` | Validation and consumption of planned bootstrap artifacts |
| `player.py` | One launch and replay command interface |
| `pm_backend.py` and port adapters | Backend construction and machine-specific I/O |
| `verification.py`, `frame_verify.py`, replay verification | Oracle/candidate comparison |
| `export.py` | Closed-world packaging |
| `hooks.py`, `interrupts.py` | Low-level runtime interception, not implementation selection |

`RuntimeServiceCatalog` is separate from `ImplementationCatalog`: a display,
input, or diagnostic service does not own recovered program code.

## Invariants

1. Stable identities cross artifact boundaries; generated names and bare
   addresses do not.
2. Every evidence record cites its producer and provenance.
3. Atlas queries preserve conflicts and unresolved uncertainty.
4. Observation extends knowledge but never proves an unseen edge absent.
5. `ImplementationCatalog` is the only available-implementation inventory.
6. `ExecutionConfiguration` is the only composition and policy input.
7. A validated plan binds exactly one owner to every reachable authoritative
   identity.
8. Player and backend adapters cannot select or silently fall back.
9. Verification compares selected plans without changing them.
10. Bootstrap is declared and validated before runtime construction.
11. Export includes only the exact file and dependency closure of its plan.

## Backend boundary

Real mode, protected mode, generated execution, and detached native execution
may differ in construction, event application, continuation capture, and
canonical projection. They share identity, catalog, planning, replay,
verification, and export contracts.

An authored semantic body is not duplicated by backend. A selected activator
may translate registers, stack state, DOS memory, native values, arguments,
returns, timing, and control transfer for one backend.

## Dependency guards

- `identity.py` must not import replay, Atlas, execution, player, or backends.
- replay must not import Atlas or implementation selection;
- Atlas must not import player or backend dispatch;
- `execution.py` stays backend-neutral;
- player and export consume plans but cannot create alternate selection
  authorities.

Historical designs are isolated under [`history/`](history/).
