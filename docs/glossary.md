# dos_re 3.0 glossary

| Term | Definition |
|---|---|
| **Recovered program** | One original program identity plus the evidence and available implementations known for its functions and regions. It does not imply a particular representation depth. |
| **Recovery operation** | An optional observation, analysis, generation, verification, or rewriting activity. Operations are composable, not mandatory stages. |
| **Recovery IR** | Deterministic retained static structure and machine-level facts with provenance. |
| **Stable identity** | Backend-independent serialized identity for a program, image, function, region, execution point, boundary, or runtime-code variant. |
| **Evidence source** | The artifact or declaration that owns a fact, such as Recovery IR, ReplayArtifact, an explicit fact document, or an implementation descriptor. |
| **Execution Atlas** | Deterministic materialized projection and query index over cited evidence sources. It does not execute, decode, or select code. |
| **ReplayArtifact** | Sole persistent deterministic replay format: one portable immutable behavioral timeline plus profile-local base continuations, points, visits, transfers, annotations, and derived boundaries. |
| **Capture profile** | Exact execution composition used to record a ReplayArtifact's immutable input stream. It may be an oracle or candidate and is provenance, not the playback authority or a correctness claim. |
| **Trusted replay** | An oracle-captured artifact, or a candidate-captured artifact whose complete finite timeline has passed equivalent oracle/candidate validation for the same capture execution. Trust is scoped to that event stream, not every possible function input. |
| **Verification claim** | Reproducible finite result binding an implementation digest, oracle and candidate execution identities, ReplayArtifact, exact interval, and comparison schema. A passing claim is not universal correctness. |
| **Counterexample** | Persisted replay boundary and mismatch showing one verification claim fails; it becomes a focused regression for the next implementation digest. |
| **ReplayExecutionIdentity** | Immutable identity of one oracle or candidate execution composition used to validate replay continuation caches. It is distinct from execution-policy profiles. |
| **ReplayPoint** | Stable identity and ordering position on a replay timeline whether or not a snapshot is cached there. |
| **ReplayPointCoordinate** | Schema-tagged backend-neutral stop coordinate for a ReplayPoint, such as guest instruction count, simulation tick, or presentation fence. Host backend-dispatch counts are forbidden. |
| **CachedBoundary** | Independently restorable continuation at a ReplayPoint, stored as metadata plus pages changed from the replay base. |
| **ContinuationState** | Backend-specific complete state required for deterministic continuation, including devices, scheduling, timing, and event cursor. |
| **CanonicalState** | Backend-neutral authoritative projection used when oracle and candidate representations differ. |
| **ProgramCoverage** | Conservative roots, reachable identities, known resolved/observed transfers, unresolved edges, and evidence identity supplied to planning. |
| **ImplementationCatalog** | Available interpreted, generated, authored, and region implementations. |
| **Implementation property** | Per-implementation property such as interpreted, VMless, CPUless, ABI-recovered, DOS-memory-backed, memoryless, or native. |
| **Override** | Authored implementation selected from the same catalog as generated and original implementations. |
| **Hook** | Temporary low-level interception at a selected cross-owner edge. It collapses when one implementation owns both endpoints; it is not a registry or permanent native architecture. |
| **Execution carrier** | Calling and state mechanism surrounding a selected root provider, such as interpreted CPU, generated VMless CPU, generated CPUless, DOS memory, or native state. It is not a recovery level. |
| **Backend adapter** | Stable bridge that marshals one semantic implementation contract through one execution carrier. |
| **Feature** | Planned optional presentation, behavioral, or instrumentation policy. It does not own recovered program targets. |
| **ExecutionConfiguration** | Explicit composition, execution policy, verification policy, bootstrap, features, services, and build target. |
| **Policy profile** | Development, verification, detached, or release preset for allowed capabilities. It is not a recovery level. |
| **Closure policy** | Permissive, observed-corpus, or strict handling of uncertainty in the resolved selected implementation graph. It does not control EXE detachment. |
| **Fallback policy** | Whether runtime execution may leave the selected implementation graph. Detached and release execution forbid fallback. |
| **ExecutionPlan** | Immutable implementation binding and dependency closure for one configuration and coverage identity. |
| **DetachmentReport** | Explanation of coverage, unresolved frontiers, retained dependencies, bootstrap status, and package readiness. |
| **Recovery frontier** | An actually reached missing runtime target, persisted with continuation state, execution plan, replay position, and Atlas path for direct repair and reproduction. |
| **BootstrapProvider** | Declared source of initial runtime state and its build/runtime artifacts and capabilities. |
| **RuntimeService** | Selected host or framework facility that does not own recovered code. |
| **Faithful replacement** | Authored implementation claiming equivalent authoritative behavior. |
| **Non-authoritative enhancement** | Optional presentation or host integration that treats gameplay state as read-only. |
| **Behavioral modification** | Explicit intentional authoritative divergence with its own scope and tests. |
| **Region replacement** | One implementation covering a declared set of original identities and boundaries. |
| **EXE-detached** | Selected runtime closure excludes the original executable and original code. |
| **Standalone** | Closed-world exported product with no unresolved reachable frontier or undeclared dependency. |
| **Closed-world export** | Physical package containing exactly the files and capabilities declared by a package-ready plan. |
| **Oracle** | Untouched original execution used as behavioral reference during development and verification. |
