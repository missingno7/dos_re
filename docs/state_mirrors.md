# State mirrors — how recovered logic reaches game state

> *The bridge keeps the old address-shaped world alive for verification, but
> prevents raw offsets from becoming the language of the recovered game.*
> This is the architectural center of the whole project.

> **Milestone scope:** this document describes the historical-view bridge used
> before M4 and as an oracle adapter.  A byte-backed view is not the final
> DOS-layout-less/native-memory-model milestone: its backing byte image remains
> authoritative.  M4 replaces it with ordinary detached values and a generated,
> dependency-inverted verification bridge; see
> [`memory_schema.md`](memory_schema.md).  The view machinery remains useful on
> the historical side of that bridge.

*(Generalized from the Prehistorik 2 port's proven `state_view_layer.md`. The
generic machinery — backends, field descriptors, view bases — ships as
[`dos_re/state_view.py`](../dos_re/state_view.py); your game's layout tables
(the `StructView` subclasses with the actual offsets) live in your adapter's
bridge module. For the terms "island" and "golden" used below, see
[`glossary.md`](glossary.md) (or the retired 1.0 starter's lifecycle docs (historical)).)*

## The problem

A 16-bit DOS game's state is a 64 KB data segment (DGROUP) full of fixed-offset
variables, fixed-stride arrays, and in-memory pointers. Early recovered code
speaks that layout directly — `rw(0x6BF6)`, `mem.data[DS + off]`. That works
and verifies, but it couples the logic to the DOS memory image: the *what*
(advance the wind, project the sprite) is buried under the *where* (which
byte). It reads like a transliteration, not source.

The goal: recovered logic that reads like source — `s.wind`, `slot.x` — with
byte offsets confined to one small, swappable layer, **without weakening
byte-exact verification** (which stays a trivial `memcmp` precisely because the
native state *is* the DOS memory image).

## The shape: one view API, swappable backends

```
        recovered logic  (pure — the WHAT)
        s.wind   slot.x = ...   entry.threshold
                    │  human-named fields, no offsets
                    ▼
        view        (StructView / StructArray / _U8 / _U16 / _S16 ...)  ── the WHERE
                    │  field → backend.rb/rw/wb/ww(offset)
                    ▼
        backend     (the HOW)
        ├── ByteBackend          → the 1 MB image        (native runtime + memcmp verify)
        ├── OverlayBackend       → {off: val} contract   (read-through; contract islands)
        └── WidthContractBackend → {off:(val,width)}     (write-only projection passes)
```

The recovered function is written **once**, against the view API. The backend
behind it decides what "reading state" means at that moment — the live VM
image, a native byte image, or an accumulating write-contract for a golden
test. One implementation, many adapters.

All layout lives in **one bridge module** per game (the Prehistorik 2 port's
`bridge/dgroup_view.py`) — the *only* file that writes down a DGROUP offset for
a migrated island. It stays pure (no `dos_re` imports), so both recovered logic
and VM adapters can import it without cycles.

## Why it is safe

The byte-backed view writes straight through the state image, so after any
migration the island's existing golden test passes with the same hashes, and
the forward oracle (native vs VM, memcmp of the data segment) stays
byte-for-byte identical. The "clean" representation and the "verifiable"
representation are the *same bytes* — migration proceeds island-by-island with
no window where correctness is unprovable.

## Practical notes (learned on the source ports)

- An island's backend is dictated by **how its golden returns results** — match
  the golden, don't choose freely.
- Name the shared structs once (the on-screen entity record is typically ~40 %
  of all offsets — the single biggest readability payoff).
- Leave genuinely union-typed offsets (read at different widths per entity
  type) as raw backend access with a comment; three aliases for one
  triple-typed offset add noise, not clarity.
- Byte-backed ≠ VM-backed: a `bytearray` + an offset map is pure Python data
  and can be a legitimate intermediate release representation — it is not the
  EXE, not a VM, and not a silent ASM fallback.  It nevertheless remains
  historical-layout-backed and therefore does not satisfy M4.
- Gameplay logic stopping its use of raw offsets is an important intermediate
  boundary.  M4 additionally requires the storage authority itself to become
  detached native state.
- This layer is for clean *simulation* code. Presentation enhancements attach
  at a different seam (a render-intent model emitted by the faithful renderer)
  and must never fake data the recovered core doesn't expose.
