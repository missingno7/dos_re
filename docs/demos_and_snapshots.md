# Deterministic demos and snapshots

Demos and snapshots are the *evidence substrate*: every proof the framework
offers ("the recovered code equals the original") is ultimately "replay this
recorded input from this pinned state and diff everything."

## Snapshots (`dos_re/snapshot.py`)

`write_snapshot(rt, dir, status=..., steps=..., trace_tail=...)` freezes the
full machine — 1 MB memory + EGA planes, CPU registers/flags, DOS state (video,
DAC, PIT, OPL registers, allocations, file handles), program metadata.
`load_snapshot(exe_path, snapshot_dir)` restores it into a fresh runtime.

Uses:

- **skip the bootstrap** — run the slow unpack/asset-decode once, snapshot past
  it, and start every investigation from there;
- **pin a reproducible fixture** — a snapshot before/after a routine is the
  entry state for its oracle;
- **anchor a demo** — a demo's start snapshot is what makes its replay exact.

Snapshots are evidence: name them descriptively, keep the ones that justify
hooks/tests/findings, prune scratch ones freely.

**Loading rebuilds the runtime fresh** — `load_snapshot` goes through
`create_runtime`, which reinstalls every hook registered in the global
`registry`. Snapshots persist no hook state; determinism holds because the
same hooks register at the same addresses every load. If you need a pure-ASM
oracle from a snapshot, clear/skip the registry installs explicitly (the
`DOS_RE_DISABLE_HOOKS` env var disables individual addresses).

## Where evidence lives (git convention)

Everything under `artifacts/` is **regenerable scratch and gitignored** —
record freely, prune freely. When a demo or snapshot becomes *evidence* (a
test replays it, a ledger cites it, the proof corpus includes it), **promote
it**: move it to `artifacts/test_oracles/` or `artifacts/evidence/` (both
tracked) and give it an entry in your `docs/<game>/demo_manifest.md` (name,
purpose, length, what it exercises, pass status). An unpromoted demo is a
scratch run; an unlisted promoted demo is a corpus blind spot — the manifest
is what makes corpus coverage a measured number (pitfall #22). Mind size:
demos are small (events + one snapshot); prefer promoting demos over raw
memory dumps, and never commit anything containing original game data beyond
what a snapshot inherently embeds (snapshots contain the game's memory image —
they stay local/private unless the rights situation allows otherwise).

`dos_re/repro_artifacts.py` captures *divergence* snapshots automatically: a
detached clone of the runtime taken before the failing hook ran, plus a
manifest of when/why — a ready-to-load repro.

`dos_re/dosbox_savestate.py` can import a DOSBox-X save state (memory +
registers) as an alternative evidence source; the adapter locates the program
image inside it by code signature.

## Input demos (`dos_re/input_demo.py`)

A demo = an optional start snapshot + VM-visible key events keyed to an
**emulated boundary counter** (the "demo clock") + opaque metadata (the adapter
records video/sound mode, command tail, …). Replay delivers each event when the
counter reaches its boundary — deterministically, into one runtime or a
reference/candidate pair at once.

Two anchoring styles:

- **snapshot demos** — start from the recorded snapshot; the default.
- **cold-start demos** (`recorder.start(rt, boundary=0, write_start_snapshot=False)`)
  — no snapshot; playback boots a fresh runtime from the boot params in
  `metadata` and replays from boundary 0: an input-only capture of a whole
  session from power-on. (`playback.is_cold_start` tells the driver.)

Delivery uses `deliver_scancode` (port 60h + the game's own INT 09h ISR), not
BIOS INT 16h — action games poll their own key-state table. `KeyDispatcher`
holds keys ≥ 1 polled frame so quick taps are never lost. For fine-grained menu
poll waits, `apply_to_runtime(..., single=True)` delivers at most one event per
call so a release/re-press pair recorded on the same boundary is observed as
two states instead of collapsing.

## The boundary-clock invariant (the trap that voids proofs)

**Read this twice.** A demo is a valid proof artifact only if it replays
byte-for-byte identically under **every driver** — the interactive play loop,
the headless hook verifier, and the frame verifier. If they count "a boundary"
differently, the same demo replays at different internal points per driver and
the corpus pass/fail becomes driver-dependent: the proof is an illusion. In
practice this manifests as **freezes**, not loud errors.

Two concrete failure modes:

1. **Boundary-less input-wait loops.** Some original code busy-waits on the
   keyboard *without* reaching a timer/retrace/present boundary ("press FIRE to
   start"). The demo clock is frozen inside the loop, so a recorded key release
   keyed to a later boundary is never delivered — the loop waits forever. Every
   driver must recognize these loops (at their **canonical head address**,
   checked **every step**) and treat them as a boundary. Keep the detectors in
   **one shared registry** in the adapter (`input_waits.py`) consumed by all
   drivers — per-driver copies drift.

2. **Driver-specific clocks.** Before standing up a demo corpus, unify the
   boundary/clock definition so record-time, replay-time, and every driver
   agree on exactly what increments the counter.

Also model out in verify mode: wall-clock pacing, asynchronous timer-IRQ
delivery, RNG seeding. The oracle keeps the hardware-wait hooks (timer,
retrace) so the ASM doesn't spin on a flag a real IRQ would clear — but those
waits must return deterministically.

## The proof spine (how demos become "proven equivalent")

1. Per-hook ASM match for every hooked address.
2. Semantic frame verifier at each frame boundary.
3. Widen the frame sample until it covers all observable state (find the RNG
   state early — it is usually the first hard sub-task).
4. Deterministic demo-replay harness: candidate ≡ oracle for every frame to the
   end of each demo.
5. A demo corpus covering all levels, bosses, spawn types, and RNG paths —
   with coverage *measured*, not vibed.

See [`ai_porting_charter.md`](ai_porting_charter.md) §5–6 for the full
treatment.
