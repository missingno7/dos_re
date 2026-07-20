# The agent toolbox — task → tool → command

**Audience: the AI agent using `dos_re` to recover a game.** This is the
operational index: for each recurring task, the tool that does it mechanically,
the command, and what to do when it fails. The porting *method* (phases, loop
protocol, checklists) lives in [`getting_started.md`](getting_started.md); the mechanism deep-dives
live in the other docs here. This page is the routing layer between them.

## The division of labor (who does what)

| Work | Owner |
|---|---|
| Mechanical first-draft translation of ASM → Python | the **lifter** (`liftgen`/`liftverify`), never hand-transcription |
| Deterministic comparison against the original | the **oracles** (hook verifier, frame verifier, demo replay), never eyeballing |
| Repetitive diagnosis (hotspots, lengths, layouts, leaks) | the **tools** (`profile_hotspots`, `lindis`, audits), never re-derivation |
| Interpretation, naming, refactoring, deciding what's next | **you** |
| The final claim "this equals the original" | an **oracle proof** — your reading of the code is never evidence |

If a step is deterministic and you're doing it by hand, stop: either a tool
exists (below) or the framework should grow one (`AGENTS.md`).

---

## 1. Boot / load a new DOS executable

```python
from dos_re.runtime import create_runtime
rt = create_runtime("assets/GAME.EXE", command_tail=b"")
rt.cpu.run(1_000_000)
print(rt.cpu.addr(), rt.cpu.instruction_count)
```

Or zero-setup, with a window: `python tools/view.py --exe assets/GAME.EXE`
(the standard player CLI: `--headless`, `--snapshot`, `--save-snapshot`,
`--record-demo`, `--play-demo`, hotkeys F10 screenshot / F11 demo / F12
snapshot).

- Packed EXE (LZEXE etc.)? The unpacker is *bootstrap, not gameplay*: run it
  once (`dos_re.bootstrap_lzexe` accelerates LZEXE 0.91), then snapshot past
  it and work from the unpacked image.
- Game needs Sound Blaster? `dos_re.runtime.enable_sound_blaster(rt)`.
- Arrived as a DOSBox-X save instead of a clean boot?
  `dos_re.dosbox_savestate` imports it.

## 2. The VM fails loud (unsupported CPU / DOS / BIOS / VGA behaviour)

This is by design — the framework never guesses. The exception names the
exact instruction/service/port and context.

1. Decode what the program actually needed (`tools/lindis.py` at that address;
   the exception text usually suffices).
2. Implement the **observed** behaviour in the owning module (`cpu.py`,
   `dos.py`, `memory.py`…) with the register/flag contract documented, plus a
   focused test. Rules and the per-area extension recipes: [`AGENTS.md`](../AGENTS.md).
3. Never work around it in the game port — the next game hits it too.

Port reads specifically: unmodeled reads return 0 by default but are always
recorded in `dos.unmodeled_port_reads`; set `rt.dos.strict_ports = True` to
make each one fail loud with the reading CS:IP.
[`hardware_support.md`](hardware_support.md) is the honest model-status matrix.

## 3. Capture / restore snapshots

```python
from dos_re.snapshot import write_snapshot, load_snapshot
write_snapshot(rt, "artifacts/snap_after_init",           # full machine freeze
               status="after init", steps=rt.cpu.instruction_count)
rt2 = load_snapshot("assets/GAME.EXE", "artifacts/snap_after_init")   # exact resume
```

When: after the packer bootstrap, after init/first playable state, before any
routine you're studying (the verifier's fixture), at any divergence. In the
viewer: F12. Snapshots are evidence, never a runtime dependency of the
shipped port.

PM (DOS/4GW) runtimes use `dos_re.pm_snapshot` instead: `save_pm_snapshot` /
`load_pm_snapshot` (same directory convention), plus `clone_pm_runtime` for
in-memory oracle clones. Same F12 in the PM viewer.

## 4. See what the screen shows

```bash
python tools/render_frame.py <snapshot_dir>              # snapshot → PNG
python tools/render_frame.py out.png --exe GAME.EXE --steps 2000000
```

