"""Linear disassembler built on the framework's own 8086 decoder.

Loads a snapshot memory image, then linearly decodes a CS:offset..offset range
by executing one instruction at a time on a throwaway runtime and advancing by
the exact number of *code* bytes the decoder fetched.  Replacement hooks are
removed so raw original bytes are decoded.  Per-instruction exceptions are
swallowed so an odd opcode does not stop the sweep.

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

    # Count code bytes fetched during one instruction.
    orig_fetch8 = cpu.fetch8
    counter = {"n": 0}

    def counting_fetch8():
        counter["n"] += 1
        return orig_fetch8()

    cpu.fetch8 = counting_fetch8

    # Capture the asm text the decoder produces, without trace parsing.
    orig_exec = cpu.execute_opcode
    last = {"asm": "?"}

    def capturing_exec(op, seg_override, rep):
        res = orig_exec(op, seg_override, rep)
        last["asm"] = res
        return res

    cpu.execute_opcode = capturing_exec

    ip = start
    while ip <= end:
        cpu.s.cs = cs
        cpu.s.ip = ip
        counter["n"] = 0
        before_ip = ip
        last["asm"] = "?"
        asm = "?"
        try:
            cpu.step()
            asm = last["asm"]
        except Exception as exc:  # noqa: BLE001
            asm = f"<dec-exc {type(exc).__name__}: {exc}>"
        n = counter["n"] if counter["n"] > 0 else 1
        raw = bytes(cpu.mem.rb(cs, (before_ip + i) & 0xFFFF) for i in range(n))
        print(f"{cs:04X}:{before_ip:04X}  {raw.hex():<16}  {asm.strip()}")
        ip = before_ip + n
if __name__ == "__main__":
    if len(sys.argv) != 6:
        print(__doc__.strip(), file=sys.stderr)
        raise SystemExit(2)
    main(sys.argv[1:])
