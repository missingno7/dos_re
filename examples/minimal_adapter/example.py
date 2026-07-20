"""Narrow CPU-backed adapter and focused-verifier example — no game assets.

This script builds a tiny synthetic DOS MZ executable (a stand-in "game"), then
demonstrates one optional low-level development slice:

  1. run the original binary in the VM (the oracle),
  2. select one authored implementation through the implementation catalog,
  3. let the differential verifier prove its CPU adapter byte-exact against the
     interpreted original ASM (and watch it catch a deliberately wrong hook),
  4. check that the backend's raw machine snapshot restores deterministically.

This is not the persistent ReplayArtifact workflow and not a required recovery
sequence. See ``examples/tiny_frame_game`` for an integration example and
``docs/getting_started.md`` for the composable workspace model.

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

from dos_re.cpu import CPU8086, HaltExecution  # noqa: E402
from dos_re.execution import (  # noqa: E402
    ImplementationCatalog,
    ImplementationDescriptor,
    ImplementationEntry,
    ImplementationOrigin,
    OverrideCategory,
    ProgramCoverage,
    plan_execution,
    profile_configuration,
)
from dos_re.player import GameFrontend  # noqa: E402
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


def select_inc_implementation(
    rt: Runtime,
    implementation_id: str,
    body,
) -> None:
    """Resolve and bind one authored body through the canonical plan."""
    address = (rt.program.entry_cs, ROUTINE_OFFSET)
    target = f"function:{address[0]:04x}:{address[1]:04x}:v1"

    def activate(runtime, targets):
        assert targets == (target,)

        def cpu_adapter(cpu: CPU8086) -> None:
            before = cpu.s.ax
            carry = cpu.s.flags & 0x0001
            cpu.s.ax = body(before)
            cpu.set_add_flags(before, 1, cpu.s.ax, 16)
            cpu.s.flags = (cpu.s.flags & ~0x0001) | carry
            cpu.s.ip = cpu.mem.rw(cpu.s.ss, cpu.s.sp)
            cpu.s.sp = (cpu.s.sp + 2) & 0xFFFF

        runtime.cpu.replacement_hooks[address] = cpu_adapter
        runtime.cpu.hook_names[address] = implementation_id

    catalog = ImplementationCatalog((ImplementationEntry(
        descriptor=ImplementationDescriptor(
            implementation_id=implementation_id,
            targets=frozenset({target}),
            origin=ImplementationOrigin.AUTHORED,
            category=OverrideCategory.FAITHFUL,
            implementation_digest=implementation_id,
        ),
        implementation=body,
        activate=activate,
    ),))
    plan = plan_execution(
        profile_configuration(
            "development",
            program_identity="minimal-example",
            selected_overrides=(implementation_id,),
        ),
        ProgramCoverage((target,), frozenset({target}), evidence_identity="v1"),
        catalog,
    )
    GameFrontend(ROOT).bind_execution_plan(rt, plan)


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

        # --- 2. A deliberately WRONG body: the verifier must catch it ---------
        def wrong_inc(value: int) -> int:
            return (value + 2) & 0xFFFF

        select_inc_implementation(rt, "wrong_inc", wrong_inc)
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

        def inc_body(value: int) -> int:
            # Real INC updates arithmetic flags but preserves CF. The semantic
            # body stays CPUless; the selected backend adapter handles flags.
            return (value + 1) & 0xFFFF

        select_inc_implementation(rt, "recovered_inc", inc_body)
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

    print("adapter example completed: focused verification and snapshot restore are green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
