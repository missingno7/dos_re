# De-SMC — lifting self-modifying code as explicit data flow

> «Detection must always prevent a frozen lift.  A separate evidence-driven
> de-SMC pass may replace code mutation with explicit data or control flow,
> but only after proving equivalence against the interpreter.»

## The problem

A literal lift freezes the code bytes one snapshot happened to hold.  If the
program PATCHES those bytes at runtime, the lifted copy keeps executing the
frozen operands — silently.  Found the hard way on SkyRoads: its LZS decoder
(`1010:66E6`) writes each compressed file's header-derived bit-width
immediates into its own body before decoding; the lift baked one menu-time
snapshot's widths and decoded every file with them, corrupting the startup
allocation chain into an out-of-memory exit with no error anywhere near the
cause.

## The two layers

**Layer 1 — refusal (always on).**  `lift/cfg.py` collects every
statically-visible code write (a CS-override store to a direct 16-bit
address) during the scan.  A target inside the function's own instruction
bytes refuses it (`self-modifying`); `lift/irgen_core.py` adjudicates
cross-function writes census-wide and refuses the PATCHED function
(`code-patched-at-runtime`, naming the patcher).  The ordinary lift can never
silently freeze mutable code.  This layer has no opt-out.

**Layer 2 — the de-SMC pass (`lift/smc.py`, opt-in).**  Each refused write is
resolved against the DECODED target instruction: which field do the written
bytes land in?  When every write into a function is a supported operand-field
patch, the function becomes a `desmc-candidate` and `liftemit --desmc` emits
it with those operands read from LIVE CODE MEMORY instead of baked in:

    # 1010:6728  6a05  push imm8        (field runtime-patched)
    _pi = mem.rb(s.cs, 0x6729)
    cpu.push((_pi | 0xFF00) if _pi & 0x80 else _pi)

This is semantics-preserving by construction: the real CPU decodes whatever
the bytes hold at that moment, and the transformed lift reads exactly those
bytes.  Patchers need no special handling — their stores into the code
segment are ordinary memory writes the transformed consumer now observes.
Patch timing, multiple patchers, and re-patching between calls are all
covered by the same argument.  The patch-slot cells are DATA now:
`dos_re.bootimage` preserves them from poisoning automatically (from the
IR's `smc` verdicts), and `tools/audit_boot_image.py` accepts the exemption
only for slots the IR itself declares.

## Mutation classes and their verdicts

| class | example | verdict |
|---|---|---|
| immediate operand of a data instruction | `push imm8`, `ALU acc,imm`, `mov reg,imm` | **transformable** (v1) |
| absolute far-transfer pointer | `jmp/call far ptr16:16` (ISR chain-to-old-vector) | **transformable** (v1) — reads the live pointer, i.e. becomes an indirect far transfer, which the emitter already models as a tail exit |
| memory displacement / moffs address | `mov [imm16], ax` | supportable by the same mechanism; not yet enabled (no observed case) |
| relative branch displacement | patched `jcc rel8` | **refused** — a mutated relative target cannot be re-expressed against lifted block structure without new machinery |
| ModR/M or opcode mutation | instruction SHAPE changes | **refused** — a finite-variant emitter (emit each observed variant, dispatch on the live byte) is conceivable but unproven |
| whole-sequence replacement / runtime-generated code | | **refused**, permanently — that is not operand patching, it is a different program |
| patch inside the entry-signature window | | **refused** (`patched-inside-entry-signature`) — it would trip the module's own SMC guard on every legitimate re-patch |

A single unsupported write keeps the whole function refused
(`desmc-unsupported` with the slot-level reason).

## Verification contract

A candidate is a CANDIDATE, not a proof.  The emitted module is promoted by
the ordinary differential machinery — `liftverify` in situ, then the
end-to-end demo differential — run over inputs that exercise MULTIPLE patch
configurations.  The SkyRoads validation: the transformed `66E6`, executed
from the identical pre-state as the interpreted oracle over a full TREKDAT
chunk (2,148 bit-reader calls through runtime-patched widths that the
prologue re-writes per chunk), matched the oracle's final state with a
**0-byte diff over the full 1 MB machine image**, and the previously-corrupt
startup allocation sequence became byte-identical to the interpreter's.

## Census report

Every function's IR record carries its verdict:

```json
"smc": {
  "status": "desmc-candidate",
  "slots": [
    {"patcher": "66E6:66F5", "write_width": 1, "target": "66E6:6728",
     "field": "imm", "field_addr": "6729", "field_size": 1,
     "status": "candidate"}
  ]
}
```

`status` distinguishes: no `smc` key (not patched at all) /
`desmc-candidate` / `desmc-unsupported` (with per-slot reasons).  Records
with candidates keep `liftable: false` — the plain emit path still refuses
them; only `--desmc` consumes the verdict.

Semantic naming (e.g. SkyRoads' recovered `LzsWidths.width_len` for the slot
at `6729`) is deliberately NOT required by the machinery: slots are keyed by
address + decoded field, which is what the equivalence proof needs.  A port's
naming manifest may label slots for readability; that is metadata, never
evidence.
