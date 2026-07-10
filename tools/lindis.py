"""Linear disassembler: static lengths from dos_re.lift, text from the interpreter.

Loads a snapshot memory image, then linearly decodes a CS:offset..offset range.
Instruction LENGTHS come from the static decoder (``dos_re.lift.decode`` — the
lifter's, unit-tested against the interpreter); the human-readable text still
comes from executing each instruction once on a throwaway runtime and capturing
what ``execute_opcode`` returns. Per-instruction exceptions are swallowed so an
odd opcode does not stop the sweep (the static length keeps the walk aligned).

History: this tool used to measure lengths by counting ``cpu.fetch8`` calls
through one step(). The 2026-07-09 interpreter optimization rounds inlined the
hot fetch paths, which silently broke that trick (opcode/modrm/displacement
bytes no longer route through fetch8). The static decoder is now the length
authority here — and unlike the old trick it does not require the instruction
to be executable.

Usage:
    python tools/lindis.py <exe_path> <snapshot_dir> <CS> <START> <END>
e.g python tools/lindis.py assets/GAME.EXE artifacts/demos/.../snapshot 1010 9AFF 9C6B

Origin: adapted from the Overkill port's scripts/lindis.py (its game-specific
snapshot loader replaced by the generic dos_re.snapshot.load_snapshot).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dos_re.lift.decode import decode_one  # noqa: E402
from dos_re.snapshot import load_snapshot  # noqa: E402


def main(argv):
    exe, snap, cs_s, start_s, end_s = argv[0], argv[1], argv[2], argv[3], argv[4]
    cs = int(cs_s, 16) & 0xFFFF
    start = int(start_s, 16) & 0xFFFF
    end = int(end_s, 16) & 0xFFFF

    rt = load_snapshot(exe, snap)
    cpu = rt.cpu
    cpu.replacement_hooks.clear()
    cpu.hook_verifier = None
    cpu.trace_enabled = True
    cpu.pending_irq = None

    # Capture the asm text the interpreter produces, without trace parsing.
    orig_exec = cpu.execute_opcode
    last = {"asm": "?"}

    def capturing_exec(op, seg_override, rep):
        res = orig_exec(op, seg_override, rep)
        last["asm"] = res
        return res

    cpu.execute_opcode = capturing_exec
    mem = cpu.mem

    ip = start
    while ip <= end:
        inst = decode_one(lambda off: mem.rb(cs, off & 0xFFFF), ip)
        cpu.s.cs = cs
        cpu.s.ip = ip
        last["asm"] = "?"
        try:
            cpu.step()
            asm = last["asm"] or inst.mnemonic
        except Exception as exc:  # noqa: BLE001
            asm = f"{inst.mnemonic}  <exec-exc {type(exc).__name__}: {exc}>"
        print(f"{cs:04X}:{ip:04X}  {inst.raw.hex():<16}  {str(asm).strip()}")
        ip = (ip + inst.length) & 0xFFFF


if __name__ == "__main__":
    if len(sys.argv) != 6:
        print(__doc__.strip(), file=sys.stderr)
        raise SystemExit(2)
    main(sys.argv[1:])
