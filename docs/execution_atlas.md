# Execution Atlas

The Execution Atlas is a persistent, queryable projection of what is known
about a recovered program. It joins independently owned evidence under stable
identities so an AI or tool can navigate code, observed execution, replay
coverage, runtime variants, implementation choices, and unresolved uncertainty.

It is not a mandatory analysis stage. An Atlas can begin with a replay, a
manual entry point, runtime transfers, or retained Recovery IR, and can be
enriched in any order.

## Authority boundary

The Atlas:

- cites static Recovery IR without decoding instructions;
- cites ReplayArtifact visits, transfers, annotations, and runtime variants
  without owning events or continuation state;
- cites explicit identity-based fact documents;
- materializes deterministic graph and inverse-replay indexes;
- implements the planner’s conservative `CoverageSource` protocol;
- joins a supplied catalog or plan for queries without storing callables or
  selecting implementations.

The source artifact or declaration remains authoritative. Files under
`atlas/sources/` are normalized materializations with source identities and
digests, not a competing fact format. Rebuilding indexes cannot mutate the
source replay, IR, or manual declaration.

## Stable identities

`dos_re.identity` provides immutable canonical identities:

- `ProgramIdentity`;
- `ImageIdentity`;
- `FunctionIdentity`;
- `RegionIdentity`;
- `ExecutionPointIdentity`;
- `RuntimeCodeSlotIdentity` and `RuntimeCodeVariantIdentity`;
- `BoundaryIdentity`.

Image identity includes a content digest, so overlays and executables at the
same address do not collide. Runtime-written code uses a stable slot plus a
content-hashed variant. Symbols and generated filenames are display metadata.

## Evidence sources

### Observed execution and replay

Replay ingestion accepts oracle-owned function visits and actual observed
transfers. It can create observed-only function or execution-point nodes when
no static function boundary exists. It also retains:

- invocation count, first entry, and final completed last exit;
- nearest cached boundaries;
- divergence, crash, verification, and manual point annotations;
- encountered runtime-code variants;
- incomplete-function markers;
- replay and execution identity digests.

Consecutive visits are not assumed to be edges. Observers report actual
transfers. An observed indirect target extends the graph but does not prove the
site complete.

### Recovery IR

IR ingestion adds decoded functions, blocks, direct transfers, platform
boundaries, refusals, effects, runtime slots, and unresolved indirect sites.
Direct targets missing from the document remain execution-point frontiers.

IR is optional. Importing it later enriches existing observed nodes with static
metadata and provenance.

### Explicit facts

Reviewed entry points, boundaries, regions, recovered contracts, or transfer
facts can be ingested from a JSON document with its own stable evidence-source
identity. Conflicting evidence remains visible; a manual fact does not erase
the sources it supersedes or disputes.

```json
{
  "identity": "reviewed-facts:2026-07-20",
  "nodes": [
    {"id": "STABLE_ID", "kind": "function", "label": "display name"}
  ],
  "edges": []
}
```

### Implementations and plans

`implementation_view` and `region_view` join a supplied
`ImplementationCatalog` and optional `ExecutionPlan` at query time. They expose
properties, requirements, digests, verification references, and selected
status without making the Atlas an implementation inventory.

## Graph and uncertainty

Initial node kinds include program, image, function, execution point, region,
runtime-code slot/variant, and external/platform boundary.

Edges include static and observed calls/jumps/transfers, interrupts,
containment, region coverage, and runtime-variant relationships. Each edge
carries source identities, status, observation count, and metadata.

When two sources claim different labels or metadata for the same stable
identity, materialization records source-attributed `conflicts` instead of
silently letting import order choose a fact. Non-conflicting claims remain in
the ordinary metadata view, so callers must resolve visible conflicts before
using the disputed field as a proof input.

Unresolved transfers are first-class. Static reachability is never removed for
lack of observation, and dynamic absence never marks a node unreachable.

## Replay navigation

For a covered function or region, Atlas queries can identify:

- which replays visit it;
- total invocation count;
- first-entry and final-last-exit points;
- whether the interval is complete;
- cached boundaries at or before its endpoints;
- relevant runtime variants and annotations.

`best_replay` orders choices deterministically by interval completeness, exact
entry cache, interval size, invocation coverage, and replay identity. The
boundary remains part of the original ReplayArtifact; the Atlas creates no
suffix replay or reproduction artifact.

## Conservative coverage

`coverage_for(product_profile)` walks known resolved and observed function
edges from declared roots and returns `ProgramCoverage` with:

- stable roots;
- conservatively reachable identities;
- reachable unresolved frontiers;
- a digest of the contributing materialized evidence.

Planning can consume this result or another valid `CoverageSource`. Release
planning rejects reachable unresolved control flow; Atlas observation alone
cannot close it.

## Storage and regeneration

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

Named source materializations are independently replaceable. Indexes rebuild
deterministically from all current sources. JSON ordering, source digests, and
index validation make unchanged inputs byte-stable and stale materializations
detectable.

Program mismatch, unsupported schemas, changed source digests, and stale replay
execution identities fail loudly. A runtime-code byte change creates another
variant identity rather than overwriting evidence.

## CLI

Create an empty projection:

```bash
python tools/atlas.py create artifacts/atlas --program my-game:1
```

Add any available sources:

```bash
python tools/atlas.py ingest-replay artifacts/atlas artifacts/replays/gameplay
python tools/atlas.py ingest-facts artifacts/atlas atlas_facts.json
python tools/atlas.py ingest-ir artifacts/atlas \
  --ir recovery_ir.json \
  --program my-game:1 \
  --image-label GAME.EXE \
  --image-sha256 SHA256 \
  --root FUNCTION_ID \
  --product-profile game
```

Query it:

```bash
python tools/atlas.py validate artifacts/atlas
python tools/atlas.py show artifacts/atlas FUNCTION_ID
python tools/atlas.py callers artifacts/atlas FUNCTION_ID
python tools/atlas.py callees artifacts/atlas FUNCTION_ID
python tools/atlas.py best-replay artifacts/atlas FUNCTION_ID
python tools/atlas.py unresolved artifacts/atlas
python tools/atlas.py path artifacts/atlas SOURCE_ID TARGET_ID
python tools/atlas.py coverage artifacts/atlas game
```

All commands support stable identities; query output cites contributing
evidence.
