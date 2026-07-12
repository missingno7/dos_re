"""32-bit lifter end-to-end: scan -> emit -> exec -> differential verify.

Game-free: synthesized functions are lifted mechanically and the generated
hook must survive the strict PM differential verifier (full-machine diff
against the interpreted original) — the same proof a real lift gets.
"""
from __future__ import annotations

import pytest

from dos_re.cpu386 import CPU386, FlatMemory
from dos_re.cpu import HaltExecution
from dos_re.dos4gw import DOS4GWHost
from dos_re.runtime import PMRuntime
from dos_re.lift.cfg32 import scan_function32
from dos_re.lift.emit32 import emit_function32
from dos_re.pm_verification import install_pm_hook_verifier

CODE = 0x1000
FUNC = 0x2000
HELPER = 0x2800
DATA = 0x3000
STACK = 0x8000


def build_rt(func_code: bytes, calls: int = 2) -> PMRuntime:
    mem = FlatMemory(size=0x10000 * 8)
    main = b""
    for _ in range(calls):
        disp = FUNC - (CODE + len(main) + 5)
        main += b"\xE8" + (disp & 0xFFFFFFFF).to_bytes(4, "little")
    main += b"\xF4"
    mem.load(CODE, main)
    mem.load(FUNC, func_code)
    # helper: add eax, 3 ; ret
    mem.load(HELPER, bytes.fromhex("83C003C3"))
    mem.w32(DATA, 7)
    cpu = CPU386(mem, eip=CODE, esp=STACK)
    dos = DOS4GWHost(mem, ".")
    cpu.interrupt_handler = dos.interrupt
    cpu.idt = dos.pm_vectors
    dos._cpu = cpu
    return PMRuntime(image=None, cpu=cpu, dos=dos, mem=mem)


def lift_and_install(rt, entry: int = FUNC, name: str = "lifted"):
    fetch = rt.mem.data.__getitem__
    scan = scan_function32(fetch, entry)
    assert scan.liftable, scan.refusals
    sig = bytes(rt.mem.data[entry:entry + 8])
    src = emit_function32(scan, name, signature=sig)
    ns: dict = {}
    exec(compile(src, f"<lifted 0x{entry:X}>", "exec"), ns)
    rt.cpu.replacement_hooks[entry] = ns[name]
    rt.cpu.hook_names[entry] = name
    return src


def run_to_halt(rt, max_steps=100_000):
    try:
        rt.cpu.run(max_steps)
    except HaltExecution:
        pass


# mov eax,[0x3000] ; add eax,5 ; mov [0x3004],eax ; ret
STRAIGHT = bytes.fromhex("A100300000" "83C005" "A304300000" "C3".replace(" ", ""))

# xor eax,eax ; mov ecx,5 ; L: add eax,ecx ; loop L ; mov [0x3004],eax ; ret
LOOPY = bytes.fromhex("31C0" "B905000000" "01C8" "E2FC" "A304300000" "C3".replace(" ", ""))

# push ebx ; mov ebx,[0x3000] ; lea eax,[ebx+ebx*2+7] ; call HELPER ;
# mov [0x3004],eax ; pop ebx ; ret        (1+6+4 = 11 bytes before the call)
CALLING = (bytes.fromhex("53" "8B1D00300000" "8D445B07".replace(" ", ""))
           + b"\xE8" + ((HELPER - (FUNC + 11 + 5)) & 0xFFFFFFFF).to_bytes(4, "little")
           + bytes.fromhex("A304300000" "5B" "C3".replace(" ", "")))


@pytest.mark.parametrize("func,expect", [
    (STRAIGHT, 12),
    (LOOPY, 15),
    (CALLING, 7 * 3 + 7 + 3),
])
def test_lifted_hook_passes_differential_verify(func, expect):
    rt = build_rt(func)
    lift_and_install(rt)
    verifier = install_pm_hook_verifier(rt)
    run_to_halt(rt)
    assert verifier.total_verified == 2
    assert rt.mem.r32(DATA + 4) == expect


def test_lifted_source_shape():
    rt = build_rt(STRAIGHT)
    src = lift_and_install(rt)
    assert "check_signature" in src
    assert "  # (interpreter fallback)" not in src   # fully native for this one
    assert "cpu.eip = cpu.pop(4)" in src             # the RET terminator


def test_fallback_lines_still_verify():
    # fldz ; fstp dword [0x3004] ; ret — x87 goes through interp_one32.
    func = bytes.fromhex("D9EE" "D91D04300000" "C3".replace(" ", ""))
    rt = build_rt(func)
    src = lift_and_install(rt)
    assert "interp_one32" in src
    verifier = install_pm_hook_verifier(rt)
    run_to_halt(rt)
    assert verifier.total_verified == 2
    assert rt.mem.r32(DATA + 4) == 0              # 0.0f bits


def test_signature_tripwire():
    from dos_re.lift.runtime32 import LiftRuntimeError
    rt = build_rt(STRAIGHT)
    lift_and_install(rt)
    rt.mem.data[FUNC] ^= 0xFF                     # self-modified entry
    with pytest.raises(LiftRuntimeError):
        run_to_halt(rt)