Decodes VGA mode 13h and the EGA/VGA planar path (shadow planes + CRTC start
+ DAC). A mode it doesn't cover means your adapter grows a rasterizer — this
tool is the template.

**DOS/4GW (MZ+LE, 32-bit protected mode) titles** use the flat-386 pair
instead:

```bash
python tools/le_info.py assets/GAME.EXE                  # objects/entry/fixups
python tools/pm_boot.py --exe assets/GAME.EXE --png frame.png \
    --keys 20 --scancodes 39,b9 --at 30000000            # run to the frontier
```

`tools/pm_view.py` is the zero-setup live window for these titles (the
PM `view.py`); `dos_re.pm_player.main` is the same runner as a library for a
port's own `scripts/play.py`.

`pm_boot` is the bring-up loop: each run stops at the first unimplemented
opcode/service and names it — implement the observed behaviour, re-run.  The
PNG render follows the live VGA state (chained 13h or unchained Mode X).
Programmatic use: `dos_re.runtime.create_pm_runtime` +
`dos_re.dos4gw.render_pm_frame`.

For lifting, the 32-bit pipeline is `tools/pmlift.py` (census + emit +
in-situ differential verify in one CLI; the pieces are
`lift/decode32|cfg32|emit32|runtime32` + `pm_verification.PMHookVerifier`):

```bash
python tools/pmlift.py --exe GAME.EXE --auto-entries 300 --census
python tools/pmlift.py --exe GAME.EXE --boot-steps 2000000 \
    --auto-entries 40 --verify --steps 8000000 --emit-dir mygame/lifted32
```

## 5. Trace / read code

```python
rt.cpu.trace_enabled = True          # per-instruction disassembly + state
...
for line in rt.cpu.trace: ...
```

```bash
python tools/lindis.py assets/GAME.EXE <snapshot_dir> 1010 9AFF 9C6B
```

