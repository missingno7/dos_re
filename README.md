# dos_re 3.0

dos_re is a modular, evidence-driven workspace for recovering and modifying DOS
games. It keeps the original program available as an oracle during development,
combines static and observed facts under stable identities, and lets generated
and authored implementations coexist at any useful recovery depth.

There is no required decompilation ladder. One function may remain interpreted,
another may use generated instruction-shaped code, a third may have a recovered
ABI, and a renderer may be replaced with authored native code. A project can
stop wherever the result is useful.

## The model

```text
stable program / image / function / region / point identities
                 |
       +---------+----------+----------------+----------------+
       |                    |                |                |
  static recovery      observed runs    manual facts    implementation
  and Recovery IR      and replays      and contracts   descriptors
       |                    |                |                |
       +--------------------+----------------+                |
                            |                                 |
                 Execution Atlas projection                  |
                 navigation + conservative coverage          |
                            |                                 |
                            +------ CoverageSource -----------+
                                              |
                     configuration + services + bootstrap
                                              |
                                      ExecutionPlan
                              +---------------+---------------+
                              |                               |
                  development and verification        closed-world export
```

The arrows are dependency boundaries, not a mandatory workflow. The Atlas can
grow from observed execution or explicit facts before complete static recovery
exists. Planning accepts any conservative `CoverageSource`; it does not require
Atlas storage. Lifting and authored reconstruction can start with a targeted
function and later contribute their facts to the same identity and provenance
model.

## What can be combined

The workspace provides optional, composable operations:

- execute the original EXE through the real-mode or protected-mode runtime;
- retain decoded structure and machine facts in Recovery IR;
- record deterministic `ReplayArtifact`s with reusable continuation boundaries;
- collect observed functions, transfers, runtime-code variants, and failures;
- generate VMless, CPUless, or ABI-recovered implementations;
- author faithful replacements, presentation enhancements, or behavioral
  modifications;
- query the Execution Atlas for relevant code, evidence, replays, and unresolved
  uncertainty;
- verify a selected implementation locally or over a replay interval;
- plan a mixed implementation graph under explicit dependency policy;
- export a closed-world product when its selected coverage and dependencies are
  complete.

VMless, CPUless, ABI-recovered, DOS-memory-backed, memoryless, and native are
properties of individual implementations. They are not player modes or
universal project stages.

## Evidence and navigation

Facts remain owned by the artifact or declaration that produced them:

- Recovery IR retains static decoded structure and its provenance;
- `ReplayArtifact` retains events, continuation state, observed visits and
  transfers, annotations, and derived cached boundaries;
- implementation descriptors retain availability, requirements, digests, and
  verification references;
- explicit fact documents retain reviewed manual claims.

The Execution Atlas is a deterministic, queryable projection over those
sources. It does not decode code, execute replays, select implementations, or
turn absence of observation into proof. Conflicting and unresolved evidence
stays visible.

Replay capture may use a responsive generated or previously replay-backed
override composition. The artifact records that exact capture identity; full
oracle/candidate validation establishes trust, and post-hoc oracle replay can
attach function and transfer evidence independently for Atlas ingestion.
That trust is scoped to the recorded timeline. It does not claim that every
function the replay visits is correct for inputs the corpus has never exercised.

## Execution and release

`ImplementationCatalog` is the available implementation inventory.
`ExecutionConfiguration` selects composition, policy, bootstrap, features,
services, and build target. The planner binds one implementation to each
reachable identity, chooses one root execution carrier, computes the dependency
closure, and reports every known cross-owner boundary. A hook is only the
adapter at such a boundary; selecting a larger provider for both endpoints
collapses it. A long-lived execution region can own an entire subsystem across
semantic replay ticks, leaving adapters only at its declared entries and
exits. The unified player executes that validated plan without fallback
outside it. See [long-lived execution regions](docs/execution_regions.md).

Development may retain the EXE, interpreter, oracle comparison, replay,
instrumentation, and diagnostics. Detached development forbids the EXE,
original-code execution, and interpreter fallback while allowing unresolved
static edges to remain summarized warnings. If execution reaches an actually
missing target, it fails loudly and saves a resumable recovery-frontier
artifact. A release configuration is a closed world:
unknown reachable transfers fail planning, forbidden dependencies are excluded,
bootstrap artifacts are materialized before launch, and export packages only
the selected closure plus a static `execution_plan.json`. A release launcher
does not import the planner and choose implementations again.

EXE-detached, CPU-model-detached, DOS-memory-detached, and dos_re-runtime-
detached are independent properties of a selection. None is the universal
definition of a “finished” recovered game.

## Authored alternatives

- A **faithful replacement** claims equivalent authoritative behavior and needs
  oracle evidence.
- A **non-authoritative enhancement** changes presentation or host integration
  while treating authoritative game state as read-only.
- A **behavioral modification** declares intentional divergence and is tested
  against its own contract.

These categories are metadata on the same generic implementation mechanism.

## Validate the checkout

From Python 3.11 or newer:

```bash
python -m pytest -q
python tools/lint.py
python tools/check_undefined_names.py
python tools/check_doc_links.py
python examples/minimal_adapter/example.py
python examples/tiny_frame_game/walkthrough.py
```

The examples are independent teaching slices. `tiny_frame_game` runs several
capabilities in one convenient order; that order is not a required port
workflow.

## Start a port

```bash
python tools/new_project.py --game mygame --output ../mygame_port
```

A port owns game-specific inputs, facts, implementations, services, assets,
bootstrap materialization, and replay corpus. Keep dos_re as a dependency or
submodule; do not copy its planner or invent project-local replay, coverage, or
implementation-selection authorities.

Read [Getting started](docs/getting_started.md) and the
[documentation map](docs/README.md). Commands are indexed in
[tools/README.md](tools/README.md).

## Scope and license

dos_re is a recovery framework, not a universal DOS emulator or turnkey
decompiler. Hardware behavior and automatic recovery are intentionally
incomplete; add only evidence-backed behavior required by a concrete target.

The repository contains framework code, not proprietary game binaries or
assets. Generated boot images, snapshots, replays, and recovered assets may
contain original-game material and must be handled under the relevant rights.
The framework is licensed under the [MIT License](LICENSE).
