# dos_re 3.0 documentation

The [repository README](../README.md) introduces the workspace. The active
documents are grouped by concern rather than recovery stage.

## Core contracts

| Document | Scope |
|---|---|
| [Getting started](getting_started.md) | Choosing and combining recovery operations |
| [Architecture](architecture.md) | Authority ownership and dependency direction |
| [Glossary](glossary.md) | Current terminology |
| [Execution planning](execution_planner.md) | Composition, policy, bootstrap, dependency closure, release |
| [Execution Atlas](execution_atlas.md) | Materialized evidence projection and navigation |
| [Replay architecture](replay_architecture.md) | Deterministic replay, continuation state, cached boundaries |
| [Override architecture](override_architecture.md) | Generated and authored implementations |
| [Progressive replacement](progressive_replacement.md) | Carriers, candidate fallback, product features, and hook-boundary collapse |
| [Execution regions](execution_regions.md) | Long-lived ownership, handoff, contextual suppression, replay, and materialization |

## Optional mechanism references

| Document | Scope |
|---|---|
| [Recovery IR](recovery_ir.md) | Retained static evidence |
| [Generated implementations](lifting_design.md) | Literal, CPUless, and ABI-recovered generation |
| [Memory schemas](memory_schema.md) | Optional per-region state ownership and codecs |
| [Backend adapters and verification](hooks_and_verification.md) | Low-level interception and comparison |
| [State mirrors](state_mirrors.md) | Typed views over historical layouts |
| [Performance](performance.md) | Measurement and optimization |
| [Hardware status](hardware_support.md) | Device-model coverage |
| [Enhancements](enhancements.md) | Presentation and host integrations |
| [Agent toolbox](agent_toolbox.md) | Task-to-command index |

Other active files are focused mechanism notes and must follow the same
identity, evidence, catalog, planning, replay, and verification contracts.

Documents under [`history/`](history/) are non-normative records. They contain
retired stage names and commands and are not onboarding guidance.

Runnable examples live under [`examples/`](../examples/). Current commands are
indexed in [`tools/README.md`](../tools/README.md).
