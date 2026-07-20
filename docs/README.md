# dos_re 3.0 documentation

The [repository README](../README.md) is the human introduction. The active
reading path is:

1. [Getting started](getting_started.md) — the end-to-end port workflow.
2. [Execution planning](execution_planner.md) — configuration, dependency
   closure, bootstrap, detachment, and release.
3. [Override architecture](override_architecture.md) — generated and authored
   implementations and their verification policies.
4. [Execution Atlas](execution_atlas.md) — retained evidence, navigation, and
   conservative coverage.
5. [Replay architecture](replay_architecture.md) — deterministic replay,
   continuation state, cached boundaries, and verification intervals.
6. [Architecture](architecture.md) — module ownership and dependency rules.
7. [Glossary](glossary.md) and [agent toolbox](agent_toolbox.md).

## Specialized mechanism references

| Document | Scope |
|---|---|
| [Recovery IR](recovery_ir.md) | Canonical retained static recovery structure |
| [Lifting design](lifting_design.md) | Generated implementation pipeline |
| [Hooks and verification](hooks_and_verification.md) | Low-level interception and comparison mechanisms |
| [Memory schema](memory_schema.md) | Typed views over original memory layouts |
| [Performance](performance.md) | Measurement and optimization |
| [Hardware status](hardware_support.md) | Device-model coverage and known limits |
| [Enhancements](enhancements.md) | Read-only presentation and host integrations |
| [Future work](future_work.md) | Explicitly unimplemented proposals |

Other files in this directory are focused mechanism notes and must be read in
the context of the authorities above. Documents under [`history/`](history/)
are non-normative design records. They may contain retired names and commands;
they are not onboarding material.

Runnable examples live under [`examples/`](../examples/). Current command-line
tools are indexed in [`tools/README.md`](../tools/README.md).
