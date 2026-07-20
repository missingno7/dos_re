# State mirrors — how recovered logic reaches game state

State mirrors are an optional readability and adapter mechanism. They give
DOS-memory-backed implementations named fields while keeping original offsets
in one project-owned bridge. A game may use them for one subsystem, use
detached values elsewhere, or not use them at all.

The generic backends and field descriptors live in
[`dos_re/state_view.py`](../dos_re/state_view.py). Game layout tables belong in
the port. A [memory schema](memory_schema.md) is a separate optional mechanism
when a region needs explicit ownership and generated import/export.

## The problem

A 16-bit DOS game's state is a 64 KB data segment (DGROUP) full of fixed-offset
variables, fixed-stride arrays, and in-memory pointers. Early recovered code
speaks that layout directly — `rw(0x6BF6)`, `mem.data[DS + off]`. That works
and verifies, but it couples the logic to the DOS memory image: the *what*
(advance the wind, project the sprite) is buried under the *where* (which
byte). It reads like a transliteration, not source.

The goal: recovered logic that reads like source — `s.wind`, `slot.x` — with
byte offsets confined to one small, swappable layer, **without weakening
byte-exact verification** when the selected implementation intentionally keeps
the DOS memory image authoritative.

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
        ├── ByteBackend          → the 1 MB image        (DOS-memory-backed execution)
        ├── OverlayBackend       → {off: val} contract   (read-through verification)
        └── WidthContractBackend → {off:(val,width)}     (write-only projection passes)
```

The recovered function is written **once**, against the view API. The backend
behind it decides what "reading state" means at that moment — the live VM
image, a native byte image, or an accumulating write-contract for a golden
test. One implementation, many adapters.

All layout for a view family lives in one game-owned bridge module. Keeping it
free of runtime selection makes the same semantic implementation usable through
multiple backend adapters.

## Why it is safe

The byte-backed view writes straight through the state image. For a faithful
DOS-memory-backed implementation, oracle and candidate can therefore compare
the same bytes. Moving a particular region to detached values instead requires
an explicit canonical projection or generated codec; it does not invalidate
views used by other regions.

## Practical notes (learned on the source ports)

- A function's backend is dictated by its selected implementation contract and
  verification representation.
- Name the shared structs once (the on-screen entity record is typically ~40 %
  of all offsets — the single biggest readability payoff).
- Leave genuinely union-typed offsets (read at different widths per entity
  type) as raw backend access with a comment; three aliases for one
  triple-typed offset add noise, not clarity.
- Byte-backed does not imply interpreter-backed: a `bytearray` plus an offset
  map can be a valid release representation when the selected closure permits
  DOS-memory-backed state.
- Removing raw offsets improves readability but does not, by itself, change
  storage ownership. Detachment is a separate per-region choice.
- This layer is for clean *simulation* code. Presentation enhancements attach
  at a different seam (a render-intent model emitted by the faithful renderer)
  and must never fake data the recovered core doesn't expose.
