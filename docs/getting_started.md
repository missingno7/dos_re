# Getting started with dos_re 3.0

dos_re is a workspace of composable recovery operations, not a fixed sequence.
Start with the question you need to answer and add only the evidence and
representation depth that helps answer it.

## Validate the framework

```bash
python -m pytest -q
python tools/lint.py
python tools/check_undefined_names.py
python tools/check_doc_links.py
python examples/tiny_frame_game/walkthrough.py
```

## Create a port

```bash
python tools/new_project.py --game mygame --output ../mygame_port
```

The port owns original inputs, recovered facts, replays, generated and authored
implementations, assets, bootstrap materialization, and its configuration
factory. Generated artifacts stay reproducible and are never hand-edited.

## Choose useful operations

These can happen in any evidence-supported order:

### Observe the original

Run the original program through a real-mode or protected-mode adapter. Use
focused probes to identify entries, transfers, effects, runtime-written code,
and failures. Record every claim with its program/image identity and
provenance; an observed path is evidence of presence, not absence elsewhere.

### Retain static structure

Recovery IR stores pinned function/block/instruction structure, transfers,
effects, refusals, runtime-code facts, and provenance. It is valuable for
repeatable corpus analysis, but a targeted scan can be used without first
building whole-program IR.

See [Recovery IR](recovery_ir.md).

### Record deterministic replays

The unified player exposes ReplayArtifact operations:

```bash
python scripts/play.py --record-replay artifacts/replays/gameplay
python scripts/play.py --play-replay artifacts/replays/gameplay
python scripts/play.py --play-replay artifacts/replays/gameplay --replay-continue
```

A replay owns immutable normalized events, stable points, complete continuation
bases, function visits, observed transfers, annotations, and base-relative
cached boundaries. It is the only persistent deterministic replay format.
Recording may use a responsive generated or verified-override composition.
The artifact retains that capture-plan identity; it becomes trusted evidence
only after a complete oracle/candidate validation of the same capture
execution. Function visits and transfers may be attached later by replaying
the immutable inputs on the oracle with explicit observer provenance.

### Create or enrich an Atlas

An Atlas can begin empty and accept whichever evidence exists:

```bash
python tools/atlas.py create artifacts/atlas --program my-game:1
python tools/atlas.py ingest-replay artifacts/atlas artifacts/replays/gameplay --json
python tools/atlas.py ingest-facts artifacts/atlas atlas_facts.json
```

Retained IR is optional:

```bash
python tools/atlas.py ingest-ir artifacts/atlas \
  --ir recovery_ir.json \
  --program my-game:1 \
  --image-label GAME.EXE \
  --image-sha256 SHA256 \
  --root FUNCTION_ID \
  --product-profile game
```

Then query or validate the materialized projection:

```bash
python tools/atlas.py validate artifacts/atlas
python tools/atlas.py show artifacts/atlas FUNCTION_ID
python tools/atlas.py best-replay artifacts/atlas FUNCTION_ID
python tools/atlas.py unresolved artifacts/atlas
python tools/atlas.py coverage artifacts/atlas game
```

Observed-only nodes are valid. Static evidence can enrich them later without
changing their stable identities.

### Add implementations

Register interpreted, generated, or authored alternatives in one
`ImplementationCatalog`. Representation depth is descriptor metadata on a
function or region, not a player mode. Authored alternatives are faithful
replacements, non-authoritative enhancements, or behavioral modifications.

Backend activators marshal the selected semantic implementation to machine or
native state. They do not duplicate its behavior.

### Plan and run

An `ExecutionConfiguration` selects composition, execution policy, verification
policy, runtime/product services, one bootstrap provider, and an optional build
target. `plan_execution` consumes any conservative `CoverageSource` and returns
an immutable `ExecutionPlan` plus `DetachmentReport`.

Development, verification, detached, and release are policy presets over the
same mixed program. They are not recovery levels or separate players.

### Verify a change

Use a focused call verifier for fast local diagnosis or select a stable
ReplayArtifact interval for reusable oracle-versus-candidate evidence. Compare
complete continuation state when representations match, or a shared canonical
authoritative projection when they do not.

Enabling a presentation enhancement excludes only its declared output.
Behavioral modifications are tested against their declared divergence scope.

### Export a closed world

```bash
python tools/export.py --factory project.release:build_export --output dist/game
python tools/verify_export.py --artifact dist/game -- python launch.py
```

Export accepts only a package-ready release plan. Missing coverage, unresolved
transfers, forbidden capabilities, missing bootstrap artifacts, and undeclared
files fail before packaging. A useful project does not need to detach from
every low-level representation unless its selected release policy requires it.

## A practical AI loop

For a targeted change:

- query a function, region, or observed point;
- inspect its static, dynamic, manual, and implementation evidence;
- choose the smallest useful representation or override;
- restore the nearest replay boundary and reproduce the relevant interval;
- verify the change locally and at the appropriate broader scope;
- retain new facts, results, and unresolved questions under the same identities.

Repeat only where further recovery has practical value.
