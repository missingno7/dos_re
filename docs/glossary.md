# dos_re 3.0 glossary

| Term | Definition |
|---|---|
| **Recovered program** | One original program identity plus the evidence and available implementations known for its functions and regions. It does not imply a particular representation depth. |
| **Recovery operation** | An optional observation, analysis, generation, verification, or rewriting activity. Operations are composable, not mandatory stages. |
| **Recovery IR** | Deterministic retained static structure and machine-level facts with provenance. |
| **Stable identity** | Backend-independent serialized identity for a program, image, function, region, execution point, boundary, or runtime-code variant. |
| **Evidence source** | The artifact or declaration that owns a fact, such as Recovery IR, ReplayArtifact, an explicit fact document, or an implementation descriptor. |
| **Execution Atlas** | Deterministic materialized projection and query index over cited evidence sources. It does not execute, decode, or select code. |
| **ReplayArtifact** | Sole persistent deterministic replay format: base continuation state, immutable events, points, metadata, visits, transfers, annotations, and derived boundaries. |
| **ReplayExecutionIdentity** | Immutable identity of one oracle or candidate execution composition used to validate replay continuation caches. It is distinct from execution-policy profiles. |
| **ReplayPoint** | Stable position on a replay timeline whether or not a snapshot is cached there. |
| **CachedBoundary** | Independently restorable continuation at a ReplayPoint, stored as metadata plus pages changed from the replay base. |
| **ContinuationState** | Backend-specific complete state required for deterministic continuation, including devices, scheduling, timing, and event cursor. |
| **CanonicalState** | Backend-neutral authoritative projection used when oracle and candidate representations differ. |
| **ProgramCoverage** | Conservative roots, reachable identities, unresolved edges, and evidence identity supplied to planning. |
| **ImplementationCatalog** | Available interpreted, generated, authored, and region implementations. |
| **Implementation property** | Per-implementation property such as interpreted, VMless, CPUless, ABI-recovered, DOS-memory-backed, memoryless, or native. |
| **Override** | Authored implementation selected from the same catalog as generated and original implementations. |
| **Hook** | Low-level CPU/runtime interception used by an adapter or service; not a general implementation registry. |
| **ExecutionConfiguration** | Explicit composition, execution policy, verification policy, bootstrap, services, and build target. |
| **Policy profile** | Development, verification, detached, or release preset for allowed capabilities. It is not a recovery level. |
| **ExecutionPlan** | Immutable implementation binding and dependency closure for one configuration and coverage identity. |
| **DetachmentReport** | Explanation of coverage, unresolved frontiers, retained dependencies, bootstrap status, and package readiness. |
| **BootstrapProvider** | Declared source of initial runtime state and its build/runtime artifacts and capabilities. |
| **RuntimeService** | Selected host or framework facility that does not own recovered code. |
| **Backend adapter** | Machine-specific construction, state translation, and implementation activation beneath the unified player. |
| **Faithful replacement** | Authored implementation claiming equivalent authoritative behavior. |
| **Non-authoritative enhancement** | Optional presentation or host integration that treats gameplay state as read-only. |
| **Behavioral modification** | Explicit intentional authoritative divergence with its own scope and tests. |
| **Region replacement** | One implementation covering a declared set of original identities and boundaries. |
| **EXE-detached** | Selected runtime closure excludes the original executable and original code. |
| **Standalone** | Closed-world exported product with no unresolved reachable frontier or undeclared dependency. |
| **Closed-world export** | Physical package containing exactly the files and capabilities declared by a package-ready plan. |
| **Oracle** | Untouched original execution used as behavioral reference during development and verification. |
