# Game adapter skeleton

This is the *shape* of a game adapter package built on the `dos_re` framework —
the files you create when you start recovering a new DOS game.  It is a
**template, not runnable code**: every constant is a placeholder you must
re-derive for your game against your own oracle (the original executable).

The layout mirrors the two proven adapters this framework was extracted from
(`pre2_port/pre2/` and `overkill_port/overkill/`):

```
mygame/
  runtime.py        boot / snapshot-load wiring (EXE path, command tail, hooks install)
  hooks.py          replacement hooks: @registry.replace(cs, ip, name) thin adapters
  verification.py   per-hook continuation metadata (HookStop) for the verifier
  frame_verify.py   frame boundaries + sample builder + reference env hooks
  input_waits.py    boundary-less input-poll loop detectors (shared by ALL drivers)
  recovered/        pure recovered game logic — NEVER imports dos_re/cpu/memory;
                    every function tagged @oracle_link (dos_re.islands)
  bridge/           typed views: VM memory <-> named fields (the ONE place offsets live)
  codecs/           native decoders for the game's packed asset formats
  native/           [grows later] the VM-less runtime: native game state + boot
                    constants + the fixed-step frame driver — the shipped product,
                    composing the SAME recovered/ functions the hooks use
  probes/           throwaway observation/diagnostic scripts
```

The lifecycle these directories serve: hooks ground each recovered function
against the oracle in the hybrid runtime; `native/` later composes the same
functions with the VM gone. One implementation, many adapters — never two
copies (see `docs/lifecycle.md`). Generate your recovered-islands manifest
from the `@oracle_link` metadata:

```
python tools/gen_island_manifest.py mygame.codecs mygame.recovered -o docs/recovered_islands.md
```

Read `docs/porting_new_game.md` for the step-by-step bring-up checklist and
`docs/ai_porting_charter.md` for the full method.

Layering rules (enforce them with `tools/lint.py`-style checks from day one):

- `recovered/` and `codecs/` are pure: no `dos_re`, no segment:offset, no hooks.
- `bridge/` may know memory layout but holds no gameplay decisions.
- `hooks.py` bodies are thin adapters: read VM state -> call a pure rule ->
  write VM state -> exact return mechanics.  No logic accumulates in hooks.
- `dos_re` never learns your game's addresses, filenames, or formats.
