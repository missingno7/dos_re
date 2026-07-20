# Execution Atlas

Status: dos_re 3.0 workstream architecture and first-slice contract.

The Execution Atlas is the persistent navigation and coverage map of the
recovered original program. It combines independently replaceable evidence
sources without becoming an authority for code decoding, replay persistence,
implementation selection, or runtime dispatch.

## Repository survey

The first implementation is grounded in these existing authorities:

- `dos_re.lift.irgen_core` writes Recovery IR v0. Every function record,
  including refused records, owns its blocks, pinned instruction bytes, direct
  near/far calls, exits, interrupts, platform effects, SMC verdicts, facts, and
  provenance. `dos_re.lift.ir` is its reader.
- `dos_re.replay.ReplayArtifact` owns immutable events, execution profiles,
  stable `ReplayPoint`s, complete continuation bases, cached boundary deltas,
  point annotations, and `FunctionVisit` intervals.
- `dos_re.execution.ProgramCoverage` and `CoverageSource` are the planner
  boundary. `ExecutionPlan` remains the implementation-selection and
  dependency-policy authority.
- `ImplementationCatalog` owns implementation descriptors, categories,
  capabilities, services, digests, targets, and optional regions. Callable
  bodies and activators never enter an Atlas artifact.
- `RuntimeCodeSlot` and `RuntimeCodeVariant` own polyvariant runtime-code
  semantics. The Atlas gives them stable identities and links; it does not
  create another SMC manifest.
- generated lift manifests are implementation evidence. Their module and
  symbol names are display metadata, never original-program identity.

The survey found no shared identity type. Recovery IR keys use local
`CS:IP`, SkyRoads currently formats `skyroads:1.0:function:CS:IP`, replay
visits accept any string, and implementation targets use the project string.
A local address is insufficient for multiple executables, overlays, or
co-located runtime images. Shared identity types therefore precede the Atlas.

The survey also found that `FunctionVisit` records complete aggregate
intervals, but ReplayArtifact has no canonical observed-transfer section.
Observed edges must be added to replay-owned evidence from actual runtime
transfers; the Atlas must not infer them from visit order.

## Authority boundaries

The Atlas:

- imports and cites Recovery IR; it never decodes instructions;
- imports and cites ReplayArtifact evidence; it never stores input events,
  continuation snapshots, or replay clocks;
- calculates `ProgramCoverage`; it never chooses an implementation;
- joins `ImplementationDescriptor`s in memory; it never stores callables or
  activators;
- maps runtime-code slots and variants; it never identifies live bytes through
  a conflicting mechanism;
- persists normalized evidence and generated indexes; it never mutates the
  original evidence source.

Recovery IR, replay, catalog, planner, and runtime dispatch remain independent
sources of truth.

## Stable identity model

`dos_re.identity` defines validated immutable identities with canonical string
forms. Components other than the already-established project program key use a
restricted, percent-encoded representation.

- `ProgramIdentity`: one recovered program, for example `skyroads:1.0`.
- `ImageIdentity`: program + image label + hash algorithm/content digest. An
  EXE, COM, overlay, module, or resolved loaded image gets a distinct identity.
- `FunctionIdentity`: image + address space + local entry address.
- `RegionIdentity`: program + stable region label.
- `ExecutionPointIdentity`: image + address space + local address.
- `RuntimeCodeSlotIdentity`: image + address space + stable slot address.
- `RuntimeCodeVariantIdentity`: slot + content hash.
- `BoundaryIdentity`: program + boundary namespace + stable boundary label.

Real-mode addresses use normalized `ssss:oooo`; protected-mode or flat
addresses use a declared address-space name and fixed-width hexadecimal local
address. Identity never depends on a symbol or generated Python filename.

Normal code is identified by program, immutable image identity, and local
address. Polyvariant code is identified by stable slot plus content-hash
variant. Two images may occupy the same address without collision.

The first slice does not rewrite Recovery IR keys. Importers translate their
document-local keys exactly once through the shared identity API. Projects use
the same API for replay visits, implementation targets, roots, and runtime
instrumentation.

## Multiple images and launch profiles

An Atlas manifest names one `ProgramIdentity` and any number of images.
Independent static source files are keyed by `ImageIdentity`. Product profiles
declare roots by stable identity. The same static graph can therefore support
different executables, graphics modules, overlays, or launch paths without
inventing separate Atlases.

