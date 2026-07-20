# dos_re 3.0 glossary

| Term | Definition |
|---|---|
| **Recovered program** | One original program identity plus all known implementations of its functions and regions. |
| **Recovery IR** | Canonical retained static program structure and machine-level recovery facts. |
| **Stable identity** | Backend-independent serialized identity for a program, image, function, region, execution point, boundary, or runtime-code variant. |
| **ReplayArtifact** | The sole persistent deterministic replay format: base continuation state, immutable events, points, metadata, visits, transfers, and derived boundaries. |
| **ReplayPoint** | Stable position on a replay timeline, independent of whether a snapshot is cached there. |
| **CachedBoundary** | Independently restorable continuation state at a ReplayPoint, stored as metadata plus pages changed from the replay base. |
| **ContinuationState** | Backend-specific complete state required for deterministic continuation, including machine, devices, scheduling, timing, and event cursor. |
| **CanonicalState** | Backend-neutral authoritative semantic projection used when raw representations differ. |
| **Execution Atlas** | Persistent evidence and navigation index joining Recovery IR and replay observations. It does not execute or select code. |
| **ProgramCoverage** | Conservative reachable identities, roots, unresolved edges, and evidence identity supplied to planning. |
| **ImplementationCatalog** | The available interpreted, generated, authored, faithful, enhancement, behavioral, and region implementations. |
| **ExecutionConfiguration** | Explicit composition, execution policy, verification policy, bootstrap, services, and build target. |
| **ExecutionPlan** | Immutable selected implementation and dependency closure for one configuration and coverage identity. |
| **DetachmentReport** | Explanation of selected coverage, unresolved frontiers, retained dependencies, bootstrap status, and package readiness. |
| **BootstrapProvider** | Declared producer of initial runtime state and its build/runtime artifacts and capabilities. |
| **RuntimeService** | Host or framework facility selected through the service catalog, not an owner of recovered code. |
| **Backend adapter** | Machine-specific construction and translation beneath the unified player. |
| **Faithful replacement** | Authored implementation intended to preserve original authoritative behavior. |
| **Non-authoritative enhancement** | Optional read-only presentation or host integration with declared output differences. |
| **Behavioral modification** | Explicit intentional authoritative divergence with its own tests and scope. |
| **Region replacement** | One implementation covering a declared set of original identities. |
| **Recovery property** | Per-implementation property such as interpreted, VMless, CPUless, ABI-recovered, DOS-memory-backed, memoryless, or native. |
| **EXE-detached** | Runtime dependency closure excludes the original executable and original code. |
| **Standalone** | Complete closed-world exported product with no unresolved frontier or undeclared development dependency. |
| **Closed-world export** | Physical package of exactly the files and capabilities declared by a package-ready release plan. |
| **Oracle** | Untouched original execution used as the behavioral reference during development and verification. |
| **Hook** | A low-level CPU/runtime interception mechanism. It is not the general name for implementations or overrides. |
