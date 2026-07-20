# Recovery IR: retained static evidence

Recovery IR is the deterministic retained representation of static structure
recovered from one identified program image. It lets multiple tools reuse the
same decoded facts without parsing generated Python or repeatedly rediscovering
them.

Recovery IR is optional. A focused lift or verifier may scan a target directly
from a snapshot. A project may also begin from replay observations or manual
entry facts and generate IR only for a subsystem that benefits from retained
static structure.

## Authority boundary

`tools/irgen.py` produces the document. `dos_re.lift.ir` loads records and
re-elaborates their pinned bytes through the shared scanner. The IR owns the
static facts it contains; it does not own:

- replay events, observations, or continuation boundaries;
- implementation availability or selection;
- manual semantic claims;
- Atlas indexes;
- runtime dispatch.

An Atlas importer translates document-local addresses into stable
`ProgramIdentity`, `ImageIdentity`, `FunctionIdentity`, and execution-point
identities. Raw `CS:IP` or flat addresses remain local locators, not
cross-artifact names.

## Document contents

IR schema v0 retains, per function:

- entry locator and optional display symbol;
- basic blocks and pinned instruction bytes;
- direct near/far calls, jumps, exits, interrupts, and platform effects;
- memory and port-effect summaries;
- refusal records and unsupported constructs;
- boundary and dynamic-dispatch facts supplied by the project;
- runtime-code/SMC verdicts and signatures;
- applied recovery facts and provenance.

A refused function remains a record. Unsupported or unresolved behavior is not
dropped merely because no emitter can consume it.

Example shape:

```json
{
  "ir_version": 0,
  "provenance": {
    "exe": {"name": "GAME.EXE", "sha256": "..."},
    "snapshot": {"identity": "..."},
    "toolchain": "...",
    "facts": "..."
  },
  "functions": {
    "1010:16A9": {
      "entry": "1010:16A9",
      "liftable": true,
      "refusals": [],
      "blocks": [],
      "calls_near": [],
      "calls_far": [],
      "exits": ["ret"],
      "signature": "..."
    }
  },
  "unsupported": []
}
```

## Producing retained IR

```bash
python tools/irgen.py \
  --exe GAME.EXE \
  --snapshot artifacts/snapshots/code \
  --entries-file artifacts/entries.txt \
  --out artifacts/recovery_ir.json
```

Inputs must identify the original binary, loaded code-byte state, entry set,
explicit recovery facts, and current toolchain. Regeneration from unchanged
inputs is deterministic. A different image, code snapshot, fact set, or
toolchain produces different provenance rather than silently reusing stale
facts.

## Consumers

Optional consumers include:

- literal, CPUless, and ABI-recovered emitters;
- control-flow, effects, contract, and address-expression analyses;
- link and closure diagnostics;
- the Execution Atlas static-evidence importer.

Consumers may add separate, cited analysis artifacts; they do not mutate a
retained IR document in place. A tool that can operate on a direct scan and an
IR record must converge on the same scanner/emitter contract and retain which
input path produced its result.

To add IR to an Atlas:

```bash
python tools/atlas.py ingest-ir artifacts/atlas \
  --ir artifacts/recovery_ir.json \
  --program my-game:1 \
  --image-label GAME.EXE \
  --image-sha256 SHA256
```

The Atlas materializes a normalized projection and source digest. It does not
become the owner of the IR or decode the executable again.

## Runtime-written code

Pinned bytes identify the variant observed by the IR producer. Runtime-written
slots and content-hashed variants use the stable identities in
`dos_re.runtime_code` and `dos_re.identity`. Unknown live variants remain
unresolved. Staticization or de-SMC transformation is an implementation choice
supported by evidence, not a rewrite of original identity.