Image containment and control-flow reachability are separate. Selecting a
program or region node does not imply every contained function is reachable.
Each product profile explicitly names control roots; dynamic evidence may add
targets but never remove static reachability.

## Nodes, edges, and unresolved transfers

Initial node kinds are:

- program, image, function, execution point;
- region;
- runtime-code slot and runtime-code variant;
- external/platform boundary.

Function metadata cites its IR source digest and document locator, address,
signature, liftability, refusal records, exits, effects, symbol metadata, and
SMC status.

Initial directed edge kinds are:

- direct near call, direct far call;
- cross-node direct jump;
- observed call, jump, interrupt, return, or transfer;
- interrupt/platform boundary;
- containment and region coverage (not control-flow edges).

Every edge carries one or more evidence records. Static and observed support
can coexist on the same logical edge.

An unresolved transfer is a first-class record containing:

- stable source node and execution point;
- transfer kind;
- candidate targets, if any;
- resolution status and completeness;
- source evidence and provenance.

An indirect target observed at runtime adds an observed edge but does not mark
the site complete. Completeness requires static proof, an explicit recovery
fact, or another named evidence source.

## Evidence and provenance

Evidence is appendable normalized data, not a confidence scalar. Kinds include:

- `recovery-ir`, `static-direct`, `static-inferred`;
- `codemap-observed`, `replay-oracle-observed`,
  `replay-candidate-observed`, `replay-verified`;
- `manually-declared`, `recovery-fact`;
- `runtime-code-variant`, `unresolved`, `invalidated`, `superseded`.

Records cite applicable program/image identities, IR schema and digest,
toolchain identity, replay identity, execution-profile identity digest,
execution-plan digest, implementation digest, runtime-code signature, or
manual-fact identity. Conflicting evidence remains visible.

Absolute paths, timestamps, Python reprs, and ambient working-directory state
are forbidden from persisted Atlas data. Recovery IR provenance is retained as
source metadata, but path-like values are normalized to basenames or explicit
project-relative references before persistence.

## Static IR import

The importer reads `load_recovery_ir` and hashes the exact retained document.
It does not call a scanner or decoder.

For every function record it:

1. creates a function node, including refused and interpreted-only records;
2. imports direct near/far call edges;
3. imports cross-function direct jumps visible in pinned records;
4. creates platform-boundary edges for interrupts and effect summaries;
5. creates an unresolved site for every indirect call/jump;
6. imports declared boundary/dispatch entry points;
7. imports exits, refusals, signatures, SMC verdicts, and source locators.

A direct target absent from the IR becomes an execution-point frontier rather
than disappearing. Runtime-code slots supplied by the project are imported
through the existing `RuntimeCodeSlot` records.

## Replay evidence collection and ingestion

ReplayArtifact has one optional, versioned execution-evidence section. It
contains actual observed transfers with stable endpoints, kind, count, and
first/last points, plus encountered runtime-code variants and incomplete
functions. Observed nodes come from the transfer endpoints and function visits.
`FunctionVisit` remains the nested/recursive interval authority.

Canonical structural evidence comes from an oracle `ExecutionProfile`.
Candidate profiles may add verification and diagnostic annotations but cannot
redefine original-program structure.

An oracle observer may collect the section during recording or reconstruct it
by replaying the immutable event stream. Both paths use the same
`ReplayExecutionEvidence` semantics. Runtime observers report actual transfer
events; the Atlas never derives an edge merely from consecutive visits.

Dynamic nodes and edges absent from Recovery IR are accepted and explicitly
marked observed. Dynamic absence never marks a node unreachable.

## Inverse replay index and best replay

Each function or executable region can return replay coverage records containing:

- replay identity and oracle profile identity digest;
- invocation count;
- first-entry and final completed last-exit;
- interval completeness;
- nearest reusable boundary at or before the start;
- verification/divergence/crash annotations;
- runtime-code variants;
- replay and plan identity digests.

The first slice stores the aggregate first-entry-to-final-last-exit interval.
The schema reserves individual intervals for later.

Best-replay ordering is deterministic: complete interval, exact cached entry,
smallest useful interval, greatest invocation coverage, then replay identity.
All imported structural replay evidence is oracle-owned. Candidate-specific
verification ranking can be added later without changing replay identity.

## Planner integration

`ExecutionAtlas.coverage_for(product_profile)` implements `CoverageSource`.
It walks control-flow edges conservatively from that profile's declared roots.
It returns roots, reachable stable identities, reachable unresolved sites, and
a digest of the normalized evidence that contributed to the result.

