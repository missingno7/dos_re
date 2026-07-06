# Task: stand up a new game adapter

You have this framework and a game's files. Goal of THIS task: the original
EXE boots in the VM, you can see its output and deliver input, and a no-op
frame verification passes — nothing recovered yet.

1. Read `START_HERE.md`, then `docs/porting_new_game.md` steps 0–4. Follow
   them literally; they encode two projects' worth of ordering mistakes.
2. Create the adapter package from `examples/adapter_skeleton/`. Wire
   `create_game_runtime` with the real EXE path and command tail.
3. Boot. When the interpreter fails loud on an opcode/interrupt/port, that is
   the work: trace the exact instruction, implement the *observed* behaviour
   in the core (rules in `AGENTS.md`), add a `tests/test_core.py`-style case.
   Log every such extension in your run_status ledger.
4. If the EXE is packed: run the unpacker once, `write_snapshot` past it,
   record the packer + frontier in the ledger. Bootstrap is extraction, not
   gameplay.
5. Decode the framebuffer to an image; deliver a scancode and prove the game's
   key table changed. Screenshot + memory evidence into the ledger.
6. Find the timer wait, retrace wait, and present routine
   (`tools/profile_hotspots.py` backward edges; `tools/lindis.py` to read
   them). Stand up the frame verifier with a no-op candidate; it must match
   the oracle frame-for-frame before you hook anything.

Constraints: no hooks yet, no recovered logic yet, no guessing what the game
"is doing" — only what the trace shows. Finish with the REPORT block
(`prompts/README.md`); the status for everything here is OBSERVED at most.
