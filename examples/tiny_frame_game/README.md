# tiny_frame_game — the whole method in ten minutes

A synthetic DOS "game" small enough to read in one sitting (18 instructions:
a retrace-wait frame loop, an INT 09h keyboard ISR, and a framebuffer row
painted with `counter + keystate`), driven through **every core mechanism of
the framework** in one runnable script:

```
python examples/tiny_frame_game/walkthrough.py
```

| Stage | What it proves |
|---|---|
| oracle | the EXE boots in the VM (mode 13h via INT 10h, ISR installed via the IVT) and the framebuffer follows the frame counter; frames are stepped with `dos_re.checkpoints` |
| cold-start replay | one `ReplayArtifact` embeds its base continuation state and immutable input events, then replays byte-identically for 10 frames—with a key press visibly changing the output |
| snapshot | freeze mid-run, restore, both continuations agree frame-for-frame |
| hook oracle | a draw hook that fills **319 of 320** pixels — registers all correct — is caught by the strict differential verifier's full-memory diff at the exact byte; the correct hook then runs verified on every call |
| frame oracle | `run_frame_verifier` locksteps a pure-ASM reference against the hooked candidate: 6 frames, 0 divergences; the wrong candidate is detected at frame 1 with diff artifacts dumped |
| state mirror | a `StructView` gives the game's state human names (`view.counter`, `view.keystate`) over the exact bytes the oracle verifies |

The point is onboarding, not realism: [`game.py`](game.py) is the "original
binary" (its docstring is the disassembly), and [`walkthrough.py`](walkthrough.py)
is the whole lifecycle. For a real game, continue with
[`../../docs/getting_started.md`](../../docs/getting_started.md).

Two details worth noticing:

- **One boundary driver.** Recording and replay share `run_session()` — a
  single definition of this example's stable frame point. Real backends may use
  different stop seams, but must map them to the same semantic replay points
  ([`../../docs/demos_and_snapshots.md`](../../docs/demos_and_snapshots.md)).
- **The wrong hook has correct registers.** Only the default full-memory diff
  catches it. That is why narrowing the diff is pitfall #7.
