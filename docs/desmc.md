# De-SMC: explicit data flow for supported code mutation

Self-modifying code cannot be frozen into generated code from one observed
memory image. dos_re therefore keeps two independent facts:

- ordinary lifting refuses code whose instruction bytes may be written;
- an optional de-SMC analysis may describe a supported operand mutation and
  generate a candidate that reads the live operand bytes as data.

De-SMC is one optional recovery operation. It is not a mandatory stage, and an
observed mutation set is not evidence that every possible code version has
been seen.

## Refusal is the default

`dos_re/lift/cfg.py` records statically visible writes through `CS`.
`dos_re/lift/irgen_core.py` resolves cross-function writes across the recovered
census. A function that writes its own instruction bytes is refused as
`self-modifying`; a function written by another function is refused as
`code-patched-at-runtime`.

There is no switch that permits the ordinary emitter to freeze those bytes.
Unsupported or unresolved code mutation remains fail-loud and may continue to
execute through another selected implementation.

## Optional transform

`dos_re/lift/smc.py` resolves a statically visible write against the decoded
target instruction. A function receives a `desmc-candidate` verdict only when
every write into it targets a supported operand field. For that candidate,
`liftemit --desmc` emits reads from the live code-memory field instead of
embedding the operand found in the recovery image.

Patchers still perform ordinary memory writes. The generated consumer observes
the field at execution time, so different patch timing or repeated writes do
not require a separate generated function for each operand value. This is the
intended model, not promotion evidence; the emitted implementation must still
pass differential verification.

## Current mutation classes

| Mutation | Current verdict |
|---|---|
| Supported immediate operand of a data instruction | transformable candidate |
| Absolute pointer of a direct far call or jump | transformable candidate |
| Memory displacement or `moffs` address | refused; not implemented |
| Relative branch displacement | refused; lifted block targets would change |
| ModR/M or opcode | refused; instruction shape would change |
| Write crossing an instruction boundary | refused |
| Whole-sequence replacement or runtime-generated code | refused |
| Patch inside the generated entry-signature window | refused |

One unsupported slot gives the whole function a `desmc-unsupported` verdict
with a slot-level reason.

## Evidence and identities

Recovery IR owns the static de-SMC verdict and the cited write/field facts:

```json
{
  "smc": {
    "status": "desmc-candidate",
    "slots": [
      {
        "patcher": "66E6:66F5",
        "write_width": 1,
        "target": "66E6:6728",
        "field": "imm",
        "field_addr": "6729",
        "field_size": 1,
        "status": "candidate"
      }
    ]
  }
}
```

The plain record remains `liftable: false`; only the explicit de-SMC emit path
consumes the candidate verdict. Addresses identify the decoded write and field
within that evidence source. Runtime code variants and stable program
identities remain separate evidence that can be projected into the Execution
Atlas.

If a build image zeroes recovered code bytes, `dos_re.bootimage` preserves
de-SMC operand cells declared by Recovery IR because the generated
implementation reads them as data. That preservation makes the artifact
internally usable; code-byte poisoning is an optional diagnostic and is not
proof of release coverage or detachment.

## Verification contract

Before selecting a transformed implementation as faithful:

1. use `liftverify --desmc` for focused in-situ comparison;
2. replay oracle and candidate intervals that exercise multiple patch values
   and repeated patching when applicable;
3. compare complete continuation state or the declared canonical semantic
   projection;
4. retain the Recovery IR verdict, implementation digest, and replay evidence
   referenced by the catalog entry;
5. keep unseen or unsupported runtime code variants fail-loud.

Semantic names can label fields for readers, but they do not replace the
decoded write/operand evidence.

The incident that motivated this mechanism is recorded in
[`history/desmc_2.0.md`](history/desmc_2.0.md).
