# Verification contracts

Verification compares the strongest common authoritative representation shared
by the oracle and candidate. A more recovered implementation need not retain
private machinery merely because the oracle still uses it.

## Three comparison surfaces

| Surface | Use it when | Required authority |
|---|---|---|
| complete continuation | Both sides share one live guest representation | Complete backend continuation: registers, flags, stack, memory, devices, timing, scheduler, and replay cursor. |
| semantic state | A region owns execution and may use another representation | Declared gameplay/domain state, deterministic input, semantic boundary, RNG/timing, declared outputs, and ordered observable effects. |
| continuation seam | A native region returns to guest or generated code | Named exit and continuation identity plus every register, stack location, shared-memory region, timing/interrupt state, and device state the receiver can observe. |

These are not confidence tiers. Each is the strongest appropriate contract for
one boundary. Complete-continuation equality is stronger only when its extra
state is genuinely shared and meaningful.

## Explicit declarations

`VerificationProjectionContract` declares a projection ID, representation,
schema, required canonical fields and byte regions, effect domains, and
intentionally excluded internal state. The verifier validates requirements on
both sides before comparing them. Two candidates cannot pass by omitting the
same difficult field.

`RegionVerificationContract` pairs one interior projection with an exact
`RegionExitVerificationContract` for every region exit. Faithful authored
regions require this declaration. Execution-plan and composition digests include
it, so changing the claim invalidates profile-local replay evidence.

The runtime prints the selected projection, representation, required fields and
regions, effects, and exclusions before differential verification. A mismatch
identifies the first replay transition and the differing canonical path.

## Faithful claims

A faithful renderer selects pixel/palette parity (state plus exact framebuffer
and palette) or semantic scene parity (state plus declared scene/render intent).
Intentionally different pixels belong only to a presentation enhancement.

A faithful audio implementation states its comparison level: gameplay sound
events, abstract music/SFX commands, OPL command stream, or final samples.
The first three are not sample parity. A native audio backend may emit the
declared command stream without retaining a Sound Blaster or OPL device object.
If generated code resumes and can observe that device, the exit seam must
reconstruct and compare the compatible state it needs.

Non-authoritative presentation enhancements compare authoritative gameplay and
their declared semantic input while excluding only their owned output.
Behavioral modifications run under their own declared tests, never under a
faithful differential claim.

## Effects and determinism

Semantic point state alone is insufficient: a wrong intermediate effect can
disappear before the next point. `verify_checkpointed` combines canonical point
state with the ordered observable interval digest. Its `AUDIO_COMMAND` record
represents an OPL register/value command rather than the original two-port bus
sequence; native code emits that same command when it makes that claim.

Instruction trace, temporary registers, guest call depth, renderer scratch,
and raw device bus setup remain diagnostic evidence. They are not required for
a semantic-native claim unless declared as a timing or effect requirement.
