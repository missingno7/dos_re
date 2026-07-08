# examples/ — optional material, hard-separated from the framework

Everything in this directory is **inert onboarding and validation material**.
The boundary contract:

- **Nothing in `dos_re/` (or `tools/`) imports anything from `examples/`.**
  The dependency points one way only: examples import the framework.
- **Not packaged.** `pyproject.toml` ships `dos_re*` and `nuked_opl3*`;
  examples never end up in a wheel.
- **Deletable.** Removing this whole directory breaks nothing: the
  example-driven tests (`tests/test_tiny_frame_game.py`,
  `tests/test_no_undefined_names.py`'s examples scan) detect the absence and
  skip. A game port that vendors this framework can drop `examples/` entirely.
- **No game content.** The "games" here are hand-assembled synthetic MZ
  programs written for this repo — teaching fixtures, not recovered software.

What's here:

| Directory | Role |
|---|---|
| [`minimal_adapter/`](minimal_adapter/example.py) | 5-minute demo of the hook → verify → snapshot loop on a straight-line program. |
| [`tiny_frame_game/`](tiny_frame_game/README.md) | The whole lifecycle on a synthetic frame-loop game (oracle boot, cold-start demos, both verification oracles, state mirror). Doubles as the repo's full-stack integration test. |

The adapter template you copy to start a real game port (`adapter_skeleton/`)
lives in the `template_dos_port` repo now, not here — this repo (`dos_re`) is the
framework only, meant to be consumed as a git submodule from `template_dos_port`
(or any other adapter repo). When you start a real port, your adapter package
lives **at the porting repo's root, next to its `dos_re/` submodule** (e.g.
`mygame/`). See `template_dos_port`'s `START_HERE.md` step 2 for the conventions
that come with it (tests, lint roots, asset-skip).