`lindis` = linear disassembly of CS:START..END at a snapshot — static decoder
lengths (the lifter's, interpreter-cross-checked), interpreter-rendered text.
When: reading a routine before/after lifting it, decoding a fail-loud address,
checking what a divergence trace pointed at.

## 6. Find frame boundaries and wait loops (profile first!)

```bash
python tools/profile_hotspots.py assets/GAME.EXE 5000000 --snapshot <snap> --top 40
```

Reports where runtime goes, **tight backward edges** (= busy-wait loops: PIT
wait, retrace wait, input polls) and boundary crossings. This is the
mechanical answer to "where are my frame boundaries?" and "which loops need
classifying?" — run it before any manual tracing.

Classify what it finds:

- **Timer/retrace waits** → the frame-verify boundary hooks +
  `reference_env_hooks` (the oracle keeps these so it doesn't spin forever).
- **Boundary-less input polls** (title/menu "press fire") → the adapter's
  shared input-wait registry, consumed by EVERY driver. Miss one and demos
  freeze — [`demos_and_snapshots.md`](demos_and_snapshots.md) (the
  boundary-clock invariant) before recording anything.
- Role bookkeeping: `dos_re.hook_taxonomy` (checkpoint / env_wait /
  debug_probe / glue).

## 7. Record / replay deterministic demos

```python
from dos_re.replay import (ReplayArtifact, ReplayEvent, ReplayPoint,
                           ExecutionProfile, verify_interval, bisect_divergence)
```

Record the adapter's deterministic external events as one `ReplayArtifact`.
Register the original interpreter and each candidate as an `ExecutionProfile`,
then use `tools/replay_verify.py` or `verify_interval` for exact X→Y replay.
Machine-backed profiles use complete machine projection; detached native
profiles publish the same canonical semantic schema from native state.

On divergence use `bisect_divergence`. It persists the latest valid point and
the failing successor inside the artifact; no second replay record is created.
Inspect the corpus item with `python tools/replay_info.py <artifact-dir>`.

## 8. Hook a routine

```python
@registry.replace(0x1030, 0x4537, "scan_4537")   # dos_re.hooks.HookRegistry
def hook(cpu): ...                                # thin adapter over a pure rule
```

- Return mechanics must be exact; child hook boundaries route through
  `call_installed_hook_like_near_call` / `jump_installed_hook_boundary`
  (a direct Python call hides the child from verification —
  `tools/audit_hook_oracle.py` catches this statically).
- Runtime-patched routine? Guard with `self_disable_if_patched` /
  `code_matches`; multiple live variants → `dos_re.runtime_code`.
- Unrecovered path reached at runtime → raise `dos_re.gaps.HybridGap`. Never
  fall back silently.
- Shared 8086 flag semantics for hand-written hooks: `dos_re.asm` (INC/DEC
  preserve CF, REP string ops honouring the EGA aperture, …).

Details: [`hooks_and_verification.md`](hooks_and_verification.md).

## 9. Verify a hook against the oracle

```python
from dos_re.verification import HookVerifierConfig, install_hook_verifier
install_hook_verifier(rt, HookVerifierConfig.strict())   # no metadata needed
```

Strict mode: run the hook, take its final address as the target, run the
original ASM there, diff registers + flags + **full memory**. Faster metadata
mode: declare a `HookStop` per address. On divergence:

```bash
OK_TRACE_HOOK=CS:IP python <failing-test-command>  # prints the ASM oracle's trace
```

Read the trace before theorizing — the classic causes are freed-stack scratch
words, flag shape, early-out branch to a shared RET, a nested child hook, and
capture phase. Whole-frame equivalence: `dos_re.frame_verify.run_frame_verifier`
(reference vs candidate stepped to adapter frame boundaries, PNG + report
artifacts on mismatch).

## 10. Lift routines automatically (never hand-translate a first draft)

> **The 3.0 default is WHOLE-GRAPH assembly plus indexed interval replay**
> ([`dos_re_2.0.md`](dos_re_2.0.md)): `tools/codemap.py` (census) →
> `tools/liftemit.py` (batch-emit everything) → `tools/liftlink.py`
> (structural linking) → `lift.install.install_vmless_graph` →
> `replay.verify_interval` → `replay.bisect_divergence` on divergence. The
> per-slice `liftverify` loop below remains the tool for
> per-function diagnostics and the hybrid auto-install tier.

```bash
# Census: which entries are liftable, and why not (indirect jump / x87 / …)
python tools/liftgen.py --exe assets/GAME.EXE --snapshot <snap> \
    --entries-file docs/<game>/candidates.txt

# Lift + prove in situ: emit literal hooks, install under the strict verifier,
# run the VM, diff EVERY call against the ASM oracle, write the proof ledger
python tools/liftverify.py --exe assets/GAME.EXE --snapshot <snap> \
    --entry CS:IP --steps 5000000 --emit-dir <game>/lifted
```

Pick a snapshot where the target actually executes — `NOT_REACHED` means the
proof never ran, not that it passed. `ORACLE_PASSING` means the first
`--samples` calls (default 20) were byte-exact — a sample, not a whole-run
proof; the output marks "(hit --samples cap; later calls unverified)", and a
deeper branch can still diverge past the cap (raise `--samples`, or lean on
demo-replay equivalence). `DIVERGED` = read the reported call and treat it
like any hook divergence. Callees/INTs run through the VM
(`dos_re.lift.runtime`), so lifting order is irrelevant and lifted +
hand-written hooks compose.
Full design + failure policy: [`lifting_design.md`](lifting_design.md).

## 11. LIFTED is not RECOVERED (the metrics-honesty rule)

Lifted functions live in their own `<game>/lifted/` tier with their own JSON
proof ledger (`dos_re.lift.manifest`: `LIFTED → ORACLE_PASSING → INSTALLED →
REFACTORED`), **disjoint** from `@oracle_link` islands. A lift counts as
recovered only after **you** rename and simplify it into clean pure-layer
Python and tag it `@oracle_link` — with the same oracle tests unchanged. A
wall of unread-but-verified lifted code must never inflate "recovered %".

## 12. Prove detached execution with the shared replay corpus

Use `ReplayArtifact` for gameplay and front-end intervals alike. Each oracle or
candidate backend supplies an execution profile and replay driver. Machine-backed
profiles compare complete continuation state; detached native profiles project
their authoritative state into the same `CanonicalState` schema used by the
oracle.

Place stable points at meaningful game ticks, presentation transitions, decision
seams, and function boundaries. Restore the nearest cached point, replay only the
relevant X→Y interval, and call `replay.bisect_divergence` when a mismatch must be
localized. Function changes should use the artifact's function-visit interval
instead of replaying unrelated execution.

The rationale behind the consolidated model is recorded separately in
[`history/replay_evolution.md`](history/replay_evolution.md). That history is not
an API reference.

## 12c. Prove the VM-less AUDIO driver (the resident sound module)

A game's resident sound driver — a loaded module at its own segment, ticked from
the timer ISR, writing OPL/SB registers through ports — is provable byte-exact
INDEPENDENTLY of gameplay recovery, because its whole world is its own segment
image: the game commands it by writing cells there (page/SFX requests), and its
output is the register write stream. Two gates, both built on three trapped
addresses (`dos_re.step_probe` — the tick entry, the register-write leaf, the
game's frame boundary), captured per present-frame over an oracle
`ReplayArtifact` interval:

- **FORWARD gate** (reads the tempo + proves the music tick): seed the recovered
  driver ONCE from the VM's segment image, run it forward at the captured
  ticks-per-frame, diff writes per frame. It stays byte-exact across a steady
  single-page music window and diverges exactly at the first game event (an
  SFX/page request written into the driver's cells mid-frame — invisible to a
  forward sim). That boundary is the music-isolation surface, not a bug; and the
  captured tick count fixes the TEMPO as a measurement instead of an ear-tune.
- **PER-TICK gate** (the strongest form — verifies the whole interval, SFX and page
  changes included): re-seed the recovered driver from the true segment image at
  EVERY tick entry and diff that one tick's writes. Each tick is independently
  seeded, so there is no forward drift and nothing is out of scope.

A real driver bug shows up as a small mid-window mismatch (forward gate) or any
per-tick mismatch; lock a short per-tick window into the suite as the regression
gate. Validated on OVERKILL (`overkill/probes/verify_native_audio.py`): ~700
ticks per replay across six levels/songs, zero divergence, through SFX bursts and
page switches.

## 13. Measure progress (never estimate)

```bash
python tools/gen_island_manifest.py <game>.codecs <game>.recovered -o docs/recovered_islands.md
```

- `@oracle_link(boundary, contract, status, merge_target)` on every recovered
  function (`dos_re.islands`); the manifest is generated, never hand-edited.
- **Native %** over a demo replay — the framework collector:

  ```python
  from dos_re.coverage import CoverageCollector
  cov = CoverageCollector(classifier=my_addr_to_island,      # the adapter's only job
                          cache_path=Path("artifacts/coverage_cache.json"))
  rt.cpu.coverage_telemetry = cov
  ...replay a demo...; print(cov.format_summary()); cov.save_cache()
  ```

  Hooked work is measured in verifier-reported **ASM-equivalent instructions**
  (never hook calls); unverified runs estimate from the cache; anything
  unmeasurable is reported OUTSIDE the percentage. Wrap oracle reference runs
  in `cov.bounded_original()` so they don't read as un-recovered ASM.
- Endgame triage of the last unhooked addresses: `dos_re.frontier`.

## 14. Keep yourself honest (run with every change)

```bash
python tools/lint.py                        # the game-agnostic / lean-deps boundary (stdlib+numpy core)
python tools/audit_layers.py <game>/recovered   # pure layer never imports the VM
python tools/audit_hook_oracle.py <game>    # no child hooks hidden from verification
python tools/check_undefined_names.py       # latent-NameError guard
python tools/check_doc_links.py .           # broken doc links (docs are agent-maintained; run after edits)
python -m pytest tests -q                   # framework suite (pypy runs it ~4x faster)
```

Slow headless work? PyPy runs all headless paths unchanged, ~13–17× on raw
interpretation — [`performance.md`](performance.md). Interpreter optimizations
themselves need the byte-exact equivalence gate described there.