Static edges are never removed for lack of observation. Observed targets extend
the graph. An observed target does not close an unresolved site. Detached and
release planning therefore continue to fail on reachable incomplete control
flow. `ExecutionPlan` still selects implementations and enforces policy.

## Implementation and region joins

Queries join Atlas identities to supplied `ImplementationCatalog` descriptors.
The joined view exposes origin, category, properties, required capabilities,
services, assets, digest, verification evidence, region, and selected status
for an optional supplied plan.

The artifact never stores semantic callables, activators, or preference order.

A region view contains its covered original nodes, external entry/exit edges,
declared authoritative state, required services/capabilities, verification
boundary, and replay evidence. Internal original nodes remain visible as
historical structure after a larger replacement covers them.

## Runtime code

Projects pass their existing `RuntimeCodeSlot` objects to the importer. Slot
identity is address-stable inside an image. Each variant identity contains the
existing signature hash. Installer/staticization evidence and the existing
source target are metadata. Unknown variants remain unresolved frontiers.

No Atlas-specific byte matcher or SMC manifest is introduced.

## Deterministic storage and regeneration

The first storage layout is:

```text
atlas/
    manifest.json
    sources/
        static-<source-key>.json
        replay-<source-key>.json
        manual-<source-key>.json
    indexes/
        graph.json
        replay_coverage.json
```

Source files are independently replaceable. Static import replaces only its
named image source; replay ingestion replaces only that replay source; manual
facts are separate. Materialized indexes are rebuilt from all named sources.

JSON uses sorted keys, two-space indentation, stable list ordering, UTF-8, and
one trailing newline. Unchanged inputs produce byte-identical files. Index
validation rematerializes in memory and compares normalized content.

## Staleness

- program mismatch: reject the source;
- image or IR digest change: replace that named static source and rematerialize;
- unsupported IR or Atlas schema: reject and require regeneration;
- replay identity/profile/base change: reject or replace that replay source;
- plan/implementation digest change: recompute joined views; persisted replay
  verification referring to the old digest remains explicit stale evidence;
- runtime variant signature change: distinct variant identity, never overwrite;
- tool analysis version change: regenerate affected named sources.

Manifest and `ProgramCoverage.evidence_identity` are hashes of normalized
relevant inputs. Stale evidence is never silently consumed.

## Python API and CLI

`dos_re.atlas` provides:

- identity resolution and node lookup;
- node metadata/provenance;
- callers, callees, and static/observed edge filtering;
- unresolved sites;
- inverse replay coverage and best-replay interval;
- implementation/region joins;
- `coverage_for`;
- referential/deterministic validation.

One CLI, `tools/atlas.py`, provides `create`, `build`, `ingest-replay`, `validate`,
`show`, `callers`, `callees`, `coverage`, `best-replay`, `unresolved`, `path`,
and deterministic JSON or concise text output.

## Test strategy

- identity collision and round-trip tests across programs/images/address spaces;
- all-function IR import, including refused records;
- direct/far/cross-jump edges and unresolved indirect sites;
- runtime slot/variant import;
- replay evidence validation and inverse indexing;
- best-replay deterministic ordering;
- dynamic graph extension without false completeness;
- conservative product-profile reachability and planner failure on unresolved
  reachable sites;
- implementation and multi-node region joins;
- stale source rejection;
- byte-identical double regeneration and index rematerialization;
- CLI JSON smoke tests;
- one real game pilot.

## Phased migration

1. Shared identities, normalized storage, IR importer, replay visit and actual
   transfer evidence, graph queries, product-profile coverage adapter,
   implementation/region joins, runtime-code import, validation, and CLI.
2. SkyRoads retained IR and committed static-plus-oracle-replay Atlas pilot.
3. Individual invocation intervals, richer verification joins, and additional
   manual-fact authoring tools.
4. Future visualization and AI query consumers.

Each phase preserves the same artifact and replaces named evidence sources.

## Non-goals

The first slice does not:

- decode or rescan executable code;
- reconstruct perfect source functions;
- select or dispatch implementations;
- create replay, snapshot, suffix, or reproduction formats;
- infer transfers from visit order;
- prove indirect-target completeness from observation;
- erase original nodes covered by replacements;
- add graph drawing, web UI, t-SNE, or layout algorithms.
