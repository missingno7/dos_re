# Task: create a state mirror for an island

Move an island's raw offsets behind human-named typed views — without changing
behaviour and without weakening byte-exact verification. Read
`docs/state_mirrors.md` first.

1. **Scope**: one island per slice. List every raw offset access
   (`rw(0x6BF6)`, `mem.data[DS+off]`) it performs.
2. **Define the view** in the adapter's bridge module using
   `dos_re.state_view` (`StructView` subclass + `U8/U16/S8/S16/U16Array/
   StructArray` descriptors + your segment base via `coerce_backend`). The
   bridge module is the ONLY place these offsets may appear. Name fields by
   *evidence* (what the verified lift proved), not by guess.
3. **Width aliases**: if the ASM reads the same bytes at two widths, define
   two named fields (pitfall #2). Genuinely union-typed offsets (different
   meaning per entity type) may stay raw backend access with a comment —
   three aliases for one triple-typed offset is noise.
4. **Backend choice is dictated by the island's golden**: byte-backed for
   in-place mutation; `OverlayBackend`/`WidthContractBackend` for
   contract-returning passes. Match the golden; don't redesign it.
5. **Migrate the logic** to speak `view.field` only. Behaviour must be
   byte-identical: the island's existing golden passes with the SAME hashes,
   and the hook/frame verifiers show zero new divergence. If any hash
   changes, the migration is wrong — revert.
6. Regenerate the island manifest; note the drained offsets in run_status
   (the count of raw offsets remaining in logic is a progress metric).

The rule that governs everything here: *the bridge keeps the old
address-shaped world alive for verification, but prevents raw offsets from
becoming the language of the recovered game.* Finish with the REPORT block.
