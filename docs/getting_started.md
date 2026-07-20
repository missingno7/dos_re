# Getting started with dos_re 3.0

dos_re recovers one original program into a graph whose regions may have
different implementations. Development keeps the original executable as an
oracle and may use interpreter fallback. Release is the same graph under a
strict policy that requires complete non-EXE coverage and a closed-world
export.

## 1. Validate the framework

```bash
python -m pytest -q
python tools/lint.py
python tools/check_undefined.py
python tools/check_doc_links.py
python examples/tiny_frame_game/walkthrough.py
```

## 2. Create a port repository

```bash
python tools/new_project.py my_game_port
```

The port owns its original inputs, recovered facts, replay corpus, generated
outputs, authored implementations, assets, bootstrap materialization, and
profile factory. Keep generated artifacts reproducible and do not hand-edit
them.

## 3. Retain program structure

Load the executable through the appropriate real-mode or protected-mode
adapter. Recover functions, regions, transfers, data facts, refused areas, and
runtime-code variants into canonical Recovery IR. Name them with the stable
types from `dos_re.identity`; generated Python names and raw addresses are not
cross-artifact identities.

Unknown structure stays explicit. A refused function or unresolved transfer is
evidence, not an invitation to assume the code is unreachable.

## 4. Record deterministic oracle replays

The unified player exposes replay operations:

```bash
python scripts/play.py --record-replay artifacts/replays/gameplay
python scripts/play.py --play-replay artifacts/replays/gameplay
python scripts/play.py --replay-continue artifacts/replays/gameplay
```

A `ReplayArtifact` owns a base continuation state, immutable normalized events,
stable `ReplayPoint`s, function visits, runtime transfers, and base-relative
`CachedBoundary` deltas. It is the only persistent record/replay format.

## 5. Build the Execution Atlas

```bash
python tools/atlas.py build artifacts/atlas --ir recovery_ir.json \
  --program my-game:1 --image-label GAME.EXE --image-sha256 SHA256 \
  --root FUNCTION_ID --product-profile game
python tools/atlas.py ingest-replay artifacts/atlas artifacts/replays/gameplay
python tools/atlas.py validate artifacts/atlas
python tools/atlas.py coverage artifacts/atlas game
```

Recovery IR provides the static skeleton; replays add observed paths, counts,
and reproducible intervals. The Atlas retains provenance and exposes
conservative `ProgramCoverage`. It never selects or installs implementations.

## 6. Add implementations

Register every original, generated, or authored candidate in one
`ImplementationCatalog`. Recovery level is descriptor metadata on a function
or region, not a player mode. Authored alternatives are categorized as faithful
replacements, non-authoritative enhancements, or behavioral modifications.

The semantic implementation uses a backend-neutral override context. Backend
adapters handle registers, stack, memory, arguments, returns, and control
transfer without duplicating authored logic.

## 7. Configure and plan

An `ExecutionConfiguration` selects:

- product roots and implementation composition;
- execution policy;
- verification policy;
- runtime/product services;
- one `BootstrapProvider`;
- build target.

The planner combines Atlas coverage, the implementation catalog, service
closure, bootstrap requirements, and policy into an immutable `ExecutionPlan`
and `DetachmentReport`. The unified player executes this plan; it does not
reconstruct one or fall back around it.

Use `development` while recovery is incomplete, `verification` for oracle
comparison, `detached` to prove non-EXE execution, and `release` for a
package-ready closed world. These are policy presets, not separate players.

## 8. Verify and export

Faithful code is verified over replay intervals against the original oracle.
Compare complete continuation state for compatible representations or a
canonical authoritative projection for detached native state. Enhancements
exclude only their declared presentation output; behavioral modifications use
tests for their declared divergence.

Export only an exact package-ready release plan:

```bash
python tools/export.py --factory project.release:build_export --output dist/game
python tools/verify_export.py --artifact dist/game -- python launch.py
```

Export requires complete reachable coverage, no unresolved frontier, a
materialized release-valid bootstrap, product-safe services, and no forbidden
development dependency. It packages an explicit file closure and validates a
hermetic cold start.

Continue with [execution planning](execution_planner.md), [override
architecture](override_architecture.md), [the Atlas](execution_atlas.md), and
[replay architecture](replay_architecture.md).
