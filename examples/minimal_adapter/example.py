"""Minimal end-to-end walkthrough of the dos_re oracle workflow — no game assets needed.

This script builds a tiny synthetic DOS MZ executable (a stand-in "game"), then
demonstrates the whole loop the framework exists for:

  1. run the original binary in the VM (the oracle),
  2. replace one original routine with a native Python hook,
  3. let the differential hook verifier prove the hook byte-exact against the
     interpreted original ASM (and watch it catch a deliberately wrong hook),
  4. snapshot the machine and replay deterministically from the snapshot.

Run it from the repo root:

    python examples/minimal_adapter/example.py

The synthetic program (loaded at some segment S, entry S:0000):

    0000:  mov ax, 0
    0003:  call 0010        ; the routine we will "recover"
    0006:  cmp ax, 5
    0009:  jb  0003
    000B:  hlt
    0010:  inc ax
    0011:  ret
"""
from __future__ import annotations

import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dos_re.asm import _inc_reg16_preserve_cf  # noqa: E402
from dos_re.cpu import CPU8086, HaltExecution  # noqa: E402
from dos_re.runtime import Runtime, create_runtime  # noqa: E402
from dos_re.snapshot import load_snapshot, write_snapshot  # noqa: E402
from dos_re.verification import (  # noqa: E402
    HookVerifierConfig,
    HookVerifyDivergence,
    install_hook_verifier,
)

CODE = bytes.fromhex(
    "b8 00 00"  # 0000: mov ax,0
    "e8 0a 00"  # 0003: call 0010
    "3d 05 00"  # 0006: cmp ax,5
    "72 f8"     # 0009: jb 0003
    "f4"        # 000B: hlt
    "90 90 90 90"  # padding
    "40"        # 0010: inc ax
    "c3"        # 0011: ret
)
ROUTINE_OFFSET = 0x0010


def build_example_exe(path: Path) -> Path:
    """Write a minimal valid MZ executable containing CODE."""
    header_paragraphs = 2
    header = struct.pack(
        "<14H",
        0x5A4D,                              # e_magic "MZ"
        (header_paragraphs * 16 + len(CODE)) % 512,  # bytes in last page
        1,                                   # pages
        0,                                   # relocations
        header_paragraphs,                   # header size in paragraphs
        0,                                   # min extra paragraphs
        0xFFFF,                              # max extra paragraphs
        0,                                   # initial SS (relative)
        0xFFFE,                              # initial SP
        0,                                   # checksum
        0,                                   # initial IP
        0,                                   # initial CS (relative)
        0x1C,                                # relocation table offset
        0,                                   # overlay number
    )
    image = bytearray(header)
    image.extend(b"\x00" * (header_paragraphs * 16 - len(header)))
    image.extend(CODE)
    path.write_bytes(image)
    return path


def run_to_halt(rt: Runtime, budget: int = 1000) -> None:
    try:
        rt.cpu.run(budget)
    except HaltExecution:
        pass
    if not rt.cpu.halted:
        raise RuntimeError("program did not halt within the step budget")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        exe = build_example_exe(tmp_path / "EXAMPLE.EXE")

        # --- 1. The oracle run: pure interpreted original ASM -----------------
        rt = create_runtime(exe)
        run_to_halt(rt)
        print(f"[oracle]   original ASM ran to HLT, AX = {rt.cpu.s.ax}  (expected 5)")
        assert rt.cpu.s.ax == 5

        # The hook address is derived from the *loaded program*, never hard-coded:
        # the adapter owns this knowledge.
        rt = create_runtime(exe)
        routine = (rt.program.entry_cs, ROUTINE_OFFSET)

        # --- 2. A deliberately WRONG hook: the verifier must catch it ---------
        def wrong_hook(cpu: CPU8086) -> None:
            cpu.s.ax = (cpu.s.ax + 2) & 0xFFFF          # bug: +2 instead of INC
            cpu.s.ip = cpu.mem.rw(cpu.s.ss, cpu.s.sp)   # RET
            cpu.s.sp = (cpu.s.sp + 2) & 0xFFFF

        rt.cpu.replacement_hooks[routine] = wrong_hook
        rt.cpu.hook_names[routine] = "wrong_inc"
        # strict mode = auto-continuation: no per-hook metadata needed; the
        # verifier runs the hook, then replays the ORIGINAL ASM to the same
        # address and diffs registers + flags + memory.
        install_hook_verifier(rt, HookVerifierConfig.strict(verify_all=True), stops={})
        try:
            run_to_halt(rt)
        except HookVerifyDivergence as exc:
            first_line = str(exc).strip().splitlines()[0]
            print(f"[verifier] caught the wrong hook, as it must: {first_line}")
        else:
            raise AssertionError("the verifier failed to catch a wrong hook")

        # --- 3. The CORRECT hook, verified on every call -----------------------
        rt = create_runtime(exe)

        def inc_hook(cpu: CPU8086) -> None:
            # Real INC updates arithmetic flags but PRESERVES CF — a naive
            # set_add_flags() would clear CF and the verifier would catch it on
            # the loop's second iteration (CF=1 from the caller's CMP).  The
            # dos_re.asm helpers encode exactly these 8086 semantics.
            _inc_reg16_preserve_cf(cpu, 0)              # INC AX
            cpu.s.ip = cpu.mem.rw(cpu.s.ss, cpu.s.sp)   # RET
            cpu.s.sp = (cpu.s.sp + 2) & 0xFFFF

        rt.cpu.replacement_hooks[routine] = inc_hook
        rt.cpu.hook_names[routine] = "recovered_inc"
        install_hook_verifier(rt, HookVerifierConfig.strict(verify_all=True), stops={})
        run_to_halt(rt)
        print(f"[hybrid]   recovered hook ran verified against the ASM oracle, AX = {rt.cpu.s.ax}")
        assert rt.cpu.s.ax == 5

        # --- 4. Snapshot determinism -------------------------------------------
        rt = create_runtime(exe)
        rt.cpu.run(4)  # stop mid-program
        snap_dir = tmp_path / "snapshot_mid"
        write_snapshot(rt, snap_dir, status="example mid-run snapshot",
                       steps=rt.cpu.instruction_count, trace_tail=())
        restored = load_snapshot(exe, snap_dir)
        run_to_halt(rt)
        run_to_halt(restored)
        print(f"[snapshot] live continuation AX = {rt.cpu.s.ax}, "
              f"restored continuation AX = {restored.cpu.s.ax}  (must match)")
        assert rt.cpu.s.ax == restored.cpu.s.ax == 5

    print("example completed: oracle run, verified hook, caught bad hook, deterministic snapshot")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
